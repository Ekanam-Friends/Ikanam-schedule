"""Проверки репозитория на SQLite в памяти.

Здесь важны обещания, данные пользователю: пароль не хранится, `/logout`
не оставляет следов, а сохранённое расписание читается назад без потерь.
"""

from __future__ import annotations

import base64
import os
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.crypto import CredentialsCipher
from app.db.models import LessonRevision, ScheduleSnapshot, User
from app.db.repo import UserRepository
from app.db.session import create_schema, make_engine, make_session_factory
from app.ranepa.models import DaySchedule, EduGroup, Lesson, LessonFormat, Schedule, StudentProfile


def make_cipher() -> CredentialsCipher:
    return CredentialsCipher(base64.urlsafe_b64encode(os.urandom(32)).decode())


PROFILE = StudentProfile(
    student_uid="id-student",
    org_uid="id-org",
    group_name="ЭИ-25",
    groups=(EduGroup(uid="id-g1", name="ЭИ-25"), EduGroup(uid="id-g2", name="ЭИ/ТЭУ-25")),
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
def cipher() -> CredentialsCipher:
    return make_cipher()


@pytest.fixture
def repo(session, cipher) -> UserRepository:
    return UserRepository(session, cipher)


def sample_schedule() -> Schedule:
    return Schedule(
        days=[
            DaySchedule(
                day=date(2026, 9, 4),
                lessons=[
                    Lesson(
                        subject="Матанализ",
                        start=datetime(2026, 9, 4, 9, 0),
                        end=datetime(2026, 9, 4, 10, 20),
                        teacher="Козко А. И.",
                        room="5 - 406",
                        building="Вернадского, 82 - корпус 5",
                        lesson_format=LessonFormat.ONSITE,
                        lesson_type="Лекция",
                    )
                ],
            ),
            DaySchedule(day=date(2026, 9, 5)),
        ],
        fetched_at=datetime(2026, 9, 4, 3, 0, tzinfo=timezone.utc),
    )


async def test_connect_stores_encrypted_refresh_token_and_no_password(repo, cipher, session):
    user = await repo.connect(
        1, refresh_token="refresh-secret", access_valid_until=None, profile=PROFILE
    )

    assert user.is_connected
    assert user.refresh_token_encrypted != "refresh-secret"
    assert cipher.decrypt(user.refresh_token_encrypted) == "refresh-secret"
    assert repo.refresh_token_of(user) == "refresh-secret"
    assert "password" not in {c.name for c in User.__table__.columns}


async def test_connect_keeps_profile_needed_for_schedule(repo):
    user = await repo.connect(1, refresh_token="r", access_valid_until=None, profile=PROFILE)

    assert user.org_uid == "id-org"
    assert user.group_name == "ЭИ-25"
    assert user.group_uids == ["id-g1", "id-g2"]


async def test_feed_token_is_random_and_unique(repo):
    first = await repo.get_or_create(1)
    second = await repo.get_or_create(2)

    assert len(first.feed_token) >= 32
    assert first.feed_token != second.feed_token
    assert await repo.get_by_feed_token(first.feed_token) is first


async def test_reconnect_reactivates_user(repo):
    user = await repo.connect(1, refresh_token="r1", access_valid_until=None, profile=PROFILE)
    await repo.mark_failed(user, error="токен протух", deactivate=True)
    assert not user.is_active and not user.is_connected

    await repo.connect(1, refresh_token="r2", access_valid_until=None, profile=PROFILE)

    assert user.is_active
    assert user.consecutive_failures == 0
    assert user.last_sync_error is None


async def test_bump_revisions_survives_duplicate_uid_in_one_schedule(repo):
    """Страховка на случай, если дубликат UID всё же дошёл до базы: одна
    строка ревизии, а не IntegrityError на коммите."""
    user = await repo.connect(1, refresh_token="r", access_valid_until=None, profile=PROFILE)
    schedule = sample_schedule()
    first = schedule.days[0].lessons[0]
    schedule.days[0].lessons.append(first)

    await repo.bump_revisions(user, schedule)
    rows = (await repo._session.execute(select(LessonRevision))).scalars().all()

    assert [r.lesson_uid for r in rows].count(first.uid) == 1


async def test_schedule_round_trip(repo):
    user = await repo.connect(1, refresh_token="r", access_valid_until=None, profile=PROFILE)
    await repo.save_schedule(user, sample_schedule())

    loaded = await repo.load_schedule(user)

    assert [d.day for d in loaded.days] == [date(2026, 9, 4), date(2026, 9, 5)]
    lesson = loaded.days[0].lessons[0]
    assert lesson.subject == "Матанализ"
    assert lesson.start == datetime(2026, 9, 4, 9, 0)
    assert lesson.building == "Вернадского, 82 - корпус 5"
    assert lesson.lesson_format is LessonFormat.ONSITE
    assert loaded.days[1].is_empty


async def test_save_replaces_day_but_keeps_others(repo):
    """Новая выгрузка за две недели не должна стирать прошлый месяц."""
    user = await repo.connect(1, refresh_token="r", access_valid_until=None, profile=PROFILE)
    await repo.save_schedule(user, sample_schedule())

    update = Schedule(days=[DaySchedule(day=date(2026, 9, 4))])
    await repo.save_schedule(user, update)

    loaded = await repo.load_schedule(user)
    assert loaded.day_for(date(2026, 9, 4)).is_empty
    assert loaded.day_for(date(2026, 9, 5)) is not None


async def test_load_by_range(repo):
    user = await repo.connect(1, refresh_token="r", access_valid_until=None, profile=PROFILE)
    await repo.save_schedule(user, sample_schedule())

    only_friday = await repo.load_schedule(user, since=date(2026, 9, 4), until=date(2026, 9, 4))

    assert [d.day for d in only_friday.days] == [date(2026, 9, 4)]


async def test_logout_leaves_nothing_behind(repo, session):
    user = await repo.connect(1, refresh_token="r", access_valid_until=None, profile=PROFILE)
    await repo.save_schedule(user, sample_schedule())
    session.add(LessonRevision(user_id=1, lesson_uid="x", sequence=1, content_hash="h"))
    await session.flush()

    assert await repo.delete(1) is True

    assert await repo.get(1) is None
    assert (await session.execute(select(ScheduleSnapshot))).scalars().all() == []
    assert (await session.execute(select(LessonRevision))).scalars().all() == []
    assert await repo.delete(1) is False


async def test_list_active_excludes_disconnected(repo):
    active = await repo.connect(1, refresh_token="r", access_valid_until=None, profile=PROFILE)
    broken = await repo.connect(2, refresh_token="r", access_valid_until=None, profile=PROFILE)
    await repo.mark_failed(broken, error="dead", deactivate=True)
    await repo.get_or_create(3)

    assert [u.telegram_id for u in await repo.list_active()] == [active.telegram_id]
