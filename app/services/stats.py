"""Сводка для владельца: цифры по пользователям и картинка с активностью.

Всё строится из двух источников — таблицы пользователей (сколько подключено,
у кого ошибки) и счётчиков активности (что происходило по дням и часам).
Ни то ни другое не называет людей: сводка безопасна даже если её случайно
переслать.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db.activity import COMMAND_PREFIX, MOSCOW, ActivityLog
from app.db.models import ScheduleSnapshot, User
from app.db.repo import UserRepository

DEFAULT_DAYS = 14
"""Сколько дней показывать на графике по дням."""

HOUR_PROFILE_DAYS = 7
"""За сколько последних дней собирать профиль по часам: неделя сглаживает
случайности одного дня, но ещё отражает текущие привычки."""

# Группы событий для графика: имя → какие виды в неё входят. Команды не
# перечисляются — всё с префиксом `cmd:` попадает в первую группу.
SERIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Команды", ()),
    ("Синхронизации", ("sync:ok",)),
    ("Ошибки синхр.", ("sync:error", "sync:reauth")),
    ("Календари", ("feed",)),
    ("Подключения", ("login:ok",)),
)


@dataclass(slots=True)
class OwnerStats:
    generated_at: datetime
    users_total: int = 0
    users_connected: int = 0
    users_active: int = 0
    users_with_error: int = 0
    groups: int = 0
    connected_today: int = 0
    connected_week: int = 0
    snapshot_days: int = 0
    days: list[date] = field(default_factory=list)
    by_day: dict[date, dict[str, int]] = field(default_factory=dict)
    by_hour: dict[int, dict[str, int]] = field(default_factory=dict)

    def today(self) -> dict[str, int]:
        return self.by_day.get(self.days[-1], {}) if self.days else {}


def series_totals(counts: dict[str, int]) -> dict[str, int]:
    """Свернуть виды событий в группы для графика."""
    totals = {name: 0 for name, _ in SERIES}
    for kind, value in counts.items():
        if kind.startswith(COMMAND_PREFIX):
            totals["Команды"] += value
            continue
        for name, kinds in SERIES:
            if kind in kinds:
                totals[name] += value
                break
    return totals


async def collect(
    repo: UserRepository,
    activity: ActivityLog,
    *,
    now: datetime | None = None,
    days: int = DEFAULT_DAYS,
) -> OwnerStats:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(MOSCOW).date()
    since = today - timedelta(days=days - 1)
    session = repo.session

    connected = User.refresh_token_encrypted.is_not(None)
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(connected),
                func.count().filter(connected, User.is_active),
                func.count().filter(User.last_sync_error.is_not(None)),
                func.count(func.distinct(User.group_name)).filter(connected),
            )
        )
    ).one()
    # «Новых сегодня / за неделю» считаем в Python: SQLite хранит даты с
    # поясом строками и сравнивает их как строки, а пользователей мало.
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=MOSCOW)
    week_start = day_start - timedelta(days=6)
    connected_at = (
        await session.execute(select(User.connected_at).where(User.connected_at.is_not(None)))
    ).scalars()
    moments = [
        stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc) for stamp in connected_at
    ]
    recent = (
        sum(1 for stamp in moments if stamp >= day_start),
        sum(1 for stamp in moments if stamp >= week_start),
    )
    snapshot_days = (
        await session.execute(select(func.count()).select_from(ScheduleSnapshot))
    ).scalar_one()

    return OwnerStats(
        generated_at=now,
        users_total=row[0],
        users_connected=row[1],
        users_active=row[2],
        users_with_error=row[3],
        groups=row[4],
        connected_today=recent[0],
        connected_week=recent[1],
        snapshot_days=snapshot_days,
        days=[since + timedelta(days=offset) for offset in range(days)],
        by_day=await activity.by_day(since=since, until=today),
        by_hour=await activity.by_hour(
            since=today - timedelta(days=HOUR_PROFILE_DAYS - 1), until=today
        ),
    )


def format_summary(stats: OwnerStats) -> str:
    """Текст сводки для Telegram (HTML)."""
    today = series_totals(stats.today())
    commands_today = {
        kind[len(COMMAND_PREFIX) :]: value
        for kind, value in stats.today().items()
        if kind.startswith(COMMAND_PREFIX)
    }
    top = ", ".join(
        f"/{name} {value}"
        for name, value in sorted(commands_today.items(), key=lambda kv: -kv[1])[:5]
    )
    stamp = stats.generated_at.astimezone(MOSCOW)
    lines = [
        f"<b>Статистика на {stamp:%d.%m %H:%M}</b> (МСК)",
        "",
        f"Пользователей: <b>{stats.users_total}</b>, с кабинетом {stats.users_connected}, "
        f"активных {stats.users_active}, с ошибкой {stats.users_with_error}",
        f"Групп: {stats.groups}. Новых сегодня {stats.connected_today}, "
        f"за неделю {stats.connected_week}",
        f"Дней расписания в базе: {stats.snapshot_days}",
        "",
        f"<b>Сегодня</b>: команд {today['Команды']}, синхронизаций {today['Синхронизации']}, "
        f"ошибок {today['Ошибки синхр.']}, запросов календарей {today['Календари']}, "
        f"подключений {today['Подключения']}",
    ]
    if top:
        lines.append(f"Команды: {top}")
    return "\n".join(lines)


def render_chart(stats: OwnerStats) -> bytes:
    """Две панели: события по дням и профиль по часам. PNG."""
    # matplotlib импортируется здесь, а не наверху: он тяжёлый и нужен только
    # владельцу раз в день, остальному боту незачем платить за его загрузку.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(10, 8), dpi=100)
    fig.patch.set_facecolor("white")

    names = [name for name, _ in SERIES]
    per_day = [series_totals(stats.by_day.get(day, {})) for day in stats.days]
    positions = range(len(stats.days))
    bottoms = [0] * len(stats.days)
    for name in names:
        values = [totals[name] for totals in per_day]
        top.bar(positions, values, bottom=bottoms, label=name, width=0.7)
        bottoms = [b + v for b, v in zip(bottoms, values, strict=True)]
    top.set_xticks(list(positions))
    top.set_xticklabels([f"{day:%d.%m}" for day in stats.days], rotation=45, fontsize=8)
    top.set_title(f"События по дням, последние {len(stats.days)} дн.")
    top.legend(fontsize=8, ncol=len(names))
    top.grid(axis="y", alpha=0.3)

    hours = list(range(24))
    commands = [series_totals(stats.by_hour.get(hour, {}))["Команды"] for hour in hours]
    others = [
        sum(series_totals(stats.by_hour.get(hour, {})).values()) - commands[hour] for hour in hours
    ]
    bottom.bar(hours, commands, label="Команды", width=0.8)
    bottom.bar(hours, others, bottom=commands, label="Остальное", width=0.8)
    bottom.set_xticks(hours)
    bottom.set_xticklabels([f"{hour:02d}" for hour in hours], fontsize=8)
    bottom.set_title(f"По часам (МСК), последние {HOUR_PROFILE_DAYS} дн.")
    bottom.legend(fontsize=8)
    bottom.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    plt.close(fig)
    return buffer.getvalue()
