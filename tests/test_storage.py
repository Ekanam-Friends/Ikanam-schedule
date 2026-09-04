"""Проверки FSM-хранилища в базе.

Главное свойство — состояние переживает «перезапуск»: новый экземпляр
хранилища над той же базой видит то, что записал старый.
"""

from __future__ import annotations

import pytest_asyncio
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey

from app.bot.storage import SQLAlchemyStorage
from app.db.session import create_schema, make_engine, make_session_factory


class Login(StatesGroup):
    waiting_password = State()


KEY = StorageKey(bot_id=1, chat_id=42, user_id=42)
OTHER = StorageKey(bot_id=1, chat_id=43, user_id=43)


@pytest_asyncio.fixture
async def factory():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    yield make_session_factory(engine)
    await engine.dispose()


async def test_state_survives_a_restart(factory):
    """Новый экземпляр над той же базой — как бот после рестарта."""
    before = SQLAlchemyStorage(factory)
    await before.set_state(KEY, Login.waiting_password)
    await before.set_data(KEY, {"login": "student"})

    after = SQLAlchemyStorage(factory)

    assert await after.get_state(KEY) == Login.waiting_password.state
    assert await after.get_data(KEY) == {"login": "student"}


async def test_empty_by_default(factory):
    storage = SQLAlchemyStorage(factory)

    assert await storage.get_state(KEY) is None
    assert await storage.get_data(KEY) == {}


async def test_clearing_state(factory):
    storage = SQLAlchemyStorage(factory)
    await storage.set_state(KEY, Login.waiting_password)

    await storage.set_state(KEY, None)
    await storage.set_data(KEY, {})

    assert await storage.get_state(KEY) is None
    assert await storage.get_data(KEY) == {}


async def test_users_do_not_share_state(factory):
    storage = SQLAlchemyStorage(factory)
    await storage.set_state(KEY, Login.waiting_password)

    assert await storage.get_state(OTHER) is None
