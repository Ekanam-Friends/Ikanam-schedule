"""Проверки сервисов подключения и синхронизации.

Кабинет заменён mock-транспортом httpx, база — SQLite в памяти. Проверяется
поток целиком: логин → группы → расписание → снапшоты, и что происходит на
каждом отказе кабинета.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import date
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.core.crypto import CredentialsCipher
from app.db.repo import UserRepository
from app.db.session import create_schema, make_engine, make_session_factory
from app.ranepa.client import RanepaClient
from app.services.account import AccountService, CabinetUnavailable, ConnectError, WrongCredentials
from app.services.sync import ReauthRequired, ScheduleSyncService, SyncError

FIXTURES = Path(__file__).parent / "fixtures"
SCHEDULE = json.loads((FIXTURES / "schedule_response.json").read_text(encoding="utf-8"))
GROUPS = json.loads((FIXTURES / "student_groups.json").read_text(encoding="utf-8"))
TOKENS = {"access_token": "acc-1", "refresh_token": "ref-1", "exp": 1788600000000, "fszet": "fz-1"}
TOKENS_2 = {"access_token": "acc-2", "refresh_token": "ref-2", "exp": 1788600000000}


class FakeCabinet:
    """Личный кабинет с настраиваемым поведением по каждому эндпоинту."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.login_response = httpx.Response(200, json=TOKENS)
        self.refresh_response = httpx.Response(200, json=TOKENS_2)
        self.groups_response = httpx.Response(200, json=GROUPS)
        self.schedule_response = httpx.Response(200, json=SCHEDULE)
        self.seen_password: str | None = None
        self.seen_refresh_fszet: str | None = None
        self.seen_schedule_params = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path.rsplit("n-api/", 1)[-1])
        if path.endswith("auth/login"):
            body = request.content.decode("utf-8", "replace")
            if 'name="password"' in body:
                self.seen_password = body.split('name="password"')[1].split("\r\n\r\n")[1].split("\r\n")[0]
            return self.login_response
        if path.endswith("auth/refresh"):
            self.seen_refresh_fszet = request.headers.get("fszet")
            return self.refresh_response
        if path.endswith("student-groups"):
            return self.groups_response
        if path.endswith("schedule"):
            self.seen_schedule_params = request.url.params
            return self.schedule_response
        return httpx.Response(404)

    def client_factory(self, **kwargs) -> RanepaClient:
        return RanepaClient(transport=httpx.MockTransport(self.handler), **kwargs)


@pytest_asyncio.fixture
async def repo():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    factory = make_session_factory(engine)
    async with factory() as session:
        cipher = CredentialsCipher(base64.urlsafe_b64encode(os.urandom(32)).decode())
        yield UserRepository(session, cipher)
    await engine.dispose()


@pytest.fixture
def cabinet() -> FakeCabinet:
    return FakeCabinet()


@pytest.fixture
def services(repo, cabinet):
    sync = ScheduleSyncService(repo, client_factory=cabinet.client_factory)
    return AccountService(repo, sync, client_factory=cabinet.client_factory), sync


async def test_connect_runs_full_flow(services, cabinet, repo):
    account, _ = services

    result = await account.connect(42, "student@ranepa.ru", "p@ss")

    assert cabinet.calls == ["auth/login", "manual/student-groups", "auth/refresh", "schedule"]
    assert result.profile.group_name == "ЭИ-25"
    assert result.lessons_count == 3
    user = await repo.get(42)
    assert user.is_connected
    assert user.group_uids == ["id-group-1", "id-group-2", "id-group-3"]
    loaded = await repo.load_schedule(user)
    assert len(loaded.lessons) == 3


async def test_connect_uses_profile_groups_and_org_for_schedule(services, cabinet):
    account, _ = services

    await account.connect(42, "l", "p")

    params = cabinet.seen_schedule_params
    assert params.get_list("filter[]") == ["id-group-1", "id-group-2", "id-group-3"]
    assert params["uid_org"] == "id-org"
    assert len(params.get_list("dates[]")) == 21


