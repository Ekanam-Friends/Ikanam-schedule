"""`/login` и `/logout`: подключение и отключение личного кабинета.

Диалог входа — два шага: логин, затем пароль. Сообщение с паролем удаляется
сразу, как только прочитано, ещё до похода в кабинет: если кабинет ответит
через минуту, пароль всё это время не должен висеть в чате.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.bot.texts import (
    LOGIN_ASK_LOGIN,
    LOGIN_ASK_PASSWORD,
    LOGIN_CANCELLED,
    LOGOUT_DONE,
    LOGOUT_NOTHING,
)
from app.db.repo import UserRepository
from app.services.account import AccountService, ConnectError

log = logging.getLogger(__name__)

router = Router(name="account")


class Login(StatesGroup):
    waiting_login = State()
    waiting_password = State()


@router.message(Command("login"))
async def on_login(message: Message, state: FSMContext) -> None:
    await state.set_state(Login.waiting_login)
    await message.answer(LOGIN_ASK_LOGIN)


@router.message(Command("cancel"))
@router.message(F.text.casefold() == "отмена")
async def on_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer(LOGIN_CANCELLED)


@router.message(Login.waiting_login, F.text)
async def on_login_received(message: Message, state: FSMContext) -> None:
    login = (message.text or "").strip()
    if login.startswith("/"):
        # Человек передумал и набрал другую команду — не считать её логином.
        await state.clear()
        return
    await state.update_data(login=login)
    await state.set_state(Login.waiting_password)
    await message.answer(LOGIN_ASK_PASSWORD)


@router.message(Login.waiting_password, F.text)
async def on_password_received(
    message: Message, state: FSMContext, account: AccountService
) -> None:
    password = message.text or ""
    data = await state.get_data()
    await state.clear()

    # Удаляем немедленно и независимо от исхода. Если бот не имеет права
    # удалять сообщения (в группах такое бывает), говорим об этом прямо.
    deleted = await _try_delete(message)
    if not deleted:
        await message.answer(
            "Не получилось удалить сообщение с паролем — удалите его сами, пожалуйста."
        )

    progress = await message.answer("Захожу в кабинет…")
    try:
        result = await account.connect(message.from_user.id, data["login"], password)
    except ConnectError as exc:
        await progress.edit_text(f"{exc}\n\nПопробовать ещё раз: /login")
        return
    finally:
        del password

    group = result.profile.group_name or "группа не указана"
    if result.lessons_count:
        tail = f"Загружено пар на ближайшие недели: {result.lessons_count}. Смотрите /today."
    else:
        tail = "Пар на ближайшие недели не нашлось — возможно, каникулы. Проверьте /status."
    await progress.edit_text(f"Подключено: <b>{group}</b>.\n{tail}")


@router.message(Login.waiting_login)
@router.message(Login.waiting_password)
async def on_unexpected_input(message: Message) -> None:
    await message.answer("Нужен текст. Чтобы прервать — напишите «отмена».")


@router.message(Command("logout"))
async def on_logout(message: Message, state: FSMContext, repo: UserRepository) -> None:
    await state.clear()
    deleted = await repo.delete(message.from_user.id)
    await message.answer(LOGOUT_DONE if deleted else LOGOUT_NOTHING)


async def _try_delete(message: Message) -> bool:
    try:
        await message.delete()
        return True
    except Exception as exc:  # noqa: BLE001 — любой отказ Telegram здесь равнозначен
        log.warning("Не удалось удалить сообщение с паролем: %s", exc)
        return False
