"""Ревизии пар и путь изменений через синхронизацию.

Проверяется связка целиком: вторая выгрузка с отличиями → диффер → снапшот
с отменённой парой → выросший `SEQUENCE` → фид отдаёт всё это календарю.
"""

from __future__ import annotations

import base64
import copy
import json
import os
from datetime import date
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.calendar.ics import build_calendar
from app.core.crypto import CredentialsCipher
from app.db.repo import UserRepository
from app.db.session import create_schema, make_engine, make_session_factory
from app.ranepa.client import RanepaClient
from app.services.account import AccountService
from app.services.diff import ChangeKind
from app.services.sync import ScheduleSyncService

FIXTURES = Path(__file__).parent / "fixtures"
SCHEDULE = json.loads((FIXTURES / "schedule_response.json").read_text(encoding="utf-8"))
GROUPS = json.loads((FIXTURES / "student_groups.json").read_text(encoding="utf-8"))
TOKENS = {"access_token": "acc", "refresh_token": "ref", "exp": 1788600000000}

TODAY = date(2026, 9, 1)  # фикстура содержит 1, 5 и 23 сентября
HORIZON = 30


class Cabinet:
    def __init__(self) -> None:
        self.schedule = copy.deepcopy(SCHEDULE)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("auth/login") or path.endswith("auth/refresh"):
            return httpx.Response(200, json=TOKENS)
        if path.endswith("student-groups"):
            return httpx.Response(200, json=GROUPS)
        if path.endswith("schedule"):
            return httpx.Response(200, json=self.schedule)
        return httpx.Response(404)

    def factory(self, **kwargs) -> RanepaClient:
        return RanepaClient(transport=httpx.MockTransport(self.handler), **kwargs)


@pytest_asyncio.fixture
async def env():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = UserRepository(session, CredentialsCipher(base64.urlsafe_b64encode(os.urandom(32)).decode()))
        cabinet = Cabinet()
        sync = ScheduleSyncService(repo, client_factory=cabinet.factory)
        account = AccountService(repo, sync, client_factory=cabinet.factory)
        await account.connect(1, "l", "p")
        yield repo, sync, cabinet
    await engine.dispose()


async def resync(repo, sync):
    return await sync.sync_user(await repo.get(1), today=TODAY, horizon_days=HORIZON)


def sept23(cabinet) -> dict:
    return next(d for d in cabinet.schedule["days"] if d["date"].startswith("2026-09-23"))


async def test_first_sync_creates_revisions_at_zero(env):
    repo, sync, _ = env
    user = await repo.get(1)

    sequences = await repo.sequences_for(user)

    assert len(sequences) == 3
    assert set(sequences.values()) == {0}


async def test_unchanged_resync_reports_nothing_and_keeps_sequences(env):
    repo, sync, _ = env

    result = await resync(repo, sync)

    assert result.changes == []
    assert set((await repo.sequences_for(await repo.get(1))).values()) == {0}


async def test_room_change_bumps_sequence_and_is_reported(env):
    repo, sync, cabinet = env
    sept23(cabinet)["discs"][0]["room"] = "1 - 3406 (26) П+ПК Блок 3G (Вернадского, 84 - корпус 1)"

    result = await resync(repo, sync)

    assert [c.kind for c in result.changes] == [ChangeKind.CHANGED]
    user = await repo.get(1)
    sequences = await repo.sequences_for(user)
    assert sorted(sequences.values()) == [0, 0, 1]


async def test_vanished_lesson_is_kept_cancelled_and_feed_marks_it(env):
    repo, sync, cabinet = env
    removed = sept23(cabinet)["discs"].pop(1)  # пара в 15:40

    result = await resync(repo, sync)

    assert [c.kind for c in result.changes] == [ChangeKind.CANCELLED]
    assert result.lessons_count == 2

    user = await repo.get(1)
    stored = await repo.load_schedule(user, since=date(2026, 9, 23), until=date(2026, 9, 23))
    cancelled = [l for l in stored.lessons if l.cancelled]
    assert len(cancelled) == 1
    assert cancelled[0].start.strftime("%H:%M") == removed["start_date"][11:16]

    raw = build_calendar(stored, sequences=await repo.sequences_for(user)).decode("utf-8")
    assert "STATUS:CANCELLED" in raw
    assert raw.count("BEGIN:VEVENT") == 2


async def test_cancelled_then_restored(env):
    repo, sync, cabinet = env
    removed = sept23(cabinet)["discs"].pop(1)
    await resync(repo, sync)

    sept23(cabinet)["discs"].append(removed)
    result = await resync(repo, sync)

    assert [c.kind for c in result.changes] == [ChangeKind.RESTORED]
    user = await repo.get(1)
    stored = await repo.load_schedule(user, since=date(2026, 9, 23), until=date(2026, 9, 23))
    assert not any(l.cancelled for l in stored.lessons)
    # Отмена и восстановление — две правки одного события.
    uid = next(l for l in stored.lessons if l.start.strftime("%H:%M") == removed["start_date"][11:16]).uid
    assert (await repo.sequences_for(user))[uid] == 2


async def test_logout_removes_revisions_too(env):
    repo, sync, _ = env

    await repo.delete(1)

    assert await repo.get(1) is None
