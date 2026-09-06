"""`/calendar`: расписание в календарь телефона.

Два способа, и оба выдаются сразу:

* **подписка по ссылке** — календарь сам перечитывает её и подхватывает
  переносы. Для iPhone ссылка со схемой `webcal://`: по ней iOS сразу
  предлагает подписаться, тогда как `https://` он просто скачает как файл, и
  человек получит разовый импорт, не заметив разницы;
* **файл `.ics`** — для тех, кому нужно «прямо сейчас и без настройки».

Ссылка содержит секрет и равна доступу к расписанию, поэтому здесь же —
как её перевыпустить.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.texts import NOT_CONNECTED_HINT
from app.calendar.ics import build_calendar
from app.core.config import Settings
from app.db.models import User
from app.db.repo import UserRepository

router = Router(name="calendar")

MOSCOW = timezone(timedelta(hours=3))
FILE_HORIZON_DAYS = 21


def build_calendar_message(user: User, settings: Settings) -> str:
    webcal = settings.webcal_url(user.feed_token)
    https = settings.feed_url(user.feed_token)
    return (
        "<b>Подписка</b> — календарь обновляется сам.\n\n"
        f"iPhone / Mac — просто откройте ссылку:\n<code>{webcal}</code>\n\n"
        "Google Calendar — на компьютере: «Другие календари» → «+» → "
        f"«Добавить по URL» и вставьте:\n<code>{https}</code>\n"
        "На Android подписка появится сама, если добавить её в аккаунт Google.\n\n"
        "Ссылка личная: у любого, кто её получит, будет ваше расписание. "
        "Если она утекла — нажмите «Новая ссылка», старая перестанет работать.\n\n"
        "<b>Файл</b> — разовый импорт на три недели вперёд, обновляться не будет."
    )


def keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="📎 Файл .ics", callback_data="calendar:file")
    builder.button(text="🔁 Новая ссылка", callback_data="calendar:rotate")
    builder.adjust(2)
    return builder


async def _connected_user(message: Message, repo: UserRepository) -> User | None:
    user = await repo.get(message.from_user.id)
    if user is None or not user.is_connected:
        await message.answer(NOT_CONNECTED_HINT)
        return None
    return user


@router.message(Command("calendar"))
async def on_calendar(message: Message, repo: UserRepository, settings: Settings) -> None:
    user = await _connected_user(message, repo)
    if user is None:
        return
    await message.answer(
        build_calendar_message(user, settings),
        reply_markup=keyboard().as_markup(),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "calendar:file")
async def on_calendar_file(query: CallbackQuery, repo: UserRepository) -> None:
    user = await repo.get(query.from_user.id)
    if user is None or not user.is_connected:
        await query.answer("Сначала подключите кабинет: /login", show_alert=True)
        return

    today = datetime.now(MOSCOW).date()
    schedule = await repo.load_schedule(
        user, since=today, until=today + timedelta(days=FILE_HORIZON_DAYS - 1)
    )
    if not schedule.lessons:
        await query.answer("Пар на ближайшие недели нет — файл был бы пустым.", show_alert=True)
        return

    # Ревизии и в файле тоже: если человек импортирует его повторно, календарь
    # обновит уже известные события, а не создаст дубликаты.
    body = build_calendar(
        schedule,
        reminder_minutes=user.reminder_minutes,
        sequences=await repo.sequences_for(user),
    )
    await query.message.answer_document(
        BufferedInputFile(body, filename=f"ranepa-{today.isoformat()}.ics"),
        caption=(
            f"Расписание с {_ru(today)} на три недели. "
            "Откройте файл — телефон предложит добавить события в календарь."
        ),
    )
    await query.answer()


@router.callback_query(F.data == "calendar:rotate")
async def on_calendar_rotate(
    query: CallbackQuery, repo: UserRepository, settings: Settings
) -> None:
    user = await repo.get(query.from_user.id)
    if user is None or not user.is_connected:
        await query.answer("Сначала подключите кабинет: /login", show_alert=True)
        return

    await repo.rotate_feed_token(user)
    await query.message.answer(
        "Готово: старая ссылка больше не работает, подпишитесь заново по новой.\n\n"
        + build_calendar_message(user, settings),
        reply_markup=keyboard().as_markup(),
        disable_web_page_preview=True,
    )
    await query.answer("Ссылка перевыпущена")


def _ru(day: date) -> str:
    from app.bot.formatting import format_date

    return format_date(day)
