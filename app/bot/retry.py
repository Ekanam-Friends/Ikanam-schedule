"""Повторы исходящих вызовов к Telegram при обрывах связи.

Из России до api.telegram.org — через прокси, и он рвёт заметную долю
соединений на TLS-рукопожатии. Без повторов это выглядит так: бот получил
`/login`, а ответ «введите логин» до человека не дошёл, и бот молчит.

Middleware сессии оборачивает каждый вызов Bot API. Повторяются только
сетевые ошибки: ответ Telegram с ошибкой (400, 403, 429) — это уже ответ, и
повторять его бессмысленно или вредно.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType

log = logging.getLogger(__name__)


class RetryOnNetworkError(BaseRequestMiddleware):
    def __init__(self, *, attempts: int = 4, base_delay: float = 1.0) -> None:
        self.attempts = attempts
        self.base_delay = base_delay

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Any:
        for attempt in range(1, self.attempts + 1):
            try:
                return await make_request(bot, method)
            except TelegramNetworkError as exc:
                if attempt == self.attempts:
                    raise
                delay = self.base_delay * 2 ** (attempt - 1)
                log.warning(
                    "%s: обрыв связи с Telegram (%s), повтор %d/%d через %.0f с",
                    type(method).__name__,
                    exc,
                    attempt + 1,
                    self.attempts,
                    delay,
                )
                await asyncio.sleep(delay)
        raise AssertionError("недостижимо")
