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
    from sqlalchemy import create_engine, inspect

    sync_url = database_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg2")
    try:
        engine = create_engine(sync_url)
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
    except Exception:  # noqa: BLE001 — базы может не быть вовсе; это не ошибка
        return False
    finally:
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001
            pass
    return "users" in tables and "alembic_version" not in tables


async def upgrade_to_head(database_url: str) -> None:
    log.info("Применяю миграции базы")
    await asyncio.to_thread(upgrade_to_head_sync, database_url)
    log.info("Схема базы актуальна")
