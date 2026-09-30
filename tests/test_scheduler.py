"""Проверки фоновых задач: правила времени и рассылка сводки.

Ночной обход целиком здесь не гоняется — он состоит из уже проверенной
синхронизации. Проверяются решения, которые принимает сам планировщик: когда
запускаться, кому и когда слать сводку, когда тревожить владельца.
"""

from __future__ import annotations

import base64
import os
from datetime import date, datetime, timezone

import pytest_asyncio

from app.bot.scheduler import (
    MOSCOW,
    NightlyReport,
    SchedulerContext,
    digest_is_due,
    night_was_lost,
    recover_after_maintenance,
    report_to_owner,
    seconds_until,
    send_due_digests,
)
from app.core.config import Settings
from app.core.crypto import CredentialsCipher
from app.db.models import User
from app.db.repo import UserRepository
from app.db.session import create_schema, make_engine, make_session_factory
from app.ranepa.models import DaySchedule, EduGroup, Lesson, LessonFormat, Schedule, StudentProfile


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def settings(**overrides) -> Settings:
    base = {
        "BOT_TOKEN": "1:x",
        "CREDENTIALS_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "PUBLIC_BASE_URL": "https://example.org",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


def msk(hour: int, minute: int = 0, day: int = 7) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=MOSCOW)


# --- seconds_until ---


def test_next_run_is_later_today_when_hour_is_ahead():
    delay = seconds_until(3, now=msk(1, 0), jitter_minutes=0)

    assert delay == 2 * 3600


def test_next_run_rolls_to_tomorrow_when_hour_has_passed():
    delay = seconds_until(3, now=msk(4, 0), jitter_minutes=0)

    assert delay == 23 * 3600


def test_jitter_spreads_start_times():
    delays = {seconds_until(3, now=msk(1, 0), jitter_minutes=20) for _ in range(20)}

    assert len(delays) > 1
    assert all(2 * 3600 <= d <= 2 * 3600 + 20 * 60 for d in delays)


# --- digest_is_due ---


def user(**overrides) -> User:
    fields = dict(
        telegram_id=1,
        feed_token="t",
        morning_digest_at="08:00",
        refresh_token_encrypted="x",
        is_active=True,
    )
    fields.update(overrides)
    return User(**fields)


def test_digest_due_exactly_at_configured_minute():
    assert digest_is_due(user(), now=msk(8, 0), last_sent=None)
    assert not digest_is_due(user(), now=msk(8, 1), last_sent=None)
    assert not digest_is_due(user(), now=msk(7, 59), last_sent=None)


def test_digest_not_repeated_same_day():
    assert not digest_is_due(user(), now=msk(8, 0), last_sent=date(2026, 9, 7))
    assert digest_is_due(user(), now=msk(8, 0), last_sent=date(2026, 9, 6))


def test_digest_disabled_or_disconnected():
    assert not digest_is_due(user(morning_digest_at=None), now=msk(8, 0), last_sent=None)
    assert not digest_is_due(user(refresh_token_encrypted=None), now=msk(8, 0), last_sent=None)
    assert not digest_is_due(user(morning_digest_at="мусор"), now=msk(8, 0), last_sent=None)


def test_digest_time_is_moscow_not_utc():
    at_utc = datetime(2026, 9, 7, 5, 0, tzinfo=timezone.utc)  # 08:00 МСК

    assert digest_is_due(user(), now=at_utc, last_sent=None)


# --- send_due_digests с базой ---


PROFILE = StudentProfile(
    student_uid="s", org_uid="o", group_name="ЭИ-25", groups=(EduGroup("g", "ЭИ-25"),)
)


@pytest_asyncio.fixture
async def ctx():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    factory = make_session_factory(engine)
    cfg = settings()
    cipher = CredentialsCipher(cfg.credentials_key.get_secret_value())
    context = SchedulerContext(bot=FakeBot(), settings=cfg, cipher=cipher, session_factory=factory)
    async with factory() as session:
        repo = UserRepository(session, cipher)
        u = await repo.connect(1, refresh_token="r", access_valid_until=None, profile=PROFILE)
        u.morning_digest_at = "08:00"
        await repo.save_schedule(
            u,
            Schedule(
                days=[
                    DaySchedule(
                        day=date(2026, 9, 7),
                        lessons=[
                            Lesson(
                                subject="Матанализ",
                                start=datetime(2026, 9, 7, 9, 0),
                                end=datetime(2026, 9, 7, 10, 20),
                                teacher="Козко А. И.",
                                room="5 - 406",
                                building="Вернадского, 82 - корпус 5",
                                lesson_format=LessonFormat.ONSITE,
                            )
                        ],
                    ),
                    DaySchedule(day=date(2026, 9, 8)),
                ]
            ),
        )
        await session.commit()
    yield context
    await engine.dispose()


async def test_digest_is_sent_once_with_todays_lessons(ctx):
    sent = await send_due_digests(ctx, now=msk(8, 0, day=7))
    again = await send_due_digests(ctx, now=msk(8, 0, day=7))

    assert sent == 1 and again == 0
    chat_id, text = ctx.bot.sent[0]
    assert chat_id == 1
    assert "Сегодня" in text and "Матанализ" in text


