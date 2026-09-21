"""Рассылка владельца: доставка, ответы, выгрузка в CSV."""

from __future__ import annotations

import csv
import io
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from app.bot.handlers import broadcast as handlers
from app.db.broadcasts import KIND_ANSWER, KIND_TEXT, AnswerRow, BroadcastStore
from app.db.models import User
from app.db.session import create_schema, make_engine, make_session_factory
from app.services.broadcast import answers_csv, deliver

METHOD = SendMessage(chat_id=0, text="x")


@pytest_asyncio.fixture
async def session():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


class FakeBot:
    def __init__(self, failures: dict[int, list[Exception]] | None = None) -> None:
        self.failures = failures or {}
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        queue = self.failures.get(chat_id)
        if queue:
            raise queue.pop(0)
        self.sent.append((chat_id, text, reply_markup))


class FakeState:
    def __init__(self, data: dict | None = None) -> None:
        self.state = None
        self.data = data or {}

    async def get_data(self):
        return self.data

    async def clear(self):
        self.state, self.data = None, {}


class FakeMessage:
    def __init__(self, text: str, user_id: int = 7) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.replies: list[str] = []

    async def answer(self, text, **_):
        self.replies.append(text)


# --- Хранилище ---


async def test_draft_is_sent_only_once(session):
    store = BroadcastStore(session)
    draft = await store.create(KIND_TEXT, "Привет")

    assert await store.claim_for_sending(draft.id)
    assert not await store.claim_for_sending(draft.id)
    assert not await store.claim_for_sending(draft.id + 100)


async def test_recipients_are_everyone_in_the_base(session):
    session.add_all(
        [
            User(telegram_id=2, feed_token="a"),
            User(telegram_id=1, feed_token="b", is_active=False),
        ]
    )
    await session.flush()

    assert await BroadcastStore(session).recipient_ids() == [1, 2]


async def test_answers_are_exported_with_question_and_then_deleted(session):
    store = BroadcastStore(session)
    poll = await store.create(KIND_ANSWER, "Как бот?")
    await store.add_answer(poll.id, 7, "Отлично")
    await store.add_answer(poll.id, 8, "Нужна тёмная тема")

    rows = await store.pending_answers()
    assert [(r.user_id, r.question, r.answer) for r in rows] == [
        (7, "Как бот?", "Отлично"),
        (8, "Как бот?", "Нужна тёмная тема"),
    ]

    await store.delete_answers([rows[0].id])
    assert [r.user_id for r in await store.pending_answers()] == [8]


# --- Доставка ---


async def test_delivery_counts_blocked_and_retries_flood_limit():
    bot = FakeBot(
        {
            2: [TelegramForbiddenError(METHOD, "blocked")],
            3: [TelegramRetryAfter(METHOD, "flood", retry_after=0)],
        }
    )

    report = await deliver(bot, [1, 2, 3], "<3 привет", pause=0)

    assert (report.total, report.delivered, report.blocked, report.failed) == (3, 2, 1, 0)
    assert [chat for chat, *_ in bot.sent] == [1, 3]
    assert bot.sent[0][1] == "<3 привет"  # текст владельца не трогаем


# --- CSV ---


def test_csv_opens_in_russian_excel():
    data = answers_csv([AnswerRow(1, 7, "Вопрос; с точкой", 'Ответ\n"в кавычках"')])

    assert data.startswith("﻿".encode())
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))
    assert rows == [
        ["ID пользователя", "Вопрос", "Ответ"],
        ["7", "Вопрос; с точкой", 'Ответ\n"в кавычках"'],
    ]


# --- Ответ пользователя ---


async def test_reply_is_saved_and_state_cleared(session):
    store = BroadcastStore(session)
    poll = await store.create(KIND_ANSWER, "Как бот?")
    state = FakeState({"broadcast_id": poll.id})
    message = FakeMessage("Всё супер")

    await handlers.on_reply(message, state, SimpleNamespace(session=session))

    assert state.data == {}
    assert message.replies == [handlers.REPLY_THANKS]
    assert [r.answer for r in await store.pending_answers()] == ["Всё супер"]


async def test_command_while_replying_is_not_taken_as_answer():
    from aiogram.dispatcher.event.bases import SkipHandler

    state = FakeState({"broadcast_id": 1})
    with pytest.raises(SkipHandler):
        await handlers.on_other_command(FakeMessage("/today"), state)
    assert state.data == {}
