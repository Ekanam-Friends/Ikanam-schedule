"""`/settings`: утренняя сводка, напоминания в календаре, пуши, ссылка.

Всё — кнопками, без ввода текста: настройки нажимают на ходу с телефона, и
диалог «введите время в формате ЧЧ:ММ» там раздражает. Каждое нажатие сразу
сохраняется и перерисовывает то же сообщение — видно текущее состояние.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.texts import NOT_CONNECTED_HINT
from app.core.config import Settings
from app.db.models import User
from app.db.repo import UserRepository

router = Router(name="settings")

DIGEST_OPTIONS: tuple[str | None, ...] = ("07:00", "07:30", "08:00", "08:30", None)
REMINDER_OPTIONS: tuple[int | None, ...] = (None, 10, 15, 30)


def render(user: User) -> tuple[str, InlineKeyboardMarkup]:
    digest = user.morning_digest_at or "выключена"
    reminder = f"за {user.reminder_minutes} мин" if user.reminder_minutes else "нет"
    changes = "включены" if user.notify_on_change else "выключены"

    text = (
        "<b>Настройки</b>\n\n"
        f"Утренняя сводка: <b>{digest}</b>\n"
        f"Напоминание о паре в календаре: <b>{reminder}</b>\n"
        f"Сообщения об изменениях: <b>{changes}</b>\n\n"
        "Напоминания работают в файле .ics и в Apple Calendar; Google Calendar "
        "в подписках их игнорирует — там время напоминания задаётся в настройках календаря."
    )

    builder = InlineKeyboardBuilder()
    for option in DIGEST_OPTIONS:
        label = option or "выкл"
        mark = "• " if option == user.morning_digest_at else ""
        builder.button(text=f"{mark}{label}", callback_data=f"settings:digest:{option or 'off'}")
    for option in REMINDER_OPTIONS:
        label = f"{option} мин" if option else "без"
        mark = "• " if option == user.reminder_minutes else ""
        builder.button(text=f"{mark}{label}", callback_data=f"settings:reminder:{option or 0}")
    builder.button(
        text="🔔 Изменения: выключить" if user.notify_on_change else "🔕 Изменения: включить",
        callback_data="settings:changes:toggle",
    )
    builder.button(text="🔗 Перевыпустить ссылку на календарь", callback_data="settings:rotate")
    builder.adjust(len(DIGEST_OPTIONS), len(REMINDER_OPTIONS), 1, 1)
    return text, builder.as_markup()


@router.message(Command("settings"))
async def on_settings(message: Message, repo: UserRepository) -> None:
    user = await repo.get(message.from_user.id)
    if user is None or not user.is_connected:
        await message.answer(NOT_CONNECTED_HINT)
        return
    text, markup = render(user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("settings:"))
async def on_change(query: CallbackQuery, repo: UserRepository, settings: Settings) -> None:
    user = await repo.get(query.from_user.id)
    if user is None or not user.is_connected:
        await query.answer("Сначала подключите кабинет: /login", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""

    if action == "digest":
        user.morning_digest_at = None if value == "off" else value
        note = "Сводка выключена" if value == "off" else f"Сводка в {value}"
    elif action == "reminder":
        minutes = int(value) if value.isdigit() else 0
        user.reminder_minutes = minutes or None
        note = f"Напоминание за {minutes} мин" if minutes else "Без напоминаний"
    elif action == "changes" and value == "toggle":
        user.notify_on_change = not user.notify_on_change
        note = "Сообщения об изменениях " + ("включены" if user.notify_on_change else "выключены")
    elif action == "rotate":
        # Старая ссылка умирает в этот же момент: календарь, подписанный на
        # неё, начнёт получать 404, и его нужно переподписать. Об этом говорим.
        token = await repo.rotate_feed_token(user)
        await query.answer("Ссылка перевыпущена")
        await query.message.answer(
            "Новая ссылка на подписку — старая больше не работает, "
            "переподпишите календарь:\n"
            f"<code>{settings.webcal_url(token)}</code>\n\n"
            "Или через /calendar."
        )
        return
    else:
        await query.answer()
        return

    await query.answer(note)
    text, markup = render(user)
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001 — «message is not modified» при повторном нажатии
        pass
