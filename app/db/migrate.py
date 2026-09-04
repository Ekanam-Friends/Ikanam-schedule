"""Применение миграций при старте приложения.

`alembic upgrade head` выполняется программно перед тем, как бот начнёт
принимать апдейты. Так обновление кода никогда не требует ни ручных команд,
ни пересоздания базы: новая колонка появляется в существующей таблице, а
данные пользователей остаются на месте.

Alembic — синхронный, а у нас event loop: миграции уходят в отдельный поток.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    # Логирование Alembic из ini перебивает настройки приложения — отключаем.
    config.attributes["configure_logger"] = False
    return config


def upgrade_to_head_sync(database_url: str) -> None:
    config = alembic_config(database_url)
    if _created_before_migrations(database_url):
        # База уже есть, а отметки Alembic нет: её создали через `create_all`
        # до появления миграций. Таблицы совпадают с первой ревизией, поэтому
        # ставим отметку, а не создаём их заново поверх существующих.
        log.info("База создана до миграций — помечаю как актуальную")
        command.stamp(config, "head")
        return
    command.upgrade(config, "head")


def _created_before_migrations(database_url: str) -> bool:
    """Есть ли уже таблицы приложения без отметки Alembic.

    Смотрим через тот же async-драйвер, что и приложение: синхронный драйвер
    для PostgreSQL (psycopg2) в зависимостях не нужен ради одной проверки.
    """
    from sqlalchemy import inspect

    async def probe() -> set[str]:
        from app.db.session import make_engine

        engine = make_engine(database_url)
        try:
            async with engine.connect() as connection:
                return set(await connection.run_sync(lambda c: inspect(c).get_table_names()))
        finally:
            await engine.dispose()

    try:
        tables = _run_blocking(probe())
    except Exception:  # noqa: BLE001 — базы может не быть вовсе; это не ошибка
        return False
    return "users" in tables and "alembic_version" not in tables


def _run_blocking(coroutine):
    """Выполнить корутину из синхронного кода — и с работающим циклом, и без."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    import threading

    result: list = []
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=runner, name="db-probe")
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


async def upgrade_to_head(database_url: str) -> None:
    log.info("Применяю миграции базы")
    await asyncio.to_thread(upgrade_to_head_sync, database_url)
    log.info("Схема базы актуальна")