async def test_digest_is_silent_on_a_day_without_lessons(ctx):
    sent = await send_due_digests(ctx, now=msk(8, 0, day=8))

    assert sent == 0
    assert ctx.bot.sent == []
    assert ctx.digest_sent[1] == date(2026, 9, 8)


async def test_digest_not_sent_at_other_minutes(ctx):
    assert await send_due_digests(ctx, now=msk(8, 5, day=7)) == 0


# --- report_to_owner ---


async def test_owner_is_not_bothered_when_all_is_well():
    bot = FakeBot()
    context = SchedulerContext(
        bot=bot, settings=settings(OWNER_CHAT_ID="42"), cipher=None, session_factory=None
    )

    await report_to_owner(context, NightlyReport(total=10, synced=10))

    assert bot.sent == []


async def test_owner_is_alerted_on_failures_and_total_outage():
    bot = FakeBot()
    context = SchedulerContext(
        bot=bot, settings=settings(OWNER_CHAT_ID="42"), cipher=None, session_factory=None
    )

    await report_to_owner(
        context, NightlyReport(total=3, failed=3, errors=["1: 503", "2: 503", "3: 503"])
    )

    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 42
    assert "Упали все" in text
    assert "1: 503" in text


async def test_no_owner_configured_means_no_alert():
    bot = FakeBot()
    context = SchedulerContext(bot=bot, settings=settings(), cipher=None, session_factory=None)

    await report_to_owner(context, NightlyReport(total=1, failed=1))

    assert bot.sent == []


async def test_maintenance_alert_names_the_cause_and_the_retry():
    bot = FakeBot()
    context = SchedulerContext(
        bot=bot, settings=settings(OWNER_CHAT_ID="42"), cipher=None, session_factory=None
    )

    await report_to_owner(context, NightlyReport(total=3, failed=3, closed=3))

    assert "техработах" in bot.sent[0][1]
    assert "каждые 30 мин" in bot.sent[0][1]


async def test_report_is_sent_after_recovery_even_when_all_is_well():
    bot = FakeBot()
    context = SchedulerContext(
        bot=bot, settings=settings(OWNER_CHAT_ID="42"), cipher=None, session_factory=None
    )

    await report_to_owner(
        context, NightlyReport(total=3, synced=3), title="После техработ", always=True
    )

    assert "После техработ" in bot.sent[0][1]


def test_cabinet_closed_means_nobody_synced_and_stub_was_seen():
    assert NightlyReport(total=223, failed=223, closed=223).cabinet_was_closed
    # Заглушка у части, но кто-то прошёл — кабинет открыт, просто не всем повезло.
    assert not NightlyReport(total=3, synced=1, failed=2, closed=2).cabinet_was_closed
    # Все упали, но не из-за заглушки — обычная ночь с 503, повторять по часам незачем.
    assert not NightlyReport(total=3, failed=3).cabinet_was_closed
    assert not NightlyReport().cabinet_was_closed


# --- recover_after_maintenance: пробы раз в полчаса, пока кабинет не вернётся ---


class RecoveryHarness:
    """Кабинет, который открывается на N-й пробе, и подменённые сон и часы."""

    def __init__(self, *, opens_on_probe: int, now: datetime) -> None:
        self.opens_on_probe = opens_on_probe
        self.probes = 0
        self.slept: list[float] = []
        self.synced = 0
        self.now = now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    async def cabinet_is_open(self, ctx) -> bool:
        self.probes += 1
        return self.probes >= self.opens_on_probe

    async def sync_everyone(self, ctx) -> NightlyReport:
        self.synced += 1
        return NightlyReport(total=5, synced=5)


def recovery_context(bot: FakeBot) -> SchedulerContext:
    return SchedulerContext(
        bot=bot, settings=settings(OWNER_CHAT_ID="42"), cipher=None, session_factory=None
    )


async def test_recovery_probes_every_half_hour_then_syncs_everyone(monkeypatch):
    """Ночь упала из-за заглушки. Две пробы впустую, третья видит JSON —
    полный обход и отчёт владельцу, даже если всё прошло гладко."""
    harness = RecoveryHarness(opens_on_probe=3, now=msk(3, 30))
    monkeypatch.setattr("app.bot.scheduler.cabinet_is_open", harness.cabinet_is_open)
    monkeypatch.setattr("app.bot.scheduler.sync_everyone", harness.sync_everyone)
    bot = FakeBot()

    report = await recover_after_maintenance(
        recovery_context(bot), interval=1800, sleep=harness.sleep, now=lambda: harness.now
    )

    # Первая проба — сразу: заглушку могли снять, пока шёл обход.
    assert harness.slept == [1800, 1800]
    assert harness.probes == 3
    assert harness.synced == 1
    assert report is not None and report.synced == 5
    assert len(bot.sent) == 1 and "после техработ" in bot.sent[0][1]


