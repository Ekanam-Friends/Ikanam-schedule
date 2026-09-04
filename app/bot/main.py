"""Точка входа Telegram-бота.

Бот работает через long polling: для него не нужен публичный адрес, а значит
его можно запустить на любой машине для проверки. Вебхук появится вместе с
продакшн-деплоем, где публичный адрес и так есть у сервиса подписки.

Команды регистрируются в Telegram при каждом старте из общего списка —
меню в клиенте всегда совпадает с тем, что бот реально умеет.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message

from app.bot.commands import COMMANDS
from app.bot.texts import build_start_message
from app.core.config import get_settings

log = logging.getLogger(__name__)

router = Router(name="core")

NOT_READY = (
    "Эта команда ещё в разработке — бот пока учится подключать личный кабинет.\n"
    "Список того, что уже работает: /start"
)


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(build_start_message(is_connected=False), disable_web_page_preview=True)


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    # /help не в меню намеренно: это тот же /start, просто люди привыкли
    # набирать именно его, и молчать в ответ — грубо.
    await on_start(message)


@router.message(Command(*[c.name for c in COMMANDS if c.name != "start"]))
async def on_not_ready(message: Message) -> None:
    """Честная заглушка для команд, которые ещё не реализованы.

    Лучше прямо сказать «пока не умею», чем притворяться: человек, получивший
    молчание на /today, решит, что бот сломан, и больше не вернётся.
    """
    await message.answer(NOT_READY)


async def register_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [BotCommand(command=c.name, description=c.menu_hint) for c in COMMANDS]
    )


async def with_retries(action, *, what: str, attempts: int = 6):
    """Выполнить сетевой вызов к Telegram, переживая обрывы связи.

    Из России api.telegram.org доступен с перебоями: часть соединений просто
    не устанавливается. Падать из-за этого на старте — значит требовать от
    владельца перезапускать бота руками, пока не повезёт.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await action()
        except TelegramNetworkError as exc:
            if attempt == attempts:
                raise
            delay = min(2**attempt, 30)
            log.warning("%s: нет связи с Telegram (%s), повтор через %d с", what, exc, delay)
            await asyncio.sleep(delay)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()

    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        await with_retries(lambda: register_commands(bot), what="регистрация команд")
        me = await with_retries(bot.get_me, what="проверка токена")
    except TelegramNetworkError:
        log.error("Telegram недоступен. Если так постоянно — задайте TELEGRAM_PROXY в .env")
        await bot.session.close()
        raise SystemExit(1)

    log.info("Бот @%s запущен, команд зарегистрировано: %d", me.username, len(COMMANDS))

    # Накопившиеся за время простоя апдейты не обрабатываем: отвечать на
    # вчерашний /today сегодняшним расписанием — только путать людей.
    await dispatcher.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
