"""Проверки миграций.

Смысл один: обновление кода не должно требовать пересоздания базы. Значит,
миграции обязаны собирать схему с нуля, совпадать с моделями и переживать
повторный запуск.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.db.migrate import upgrade_to_head_sync
from app.db.models import Base
from app.db.session import create_schema, make_engine


def tables_of(path: Path) -> dict[str, set[str]]:
    engine = create_engine(f"sqlite:///{path}")
    try:
        inspector = inspect(engine)
        return {
            name: {c["name"] for c in inspector.get_columns(name)}
            for name in inspector.get_table_names()
            if name != "alembic_version"
        }
    finally:
        engine.dispose()


def test_migrations_build_the_same_schema_as_models(tmp_path):
    """Иначе тесты (create_all) и прод (alembic) живут с разными базами."""
    migrated = tmp_path / "migrated.db"
    upgrade_to_head_sync(f"sqlite+aiosqlite:///{migrated}")

    expected = {
        table.name: {c.name for c in table.columns} for table in Base.metadata.sorted_tables
    }
    assert tables_of(migrated) == expected


def test_upgrade_is_idempotent(tmp_path):
    db = tmp_path / "twice.db"
    upgrade_to_head_sync(f"sqlite+aiosqlite:///{db}")
    upgrade_to_head_sync(f"sqlite+aiosqlite:///{db}")

    assert "users" in tables_of(db)


async def test_database_created_before_migrations_is_adopted(tmp_path):
    """База от `create_all` получает отметку head, а не ошибку «таблица уже есть»."""
    db = tmp_path / "legacy.db"
    engine = make_engine(f"sqlite+aiosqlite:///{db}")
    await create_schema(engine)
    await engine.dispose()

    upgrade_to_head_sync(f"sqlite+aiosqlite:///{db}")

    engine = create_engine(f"sqlite:///{db}")
    try:
        assert "alembic_version" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
