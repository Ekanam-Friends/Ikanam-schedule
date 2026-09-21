"""`/broadcast` — сообщение владельца всем пользователям.

* `/broadcast all_text <текст>` — просто сообщение;
* `/broadcast all_answer <вопрос>` — с кнопкой «Ответить»;
* `/broadcast_answers` — все накопленные ответы одним CSV-файлом. Выгруженные
  ответы из базы удаляются: файл у владельца — единственная копия.

Сначала бот показывает черновик и число получателей и ждёт «Отправить»: одна
опечатка в команде не должна уходить сразу всем. Как и `/stats`, для всех,
кроме владельца, команд не существует.

Ответ пользователя приходит владельцу не отдельным сообщением, а строкой в
CSV — чтобы чат владельца с ботом не превращался в ленту чужих ответов.
"""

from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.handlers.stats import is_owner
from app.core.config import Settings
from app.db.broadcasts import KIND_ANSWER, KIND_TEXT, BroadcastStore
from app.db.repo import UserRepository
from app.services.broadcast import answers_csv, deliver

log = logging.getLogger(__name__)

router = Router(name="broadcast")

MODES = {"all_text": KIND_TEXT, "all_answer": KIND_ANSWER}
MAX_LENGTH = 3900
"""Лимит Telegram — 4096 символов; запас на заголовок черновика."""

USAGE = (
    "<b>Рассылка всем пользователям</b>\n\n"
    "<code>/broadcast all_text текст</code> — просто сообщение\n"
    "<code>/broadcast all_answer вопрос</code> — с кнопкой «Ответить»\n"
    "<code>/broadcast_answers</code> — ответы одним CSV (после выгрузки удаляются)\n\n"
    "Перед отправкой покажу черновик и число получателей."
)

REPLY_PROMPT = "Напишите ответ одним сообщением — я передам его автору бота. Передумали — /cancel"
REPLY_THANKS = "Спасибо! Ответ передан."
REPLY_CANCELLED = "Хорошо, ответ не отправляю."
REPLY_CLOSED = "На это сообщение уже нельзя ответить"

_running: set[asyncio.Task] = set()
"""Ссылки на идущие рассылки: без них сборщик мусора может снять задачу на полпути."""


class Reply(StatesGroup):
    waiting = State()


