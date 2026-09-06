"""`/donate`: поддержать сервер через Telegram Stars.

Stars выбраны не из любви к Telegram, а потому что это единственный способ
принять деньги внутри бота без юрлица, эквайринга и внешних ссылок: человек
платит из того же чата, а Stars потом выводятся владельцем бота. Никаких
номеров карт бот не видит и не хранит.

Суммы фиксированные и небольшие — это «скинуться на сервер», а не магазин.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

log = logging.getLogger(__name__)

router = Router(name="donate")

AMOUNTS = (25, 50, 100, 250)
"""Варианты в Stars. Один Star — примерно 1,5–2 ₽; 100 Stars покрывают
примерно месяц сервера."""

DONATE_TEXT = (
    "Бот живёт на арендованном сервере, и платит за него команда.\n"
    "Если бот экономит вам время — можно скинуться на сервер через Telegram Stars. "
    "Это необязательно и ни на что в боте не влияет.\n\n"
    "Выберите сумму:"
)


@router.message(Command("donate"))
async def on_donate(message: Message) -> None:
    builder = InlineKeyboardBuilder()
    for amount in AMOUNTS:
        builder.button(text=f"⭐ {amount}", callback_data=f"donate:{amount}")
    builder.adjust(len(AMOUNTS))
    await message.answer(DONATE_TEXT, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("donate:"))
async def on_amount_chosen(query: CallbackQuery) -> None:
    try:
        amount = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer()
        return
    if amount not in AMOUNTS:
        await query.answer("Такой суммы нет", show_alert=True)
        return

    await query.answer()
    # Для Stars валюта — XTR, provider_token пустой: так устроен Bot API.
    await query.message.answer_invoice(
        title="Поддержка сервера бота",
        description="Спасибо! Stars идут на оплату сервера, где живёт расписание.",
        payload=f"donate:{amount}",
        currency="XTR",
        prices=[LabeledPrice(label="Поддержка", amount=amount)],
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    # Telegram требует ответить в течение 10 секунд, иначе платёж отменится.
    # Проверять здесь нечего: товар один, наличие не кончается.
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message) -> None:
    payment = message.successful_payment
    log.info(
        "Донат: %s Stars от пользователя %s",
        payment.total_amount,
        message.from_user.id if message.from_user else "?",
    )
    await message.answer("Спасибо! Сервер продолжает работать благодаря вам 💛")
