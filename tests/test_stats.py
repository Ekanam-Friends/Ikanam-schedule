"""Статистика для владельца: счётчики без людей, сводка, график.

Главное обещание здесь — в таблице нет ни одного идентификатора человека,
а счётчик не теряет события при одновременной записи.
"""

from __future__ import annotations

import base64
import os
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.core.crypto import CredentialsCipher
from app.db.activity import MOSCOW, ActivityLog, command_kind
from app.db.models import ActivityCounter
from app.db.repo import UserRepository
from app.db.session import create_schema, make_engine, make_session_factory
from app.ranepa.models import EduGroup, StudentProfile
from app.services.stats import collect, format_summary, render_chart, series_totals

PROFILE = StudentProfile(
    student_uid="id-student",
    org_uid="id-org",
    group_name="ЭИ-25",
    groups=(EduGroup(uid="id-g1", name="ЭИ-25"),),
    status="Студент",
)


@pytest_asyncio.fixture
async def session():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    factory = make_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def repo(session) -> UserRepository:
    return UserRepository(
        session, CredentialsCipher(base64.urlsafe_b64encode(os.urandom(32)).decode())
    )


@pytest.fixture
def activity(session) -> ActivityLog:
    return ActivityLog(session)


# --- Разбор команд ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/today", "cmd:today"),
        ("/Today@Ikanam_schedule_bot", "cmd:today"),
        ("/week тест", "cmd:week"),
        ("  /status", "cmd:status"),
        ("student@ranepa.ru", None),
        ("мой пароль", None),
        ("/", None),
        ("", None),
        (None, None),
    ],
)
def test_command_kind(text, expected):
    assert command_kind(text) == expected


# --- Счётчики ---


async def test_same_slot_is_summed_not_duplicated(activity, session):
    at = datetime(2026, 9, 7, 11, 5, tzinfo=MOSCOW)
    await activity.record("cmd:today", when=at)
    await activity.record("cmd:today", when=at + timedelta(minutes=20))
    await activity.record("sync:ok", when=at)

    counts = await activity.by_day(since=date(2026, 9, 7), until=date(2026, 9, 7))

    assert counts == {date(2026, 9, 7): {"cmd:today": 2, "sync:ok": 1}}


async def test_slots_are_in_moscow_time(activity):
    """23:30 UTC — это уже завтра по Москве; счётчик должен лечь в московский день."""
    await activity.record("cmd:today", when=datetime(2026, 9, 7, 23, 30, tzinfo=timezone.utc))

    counts = await activity.by_hour(since=date(2026, 9, 8), until=date(2026, 9, 8))

    assert counts == {2: {"cmd:today": 1}}


async def test_counters_carry_no_user_reference():
    columns = {column.name for column in ActivityCounter.__table__.columns}
    assert columns == {"id", "day", "hour", "kind", "count"}


# --- Сводка ---


async def test_collect_counts_users_and_activity(repo, activity):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=MOSCOW)
    await repo.connect(1, refresh_token="r", access_valid_until=None, profile=PROFILE)
    await repo.connect(2, refresh_token="r", access_valid_until=None, profile=PROFILE)
    await activity.record("cmd:today", when=now)
    await activity.record("login:ok", when=now)
    await activity.record("sync:error", when=now - timedelta(days=1))

    stats = await collect(repo, activity, now=now, days=3)

    assert stats.users_total == stats.users_connected == stats.users_active == 2
    assert stats.groups == 1
    assert stats.connected_today == 2
    assert stats.days == [date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7)]
    assert series_totals(stats.today()) == {
        "Команды": 1,
        "Синхронизации": 0,
        "Ошибки синхр.": 0,
        "Календари": 0,
        "Подключения": 1,
    }
    assert stats.by_day[date(2026, 9, 6)] == {"sync:error": 1}


async def test_summary_mentions_the_numbers_and_top_commands(repo, activity):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=MOSCOW)
    await repo.connect(1, refresh_token="r", access_valid_until=None, profile=PROFILE)
    for _ in range(3):
        await activity.record("cmd:today", when=now)
    await activity.record("cmd:week", when=now)

    text = format_summary(await collect(repo, activity, now=now))

    assert "Пользователей: <b>1</b>" in text
    assert "команд 4" in text
    assert "/today 3, /week 1" in text


async def test_chart_is_a_png(repo, activity):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=MOSCOW)
    await activity.record("cmd:today", when=now)

    png = render_chart(await collect(repo, activity, now=now))

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 10_000


# --- Кто видит команду ---


def test_stats_is_only_for_the_owner():
    from types import SimpleNamespace

    from app.bot.handlers.stats import is_owner

    base = {
        "BOT_TOKEN": "1:x",
        "CREDENTIALS_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "PUBLIC_BASE_URL": "https://s.example",
    }
    with_owner = Settings(_env_file=None, OWNER_CHAT_ID="42", **base)  # type: ignore[arg-type]
    without_owner = Settings(_env_file=None, **base)  # type: ignore[arg-type]
    owner = SimpleNamespace(from_user=SimpleNamespace(id=42))
    stranger = SimpleNamespace(from_user=SimpleNamespace(id=7))

    assert is_owner(owner, with_owner)
    assert not is_owner(stranger, with_owner)
    assert not is_owner(owner, without_owner)
