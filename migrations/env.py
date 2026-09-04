"""Окружение Alembic.

Адрес базы берётся из настроек приложения (`DATABASE_URL`), а не из
`alembic.ini`: секреты в ini-файле — это секреты в репозитории.

`render_as_batch=True` нужен ради SQLite: он не умеет менять колонки через
`ALTER TABLE`, и Alembic в этом режиме пересоздаёт таблицу целиком. Для
PostgreSQL режим безвреден — там всё делается обычными `ALTER`.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.db.models import Base

config = context.config

# Из приложения логирование уже настроено; из консоли — берём из ini.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        # Прямой запуск `alembic` из консоли без окружения — читаем .env,
        # как это делает само приложение.
        from app.core.config import get_settings

        url = get_settings().database_url
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run_async_migrations())
        return

    # Нас вызвали из работающего event loop (например, из async-теста):
    # `asyncio.run` здесь запрещён, поэтому миграции уходят в отдельный поток
    # со своим циклом.
    import threading

    errors: list[BaseException] = []

    def runner() -> None:
        try:
            asyncio.run(run_async_migrations())
        except BaseException as exc:  # noqa: BLE001 — пробрасываем наверх как есть
            errors.append(exc)

    thread = threading.Thread(target=runner, name="alembic-migrations")
    thread.start()
    thread.join()
    if errors:
        raise errors[0]


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
