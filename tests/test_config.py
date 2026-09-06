"""Проверки загрузки настроек.

Настройки читаются из окружения один раз при старте, поэтому ошибки здесь
проявляются как «контейнер не поднялся» — стоит проверить заранее.
"""

from __future__ import annotations

import base64
import os

import pytest
from pydantic import ValidationError

from app.core.config import Settings

BASE_ENV = {
    "BOT_TOKEN": "123456:AA-test",
    "CREDENTIALS_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
    "DATABASE_URL": "postgresql+asyncpg://bot:bot@db:5432/bot",
    "PUBLIC_BASE_URL": "https://schedule.example.ru",
}


def build(**overrides) -> Settings:
    # `_env_file=None` отвязывает тесты от `.env` разработчика: иначе они
    # проходят локально и ведут себя иначе в CI, где файла нет.
    return Settings(_env_file=None, **{**BASE_ENV, **overrides})  # type: ignore[arg-type]


def test_challenge_solver_is_off_by_default_and_tolerates_empty_value():
    assert build().ranepa_challenge_solver == "none"
    assert build(RANEPA_CHALLENGE_SOLVER="").ranepa_challenge_solver == "none"
    assert build(RANEPA_CHALLENGE_SOLVER=" Playwright ").ranepa_challenge_solver == "playwright"


def test_unknown_challenge_solver_fails_at_startup():
    """Опечатка в имени решателя должна ронять старт, а не молча выключать проверку."""
    with pytest.raises(ValidationError):
        build(RANEPA_CHALLENGE_SOLVER="selenium")


def test_empty_optional_value_is_treated_as_absent():
    """Необязательные переменные в `.env` оставляют пустыми, а не удаляют.

    Регрессия: раньше пустой OWNER_CHAT_ID ронял приложение на старте.
    """
    assert build(OWNER_CHAT_ID="").owner_chat_id is None
    assert build(OWNER_CHAT_ID="  ").owner_chat_id is None
    assert build(OWNER_CHAT_ID="42").owner_chat_id == 42


def test_trailing_slash_does_not_double_up_in_urls():
    settings = build(PUBLIC_BASE_URL="https://schedule.example.ru/")

    assert settings.feed_url("abc") == "https://schedule.example.ru/feed/abc.ics"


def test_webcal_url_uses_subscription_scheme():
    """`https://` iOS скачает файлом — подписки не получится, нужен `webcal://`."""
    settings = build()

    assert settings.webcal_url("abc").startswith("webcal://")
    assert settings.webcal_url("abc").endswith("/feed/abc.ics")


def test_secrets_are_masked_in_repr():
    """Настройки попадают в логи при отладке — токен там оказаться не должен."""
    settings = build()

    assert "123456:AA-test" not in repr(settings)


def test_missing_required_setting_fails_fast():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, BOT_TOKEN="123456:AA-test")  # type: ignore[arg-type]


def test_sync_concurrency_is_bounded():
    """Верхняя граница защищает чужой сервер от нашего параллелизма."""
    with pytest.raises(ValidationError):
        build(SYNC_CONCURRENCY="100")
