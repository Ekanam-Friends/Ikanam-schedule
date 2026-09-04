"""FSM-хранилище aiogram поверх нашей базы.

aiogram из коробки предлагает память процесса и Redis. Память теряет диалоги
на каждом перезапуске, Redis — ещё один сервис ради нескольких строк на
пользователя. База у нас уже есть, и таблица `dialog_states` в ней решает
задачу без новых зависимостей.
"""

from __future__ import annotations

from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DialogState


def _key(key: StorageKey) -> str:
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}"


class SQLAlchemyStorage(BaseStorage):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        async with self._factory() as session:
            row = await session.get(DialogState, _key(key))
            if row is None:
                if value is None:
                    return
                row = DialogState(key=_key(key), data={})
                session.add(row)
            row.state = value
            await session.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._factory() as session:
            row = await session.get(DialogState, _key(key))
            return row.state if row else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        async with self._factory() as session:
            row = await session.get(DialogState, _key(key))
            if row is None:
                row = DialogState(key=_key(key), state=None)
                session.add(row)
            row.data = dict(data)
            await session.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._factory() as session:
            row = await session.get(DialogState, _key(key))
            return dict(row.data or {}) if row else {}

    async def close(self) -> None:
        return None
