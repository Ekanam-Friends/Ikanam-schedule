"""Счётчики активности: сколько событий какого вида случилось за час.

Здесь намеренно нет людей. Бот обещает в README не вести след о том, кто и
когда что нажимал, и статистика для владельца это обещание не нарушает:
в таблице только «в 14:00 по Москве было 7 вызовов /today». По таким числам
не восстановить ни одного человека, зато видно, живёт ли бот, в какие часы им
пользуются и не сломалась ли ночная синхронизация.

Виды событий — короткие строки: `cmd:today`, `sync:ok`, `sync:error`,
`sync:reauth`, `login:ok`, `feed`. Новый вид добавляется одной строкой в месте,
где событие происходит; таблица от этого не меняется.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityCounter

MOSCOW = timezone(timedelta(hours=3))
"""Слоты считаются по Москве: «в какие часы пользуются» имеет смысл только в
часовом поясе пользователей, а не сервера."""

COMMAND_PREFIX = "cmd:"
KIND_MAX_LENGTH = 32

_COMMAND_RE = re.compile(r"^/([a-z][a-z0-9_]{0,31})(?:@\w+)?(?:\s|$)", re.IGNORECASE)


def command_kind(text: str | None) -> str | None:
    """Вид события для текста сообщения, если это команда, иначе `None`.

    Логин и пароль, которые человек вводит в диалоге, — не команды и не
    считаются вовсе: даже число таких сообщений здесь ни к чему.
    """
    if not text:
        return None
    match = _COMMAND_RE.match(text.strip())
    if match is None:
        return None
    return f"{COMMAND_PREFIX}{match.group(1).lower()}"


class ActivityLog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, kind: str, *, when: datetime | None = None, amount: int = 1) -> None:
        """Прибавить событие к слоту «день, час, вид». Слот создаётся сам."""
        moment = (when or datetime.now(timezone.utc)).astimezone(MOSCOW)
        kind = kind[:KIND_MAX_LENGTH]
        table = ActivityCounter.__table__
        # Две синхронизации, закончившиеся в одну секунду, не должны спорить за
        # строку: upsert делает это одним запросом на стороне базы. Диалекты
        # различаются только модулем, из которого берётся `insert`.
        if self._session.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        statement = (
            insert(ActivityCounter)
            .values(day=moment.date(), hour=moment.hour, kind=kind, count=amount)
            .on_conflict_do_update(
                index_elements=["day", "hour", "kind"],
                set_={"count": table.c.count + amount},
            )
        )
        await self._session.execute(statement)

    async def by_day(self, *, since: date, until: date) -> dict[date, dict[str, int]]:
        """Сумма по видам за каждый день отрезка, включая границы."""
        rows = await self._session.execute(
            select(ActivityCounter.day, ActivityCounter.kind, func.sum(ActivityCounter.count))
            .where(ActivityCounter.day >= since, ActivityCounter.day <= until)
            .group_by(ActivityCounter.day, ActivityCounter.kind)
        )
        result: dict[date, dict[str, int]] = defaultdict(dict)
        for day, kind, total in rows.all():
            result[day][kind] = int(total)
        return dict(result)

    async def by_hour(self, *, since: date, until: date) -> dict[int, dict[str, int]]:
        """Сумма по видам за каждый час суток на отрезке — профиль дня."""
        rows = await self._session.execute(
            select(ActivityCounter.hour, ActivityCounter.kind, func.sum(ActivityCounter.count))
            .where(ActivityCounter.day >= since, ActivityCounter.day <= until)
            .group_by(ActivityCounter.hour, ActivityCounter.kind)
        )
        result: dict[int, dict[str, int]] = defaultdict(dict)
        for hour, kind, total in rows.all():
            result[int(hour)][kind] = int(total)
        return dict(result)