async def test_password_reaches_cabinet_but_not_the_database(services, cabinet, repo):
    account, _ = services

    await account.connect(42, "l", "very-secret")

    assert cabinet.seen_password == "very-secret"
    user = await repo.get(42)
    for column in user.__table__.columns.keys():
        value = getattr(user, column)
        assert value != "very-secret", column
        assert not (isinstance(value, str) and "very-secret" in value), column


async def test_stored_token_is_the_refreshed_one(services, cabinet, repo):
    """Кабинет выдаёт новую пару на каждое обновление — старый refresh мёртв."""
    account, _ = services

    await account.connect(42, "l", "p")

    assert repo.refresh_token_of(await repo.get(42)) == "ref-2"


async def test_wrong_password(services, cabinet, repo):
    account, _ = services
    cabinet.login_response = httpx.Response(401, json={"message": "Неверный логин или пароль"})

    with pytest.raises(WrongCredentials):
        await account.connect(42, "l", "wrong")

    assert await repo.get(42) is None


async def test_cabinet_down_is_reported_as_temporary(services, cabinet):
    account, _ = services
    cabinet.login_response = httpx.Response(503, text="down")

    with pytest.raises(CabinetUnavailable):
        await account.connect(42, "l", "p")


async def test_waf_block_is_not_blamed_on_the_user(services, cabinet):
    account, _ = services
    cabinet.login_response = httpx.Response(403, text="Forbidden", headers={"content-type": "text/plain"})

    with pytest.raises(CabinetUnavailable):
        await account.connect(42, "l", "p")


async def test_account_without_groups_is_not_connected(services, cabinet, repo):
    """Сотрудник или абитуриент: вход есть, расписания нет — токен не сохраняем."""
    account, _ = services
    cabinet.groups_response = httpx.Response(200, json={"items": []})

    with pytest.raises(ConnectError):
        await account.connect(42, "l", "p")

    assert await repo.get(42) is None


async def test_failed_first_sync_does_not_undo_connection(services, cabinet, repo):
    account, _ = services
    cabinet.schedule_response = httpx.Response(503, text="down")

    result = await account.connect(42, "l", "p")

    assert result.lessons_count == 0
    user = await repo.get(42)
    assert user.is_connected
    assert user.last_sync_error


async def test_sync_deactivates_user_when_refresh_is_rejected(services, cabinet, repo):
    account, sync = services
    await account.connect(42, "l", "p")
    cabinet.refresh_response = httpx.Response(401, json={"message": "expired"})

    with pytest.raises(ReauthRequired):
        await sync.sync_user(await repo.get(42))

    user = await repo.get(42)
    assert not user.is_active
    assert not user.is_connected


async def test_sync_keeps_old_snapshots_on_temporary_failure(services, cabinet, repo):
    """Кабинет лёг — расписание в базе остаётся, пользователь видит вчерашнее."""
    account, sync = services
    await account.connect(42, "l", "p")
    cabinet.schedule_response = httpx.Response(502, text="bad gateway")

    with pytest.raises(SyncError):
        await sync.sync_user(await repo.get(42))

    user = await repo.get(42)
    assert user.is_active
    assert len((await repo.load_schedule(user)).lessons) == 3


async def test_sync_requests_horizon_from_given_day(services, cabinet, repo):
    account, sync = services
    await account.connect(42, "l", "p")

    await sync.sync_user(await repo.get(42), today=date(2026, 9, 7), horizon_days=3)

    assert cabinet.seen_schedule_params.get_list("dates[]") == [
        "2026-09-07T00:00:00.000",
        "2026-09-08T00:00:00.000",
        "2026-09-09T00:00:00.000",
    ]


async def test_disconnect(services, repo):
    account, _ = services
    await account.connect(42, "l", "p")

    assert await account.disconnect(42) is True
    assert await repo.get(42) is None


async def test_refresh_carries_fszet_from_login(services, cabinet, repo):
    """Без заголовка `fszet` кабинет отвечает на refresh 400 — проверено вживую."""
    account, sync = services
    await account.connect(42, "l", "p")
    assert cabinet.seen_refresh_fszet == "fz-1"

    cabinet.seen_refresh_fszet = None
    await sync.sync_user(await repo.get(42))

    assert cabinet.seen_refresh_fszet == "fz-1"
    assert repo.fszet_of(await repo.get(42)) == "fz-1"