async def test_recovery_gives_way_to_the_regular_night(monkeypatch):
    """Кабинет не вернулся за сутки: за полчаса до штатной ночи пробы
    прекращаются — иначе два обхода наложились бы друг на друга."""
    harness = RecoveryHarness(opens_on_probe=10**9, now=msk(2, 45))
    monkeypatch.setattr("app.bot.scheduler.cabinet_is_open", harness.cabinet_is_open)
    monkeypatch.setattr("app.bot.scheduler.sync_everyone", harness.sync_everyone)
    bot = FakeBot()

    report = await recover_after_maintenance(
        recovery_context(bot), interval=1800, sleep=harness.sleep, now=lambda: harness.now
    )

    assert report is None
    assert harness.slept == []
    assert harness.synced == 0
    assert bot.sent == []


async def test_recovery_does_not_sync_while_cabinet_is_closed(monkeypatch):
    """Пока проба видит заглушку, две сотни пользователей по ней не гоняем."""
    harness = RecoveryHarness(opens_on_probe=10**9, now=msk(3, 30))
    ticks = {"n": 0}

    async def sleep(seconds: float) -> None:
        # После третьей пробы «наступает» время штатной ночи.
        ticks["n"] += 1
        if ticks["n"] == 3:
            harness.now = msk(2, 45, day=8)
        assert seconds == 1800

    monkeypatch.setattr("app.bot.scheduler.cabinet_is_open", harness.cabinet_is_open)
    monkeypatch.setattr("app.bot.scheduler.sync_everyone", harness.sync_everyone)

    report = await recover_after_maintenance(
        recovery_context(FakeBot()), interval=1800, sleep=sleep, now=lambda: harness.now
    )

    assert report is None
    assert harness.probes == 3
    assert harness.synced == 0


# --- night_was_lost: старт бота после пропавшей ночи ---


def synced_user(stamp: datetime | None) -> User:
    return User(telegram_id=1, is_active=True, notify_on_change=True, last_sync_at=stamp)


def test_night_is_lost_when_nobody_synced_after_it():
    """30.09.2026: последние удачные — вчера утром, ночь в 03:00 упала у всех,
    бот перезапущен в 05:10 МСК. Догонять надо сейчас, а не завтра."""
    users = [synced_user(msk(3, 7, day=29)), synced_user(msk(12, 51, day=29))]
    assert night_was_lost(users, now=msk(5, 10, day=30), sync_hour=3)


def test_night_is_fine_while_anyone_synced_after_it():
    users = [synced_user(msk(3, 7, day=29)), synced_user(msk(3, 9, day=30))]
    assert not night_was_lost(users, now=msk(5, 10, day=30), sync_hour=3)


def test_restart_before_the_night_is_not_a_lost_night():
    """В 01:00 последняя штатная ночь — вчерашняя, и она прошла."""
    users = [synced_user(msk(3, 9, day=29))]
    assert not night_was_lost(users, now=msk(1, 0, day=30), sync_hour=3)


def test_restart_during_the_sync_hour_counts_as_lost():
    """Перезапуск в 03:02, до старта обхода со случайным сдвигом: штатная
    петля уснёт до завтра, а люди останутся без обновления. Догоняем."""
    users = [synced_user(msk(3, 9, day=29))]
    assert night_was_lost(users, now=msk(3, 2, day=30), sync_hour=3)


def test_nothing_to_recover_without_users():
    assert not night_was_lost([], now=msk(12), sync_hour=3)


def test_users_who_never_synced_count_as_lost():
    """Подключились, а ночь упала до первого обхода — им тоже нужен догон."""
    assert night_was_lost([synced_user(None)], now=msk(12), sync_hour=3)


def test_naive_sync_timestamps_are_utc():
    user = synced_user(datetime(2026, 9, 7, 8))  # 11:00 МСК, после ночи
    assert not night_was_lost([user], now=msk(12), sync_hour=3)


# --- token_is_stale: граница доверия к ключу доступа ---

from app.bot.scheduler import token_is_stale  # noqa: E402


def test_fresh_token_is_not_stale():
    u = user(last_sync_at=msk(3, 0, day=6))

    assert not token_is_stale(u, now=msk(3, 0, day=7), max_age_days=30)


def test_token_without_sync_for_a_month_is_stale():
    u = user(last_sync_at=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert token_is_stale(u, now=datetime(2026, 9, 7, tzinfo=timezone.utc), max_age_days=30)
    assert not token_is_stale(u, now=datetime(2026, 8, 30, tzinfo=timezone.utc), max_age_days=30)


def test_never_synced_user_is_measured_from_connection_time():
    u = user(last_sync_at=None, connected_at=datetime(2026, 7, 1, tzinfo=timezone.utc))

    assert token_is_stale(u, now=datetime(2026, 9, 7, tzinfo=timezone.utc), max_age_days=30)


def test_user_without_any_timestamps_is_left_alone():
    assert not token_is_stale(user(), now=msk(3, 0), max_age_days=30)


def test_naive_timestamps_from_sqlite_are_treated_as_utc():
    u = user(last_sync_at=datetime(2026, 8, 1))

    assert token_is_stale(u, now=datetime(2026, 9, 7, tzinfo=timezone.utc), max_age_days=30)
