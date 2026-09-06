"""Зависимости для обработчиков: сессия базы, репозиторий, сервисы.

Middleware открывает сессию на один апдейт и кладёт готовые объекты в
`data`, откуда aiogram передаёт их обработчикам по имени аргумента. Обработчик
не знает ни про движок, ни про ключ шифрования — только про `repo` и `account`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.crypto import CredentialsCipher
from app.db.repo import UserRepository
from app.db.session import session_scope
from app.ranepa.client import RanepaClient
from app.services.account import AccountService
from app.services.sync import ScheduleSyncService


@dataclass(frozen=True, slots=True)
class AppContext:
    settings: Settings
    cipher: CredentialsCipher
    session_factory: async_sessionmaker[AsyncSession]
    client_factory: Callable[..., RanepaClient] = RanepaClient
    """Как создавать клиент кабинета. В продакшне сюда приходит фабрика с общим
    решателем JS-проверки; в тестах и без проверки — сам класс клиента."""


class DependenciesMiddleware(BaseMiddleware):
    def __init__(self, context: AppContext) -> None:
        self._context = context

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with session_scope(self._context.session_factory) as session:
            repo = UserRepository(session, self._context.cipher)
            factory = self._context.client_factory
            sync = ScheduleSyncService(repo, client_factory=factory)
            data["settings"] = self._context.settings
            data["repo"] = repo
            data["sync"] = sync
            data["account"] = AccountService(repo, sync, client_factory=factory)
            return await handler(event, data)