def reply_markup(broadcast_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Ответить", callback_data=f"bcast:reply:{broadcast_id}")
    return builder.as_markup()


def _id_from(data: str | None) -> int | None:
    try:
        return int((data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


# --- Владелец ---


@router.message(Command("broadcast"), is_owner)
async def on_broadcast(message: Message, command: CommandObject, repo: UserRepository) -> None:
    parts = (command.args or "").strip().split(maxsplit=1)
    kind = MODES.get(parts[0]) if parts else None
    if kind is None or len(parts) < 2:
        await message.answer(USAGE)
        return
    text = parts[1]
    if len(text) > MAX_LENGTH:
        await message.answer(f"Слишком длинно: {len(text)} символов, можно до {MAX_LENGTH}.")
        return

    store = BroadcastStore(repo.session)
    broadcast = await store.create(kind, text)
    recipients = len(await store.recipient_ids())
    title = "с кнопкой «Ответить»" if kind == KIND_ANSWER else "просто текст"

    builder = InlineKeyboardBuilder()
    builder.button(text=f"📨 Отправить ({recipients})", callback_data=f"bcast:send:{broadcast.id}")
    builder.button(text="Отмена", callback_data=f"bcast:drop:{broadcast.id}")
    await message.answer(
        f"<b>Черновик рассылки: {title}</b>\nПолучат: {recipients}\n\n{html.escape(text)}",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("bcast:send:"), is_owner)
async def on_send(query: CallbackQuery, repo: UserRepository, settings: Settings, bot: Bot) -> None:
    store = BroadcastStore(repo.session)
    broadcast_id = _id_from(query.data) or 0
    if not await store.claim_for_sending(broadcast_id):
        await query.answer("Уже отправлено или отменено", show_alert=True)
        return
    broadcast = await store.get(broadcast_id)
    recipients = await store.recipient_ids()
    # Фиксируем отметку до первой доставки: кнопка «Ответить» у получателя
    # проверяет, что рассылка отправлена.
    await repo.session.commit()
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(f"Отправляю {len(recipients)} получателям…")

    markup = reply_markup(broadcast.id) if broadcast.kind == KIND_ANSWER else None
    task = asyncio.create_task(
        _deliver_and_report(bot, settings.owner_chat_id, recipients, broadcast.text, markup),
        name=f"broadcast-{broadcast.id}",
    )
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _deliver_and_report(
    bot: Bot,
    owner: int | None,
    recipients: list[int],
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> None:
    try:
        report = await deliver(bot, recipients, text, reply_markup=markup)
    except Exception:
        log.exception("Рассылка упала")
        if owner is not None:
            await bot.send_message(owner, "Рассылка прервалась с ошибкой, подробности в логе.")
        return
    if owner is not None:
        await bot.send_message(owner, f"<b>Рассылка завершена</b>\n{report.summary()}")


@router.callback_query(F.data.startswith("bcast:drop:"), is_owner)
async def on_drop(query: CallbackQuery, repo: UserRepository) -> None:
    store = BroadcastStore(repo.session)
    broadcast = await store.get(_id_from(query.data) or 0)
    if broadcast is not None and broadcast.sent_at is None:
        await store.drop(broadcast)
    await query.answer("Отменено")
    await query.message.edit_reply_markup(reply_markup=None)


@router.message(Command("broadcast_answers"), is_owner)
async def on_answers(message: Message, repo: UserRepository) -> None:
    store = BroadcastStore(repo.session)
    rows = await store.pending_answers()
    if not rows:
        await message.answer("Новых ответов нет.")
        return
    await message.answer_document(
        BufferedInputFile(answers_csv(rows), filename="answers.csv"),
        caption=f"Ответов: {len(rows)}. Из базы они удалены.",
    )
    # Удаляем только после того, как файл дошёл: если Telegram не принял
    # документ, исключение прервёт обработчик раньше и ответы останутся.
    await store.delete_answers([row.id for row in rows])


# --- Пользователь ---


@router.callback_query(F.data.startswith("bcast:reply:"))
async def on_reply_pressed(query: CallbackQuery, state: FSMContext, repo: UserRepository) -> None:
    broadcast = await BroadcastStore(repo.session).get(_id_from(query.data) or 0)
    if broadcast is None or broadcast.kind != KIND_ANSWER or broadcast.sent_at is None:
        await query.answer(REPLY_CLOSED, show_alert=True)
        return
    await state.set_state(Reply.waiting)
    await state.update_data(broadcast_id=broadcast.id)
    await query.answer()
    await query.message.answer(REPLY_PROMPT)


@router.message(Reply.waiting, Command("cancel"))
@router.message(Reply.waiting, F.text.casefold() == "отмена")
async def on_reply_cancelled(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(REPLY_CANCELLED)


@router.message(Reply.waiting, F.text.startswith("/"))
async def on_other_command(message: Message, state: FSMContext) -> None:
    # Человек передумал отвечать и набрал команду: выходим из ожидания ответа
    # и отдаём команду её обработчику, а не записываем «/today» в ответы.
    await state.clear()
    raise SkipHandler


@router.message(Reply.waiting, F.text)
async def on_reply(message: Message, state: FSMContext, repo: UserRepository) -> None:
    data = await state.get_data()
    await state.clear()
    broadcast_id = data.get("broadcast_id")
    store = BroadcastStore(repo.session)
    if broadcast_id is None or await store.get(broadcast_id) is None:
        await message.answer(REPLY_CLOSED)
        return
    await store.add_answer(broadcast_id, message.from_user.id, message.text)
    await message.answer(REPLY_THANKS)
