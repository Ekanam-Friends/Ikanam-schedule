"""`/today`, `/tomorrow`, `/week`, `/status`.

Показывается то, что лежит в снапшотах. В кабинет по этим командам бот не
ходит: ответ мгновенный, кабинет не нагружается сотней людей, открывших бота
в 8:55, а если кабинет лежит — расписание всё равно есть. Свежесть данных
видна в `/status`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.formatting import format_day, format_missing_day, format_week
from app.bot.texts import NOT_CONNECTED_HINT
from app.db.models import User
from app.db.repo import UserRepository

router = Router(name="schedule")

MOSCOW = timezone(timedelta(hours=3))


def today_msk() -> date:
    return datetime.now(MOSCOW).date()


async def _connected_user(message: Message, repo: UserRepository) -> User | None:
    user = await repo.get(message.from_user.id)
    if user is None or not user.is_connected:
        await message.answer(NOT_CONNECTED_HINT)
        return None
    return user


@router.message(Command("today"))
async def on_today(message: Message, repo: UserRepository) -> None:
    await _send_day(message, repo, today_msk(), title="Сегодня")


@router.message(Command("tomorrow"))
async def on_tomorrow(message: Message, repo: UserRepository) -> None:
    await _send_day(message, repo, today_msk() + timedelta(days=1), title="Завтра")


async def _send_day(message: Message, repo: UserRepository, day: date, *, title: str) -> None:
    user = await _connected_user(message, repo)
    if user is None:
        return
    schedule = await repo.load_schedule(user, since=day, until=day)
    found = schedule.day_for(day)
    text = format_day(found, title=f"{title}, {_date_label(day)}") if found else (
        format_missing_day(day, title=f"{title}, {_date_label(day)}")
    )
    await message.answer(text)


@router.message(Command("week"))
async def on_week(message: Message, repo: UserRepository) -> None:
    user = await _connected_user(message, repo)
    if user is None:
        return
    start = today_msk()
    schedule = await repo.load_schedule(user, since=start, until=start + timedelta(days=6))
    await message.answer(format_week(schedule, since=start))


@router.message(Command("status"))
async def on_status(message: Message, repo: UserRepository) -> None:
    user = await repo.get(message.from_user.id)
    if user is None or not user.is_connected:
        if user is not None and not user.is_active and user.last_sync_error:
            await message.answer(
                "Кабинет перестал принимать доступ бота. Подключите его заново: /login"
            )
            return
        await message.answer(NOT_CONNECTED_HINT)
        return

    lines = [f"Группа: <b>{user.group_name or '—'}</b>"]
    if user.last_sync_at:
        stamp = user.last_sync_at.astimezone(MOSCOW)
        lines.append(f"Расписание обновлено: {stamp:%d.%m %H:%M} (МСК)")
    else:
        lines.append("Расписание ещё ни разу не загружалось.")
    if user.last_sync_error:
        lines.append(f"Последняя ошибка: {user.last_sync_error}")
    await message.answer("\n".join(lines))


def _date_label(day: date) -> str:
    from app.bot.formatting import format_date

    return format_date(day)
