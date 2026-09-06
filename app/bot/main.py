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

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message

from app.bot.commands import COMMANDS
from app.bot.deps import AppContext, DependenciesMiddleware
from app.bot.handlers import account as account_handlers
from app.bot.handlers import calendar as calendar_handlers
from app.bot.handlers import schedule as schedule_handlers
from app.bot.retry import RetryOnNetworkError
from app.bot.scheduler import SchedulerContext, start_background_tasks
from app.bot.storage import SQLAlchemyStorage
from app.bot.texts import build_start_message
from app.core.config import get_settings
from app.core.crypto import CredentialsCipher
from app.db.repo import UserRepository
from app.db.migrate import upgrade_to_head
from app.db.session import make_engine, make_session_factory

log = logging.getLogger(__name__)

router = Router(name="core")

IMPLEMENTED = {"start", "login", "logout", "today", "tomorrow", "week", "status", "calendar"}

NOT_READY = (
    "Эта команда ещё в разработке.\n"
    "Что уже работает: /login, /today, /tomorrow, /week, /calendar, /status, /logout"
)


@router.message(CommandStart())
async def on_start(message: Message, repo: UserRepository) -> None:
    user = await repo.get(message.from_user.id)
    connected = user is not None and user.is_connected
    await message.answer(build_start_message(is_connected=connected), disable_web_page_preview=True)


@router.message(Command("help"))
async def on_help(message: Message, repo: UserRepository) -> None:
    # /help не в меню намеренно: это тот же /start, просто люди привыкли
    # набирать именно его, и молчать в ответ — грубо.
    await on_start(message, repo)


@router.message(Command(*[c.name for c in COMMANDS if c.name not in IMPLEMENTED]))
async def on_not_ready(message: Message) -> None:
    """Честная заглушка для команд, которые ещё не реализованы.

    Лучше прямо сказать «пока не умею», чем притворяться: человек, получивший
    молчание на команду, решит, что бот сломан, и больше не вернётся.
    """
    await message.answer(NOT_READY)


@router.message(F.text)
async def on_unknown_text(message: Message) -> None:
    """Текст вне диалога. Молчать нельзя.

    Если человек прислал это в ответ на вопрос бота, который тот уже забыл,
    — например, пароль после перезапуска, — молчание оставит пароль висеть в
    чате без объяснений. Отвечаем всегда и говорим, что делать.
    """
    await message.answer(
        "Не понял. Если вы вводили логин или пароль — начните заново: /login. "
        "Список команд: /start"
    )


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
    # httpx пишет каждый запрос к кабинету на INFO — это лишний след о том,
    # кто и когда ходил в кабинет. Ошибки он и так поднимает на WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()

    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Повторы на каждый исходящий вызов: прокси до Telegram рвёт часть
    # соединений, и без этого ответы пользователям просто теряются.
    bot.session.middleware(RetryOnNetworkError())
    # Миграции — до первого апдейта. Обновление кода не должно требовать ни
    # ручных команд, ни пересоздания базы с потерей подключённых кабинетов.
    await upgrade_to_head(settings.database_url)
    engine = make_engine(settings.database_url)
    context = AppContext(
        settings=settings,
        cipher=CredentialsCipher(settings.credentials_key.get_secret_value()),
        session_factory=make_session_factory(engine),
    )

    # Состояние диалогов — в базе: перезапуск бота посреди ввода пароля не
    # должен превращаться в проигнорированное сообщение с паролем в чате.
    dispatcher = Dispatcher(storage=SQLAlchemyStorage(context.session_factory))
    dispatcher.update.middleware(DependenciesMiddleware(context))
    # Порядок важен: диалог входа должен перехватывать текст раньше, чем
    # общие обработчики решат, что это неизвестная команда.
    dispatcher.include_router(account_handlers.router)
    dispatcher.include_router(schedule_handlers.router)
    dispatcher.include_router(calendar_handlers.router)
    dispatcher.include_router(router)

    # Ночная синхронизация и утренняя сводка живут в том же процессе: две
    # asyncio-задачи рядом с polling, без отдельного планировщика.
    background = start_background_tasks(
        SchedulerContext(
            bot=bot,
            settings=settings,
            cipher=context.cipher,
            session_factory=context.session_factory,
        )
    )

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
    try:
        await dispatcher.start_polling(bot, drop_pending_updates=True)
    finally:
        for task in background:
            task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
