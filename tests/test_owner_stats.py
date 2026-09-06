"""Утренняя сводка владельцу: время по поясу с летним временем, отправка."""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.bot.scheduler import SchedulerContext, seconds_until_local, send_owner_stats
from app.core.config import Settings
from app.core.crypto import CredentialsCipher
from app.db.repo import UserRepository
from app.db.session import create_schema, make_engine, make_session_factory
from app.ranepa.models import EduGroup, StudentProfile

PROFILE = StudentProfile(
    student_uid="id-student",
    org_uid="id-org",
    group_name="ЭИ-25",
    groups=(EduGroup(uid="id-g1", name="ЭИ-25"),),
    status="Студент",
)


def settings(**overrides) -> Settings:
    base = {
        "BOT_TOKEN": "1:x",
        "CREDENTIALS_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "PUBLIC_BASE_URL": "https://example.org",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.photos: list[tuple[int, bytes]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))

    async def send_photo(self, chat_id: int, photo) -> None:
        self.photos.append((chat_id, photo.data))


# --- Настройки ---


def test_defaults_are_six_in_the_morning_kyiv():
    cfg = settings()
    assert cfg.owner_stats_at == "06:00"
    assert cfg.owner_stats_tz == "Europe/Kyiv"


def test_clock_is_normalised_and_empty_disables():
    assert settings(OWNER_STATS_AT="6:5").owner_stats_at == "06:05"
    assert settings(OWNER_STATS_AT="").owner_stats_at is None


@pytest.mark.parametrize("value", ["25:00", "06:60", "утром", "0600"])
def test_bad_clock_fails_at_startup(value):
    with pytest.raises(ValidationError):
        settings(OWNER_STATS_AT=value)


def test_unknown_timezone_fails_at_startup():
    with pytest.raises(ValidationError):
        settings(OWNER_STATS_TZ="Europe/Kyyv")


# --- Время с учётом летнего времени ---


def test_six_kyiv_in_summer_is_three_utc():
    now = datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc)
    assert seconds_until_local("06:00", "Europe/Kyiv", now=now) == 3 * 3600


def test_six_kyiv_in_winter_is_four_utc():
    """Зимой Киев UTC+2: те же 06:00 наступают на час позже по UTC, чем летом."""
    now = datetime(2026, 12, 10, 0, 0, tzinfo=timezone.utc)
    assert seconds_until_local("06:00", "Europe/Kyiv", now=now) == 4 * 3600


def test_past_time_rolls_to_tomorrow():
    now = datetime(2026, 7, 10, 3, 0, 1, tzinfo=timezone.utc)  # 06:00:01 по Киеву
    assert seconds_until_local("06:00", "Europe/Kyiv", now=now) == 24 * 3600 - 1


# --- Отправка ---


@pytest_asyncio.fixture
async def factory():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    factory = make_session_factory(engine)
    yield factory
    await engine.dispose()


async def test_owner_gets_text_and_chart(factory):
    cfg = settings(OWNER_CHAT_ID="42")
    cipher = CredentialsCipher(cfg.credentials_key.get_secret_value())
    async with factory() as session:
        await UserRepository(session, cipher).connect(
            1, refresh_token="r", access_valid_until=None, profile=PROFILE
        )
        await session.commit()
    bot = FakeBot()
    ctx = SchedulerContext(bot=bot, settings=cfg, cipher=cipher, session_factory=factory)

    assert await send_owner_stats(ctx)

    assert len(bot.messages) == 1 and bot.messages[0][0] == 42
    assert "Пользователей: <b>1</b>" in bot.messages[0][1]
    assert len(bot.photos) == 1 and bot.photos[0][1][:4] == b"\x89PNG"


async def test_nothing_is_sent_without_owner(factory):
    cfg = settings()
    bot = FakeBot()
    ctx = SchedulerContext(
        bot=bot,
        settings=cfg,
        cipher=CredentialsCipher(cfg.credentials_key.get_secret_value()),
        session_factory=factory,
    )

    assert not await send_owner_stats(ctx)
    assert bot.messages == [] and bot.photos == []
