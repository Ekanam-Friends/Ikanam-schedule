"""Подключение к базе.

Один движок на процесс, сессия — на единицу работы (обработка одного апдейта,
одна синхронизация). Таблицы для разработки создаются через `create_all`:
миграции Alembic появятся вместе с первым изменением схемы на проде, где уже
есть данные, которые нельзя терять. До этого они — церемония без пользы.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import Base


def make_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: после commit объекты остаются читаемыми без
    # повторного запроса — иначе каждый `user.group_name` после сохранения
    # превращался бы в скрытый SELECT, а в async-коде ещё и в ошибку.
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Сессия на одну единицу работы: commit при успехе, rollback при ошибке."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
