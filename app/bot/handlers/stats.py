"""`/stats` — сводка для владельца.

Команды нет в меню и в `/start`: она не для пользователей. Для всех, кроме
владельца, её как будто не существует — сообщение проваливается к обработчику
неизвестного текста, а не получает ответ «нет доступа», который сам по себе
подсказывал бы, что тут что-то есть.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.core.config import Settings
from app.db.activity import ActivityLog
from app.db.repo import UserRepository
from app.services.stats import collect, format_summary, render_chart

log = logging.getLogger(__name__)

router = Router(name="stats")


def is_owner(message: Message, settings: Settings) -> bool:
    return (
        settings.owner_chat_id is not None
        and message.from_user is not None
        and message.from_user.id == settings.owner_chat_id
    )


@router.message(Command("stats"), is_owner)
async def on_stats(message: Message, repo: UserRepository, settings: Settings) -> None:
    stats = await collect(repo, ActivityLog(repo.session))
    await message.answer(format_summary(stats))
    try:
        # Рисование — синхронное и заметное на одном ядре; не держим им
        # event loop, пока другие пользователи ждут ответа.
        png = await asyncio.to_thread(render_chart, stats)
    except Exception:  # noqa: BLE001 — картинка не обязана ломать цифры
        log.exception("Не удалось построить график статистики")
        await message.answer("График построить не удалось, подробности в логе.")
        return
    await message.answer_photo(BufferedInputFile(png, filename="stats.png"))
