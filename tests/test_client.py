"""Проверки клиента личного кабинета.

Живой кабинет здесь не дёргается: запросы перехватываются транспортом httpx.
Проверяется то, что легко сломать незаметно, — форма запроса (кабинет молча
вернёт пустоту, если перепутать имена параметров) и разбор ошибок, от которого
зависит, будем ли мы ретраить чужой аккаунт до блокировки.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.ranepa.client import (
    AuthError,
    BlockedError,
    RanepaClient,
    TemporaryError,
    Tokens,
)

LOGIN_OK = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "exp": 1788600000000,
}


def client_with(handler, **kwargs) -> RanepaClient:
    return RanepaClient(transport=httpx.MockTransport(handler), **kwargs)


async def test_login_sends_multipart_fields():
    """Кабинет ждёт multipart-форму: JSON он не принимает."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode("utf-8", "replace")
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, json=LOGIN_OK)

    async with client_with(handler) as client:
        tokens = await client.login("student@ranepa.ru", "secret")

    assert seen["url"].endswith("auth/login")
    assert 'name="login"' in seen["body"]
    assert 'name="password"' in seen["body"]
    assert 'name="remember_me"' in seen["body"]
    assert tokens.access_token == "access-1"
    assert tokens.refresh_token == "refresh-1"


async def test_requests_look_like_a_browser():
    """Без браузерного User-Agent кабинет отвечает 403 — это уже проверено вживую."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, json=LOGIN_OK)

    async with client_with(handler) as client:
        await client.login("a", "b")

    assert "Mozilla/5.0" in seen["ua"]


async def test_wrong_password_is_not_retried():
    """Повторять вход с неверным паролем — верный способ заблокировать аккаунт."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"message": "Неверный логин или пароль"})

    async with client_with(handler) as client:
        with pytest.raises(AuthError, match="Неверный логин"):
            await client.login("a", "wrong")

    assert calls == 1


async def test_auth_error_text_comes_from_nested_cabinet_json():
    """Живой кабинет кладёт причину во второй слой: `data.message`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": "Unauthorized", "data": {"message": "Неверный логин или пароль."}},
        )

    async with client_with(handler) as client:
        with pytest.raises(AuthError, match="Неверный логин или пароль"):
            await client.login("a", "b")


async def test_login_without_tokens_in_body_is_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "Неверный логин или пароль"})

    async with client_with(handler) as client:
        with pytest.raises(AuthError, match="Неверный логин"):
            await client.login("a", "b")


async def test_waf_block_is_distinguished_from_bad_credentials():
    """403 без JSON — это защита сайта, а не проблема с паролем пользователя."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden", headers={"content-type": "text/plain"})

    async with client_with(handler) as client:
        with pytest.raises(BlockedError):
            await client.login("a", "b")


async def test_schedule_repeats_filters_and_dates():
    """Даты перечисляются по одной: диапазона кабинет не понимает."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json={"data": []})

    tokens = Tokens("access", "refresh")
    async with client_with(handler, tokens=tokens) as client:
        await client.get_schedule(
            days=[date(2026, 9, 4), date(2026, 9, 5)],
            group_uids=["group-a", "group-b"],
        )

    params = seen["params"]
    assert params.get_list("dates[]") == [
        "2026-09-04T00:00:00.000",
        "2026-09-05T00:00:00.000",
    ]
    assert params.get_list("filter[]") == ["group-a", "group-b"]
    assert params["type"] == "group"
    assert params["branch_type"] == "main"


async def test_schedule_requires_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with client_with(handler) as client:
        with pytest.raises(AuthError):
            await client.get_schedule([date(2026, 9, 4)], ["group-a"])


async def test_schedule_sends_bearer_token():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={})

    async with client_with(handler, tokens=Tokens("access-xyz", "refresh")) as client:
        await client.get_schedule([date(2026, 9, 4)], ["group-a"])

    assert seen["auth"] == "Bearer access-xyz"


async def test_empty_arguments_are_rejected_before_the_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("запрос не должен уходить")

    async with client_with(handler, tokens=Tokens("a", "b")) as client:
        with pytest.raises(ValueError):
            await client.get_schedule([], ["group"])
        with pytest.raises(ValueError):
            await client.get_schedule([date(2026, 9, 4)], [])


async def test_server_error_is_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"data": "ok"})

    async with client_with(handler, tokens=Tokens("a", "b")) as client:
        result = await client._request("GET", "schedule", authorized=True, attempts=3)

    assert calls == 3
    assert result == {"data": "ok"}


async def test_persistent_server_error_raises_temporary():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    async with client_with(handler, tokens=Tokens("a", "b")) as client:
        with pytest.raises(TemporaryError):
            await client._request("GET", "schedule", authorized=True, attempts=2)


async def test_refresh_replaces_tokens():
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"refresh-1" in request.content
        return httpx.Response(
            200, json={"access_token": "access-2", "refresh_token": "refresh-2"}
        )

    async with client_with(handler, tokens=Tokens("access-1", "refresh-1")) as client:
        tokens = await client.refresh()

    assert tokens.access_token == "access-2"
    assert tokens.refresh_token == "refresh-2"


async def test_refresh_without_tokens_fails_clearly():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("запрос не должен уходить")

    async with client_with(handler) as client:
        with pytest.raises(AuthError):
            await client.refresh()


async def test_expired_token_is_refreshed_before_use():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "new-r"})

    expired = Tokens(
        "old", "old-r", expires_at=datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    async with client_with(handler, tokens=expired) as client:
        await client.ensure_access()

    assert any("auth/refresh" in path for path in calls)
    assert client.tokens.access_token == "new"


async def test_fresh_token_is_left_alone():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("обновление не требуется")

    fresh = Tokens("a", "b", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    async with client_with(handler, tokens=fresh) as client:
        await client.ensure_access()


def test_expiration_accepts_milliseconds_and_seconds():
    """`exp` приходит числом, и порядок величины у него разный."""
    from app.ranepa.client import _parse_expiration

    in_ms = _parse_expiration(1788600000000)
    in_s = _parse_expiration(1788600000)

    assert in_ms is not None and in_s is not None
    assert abs((in_ms - in_s).total_seconds()) < 1


def test_expiration_survives_garbage():
    from app.ranepa.client import _parse_expiration

    assert _parse_expiration(None) is None
    assert _parse_expiration("не число") is None


# --- Заголовок version и учебные данные ---


async def test_every_request_carries_api_version_header():
    """Без `version` API отвечает 400 HttpVersionException — проверено вживую.

    И это именно `1.0`, а не `currentVersion` из эндпоинта `version`: фронтенд
    читает оттуда несуществующее поле и всегда шлёт запасное значение.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("version", "<нет>"))
        return httpx.Response(200, json=LOGIN_OK)

    async with client_with(handler, tokens=Tokens("a", "b")) as client:
        await client.login("a", "b")
        await client.get_student_groups()
        await client.get_schedule([date(2026, 9, 4)], ["g"])

    assert seen == ["1.0", "1.0", "1.0"]


async def test_student_groups_endpoint_and_auth():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"items": []})

    async with client_with(handler, tokens=Tokens("tok", "r")) as client:
        await client.get_student_groups()

    assert seen["path"].endswith("manual/student-groups")
    assert seen["auth"] == "Bearer tok"


def test_client_has_no_way_to_fetch_full_profile():
    """`profile` отдаёт СНИЛС и адреса; метода для него нет намеренно."""
    assert not hasattr(RanepaClient, "get_profile")
