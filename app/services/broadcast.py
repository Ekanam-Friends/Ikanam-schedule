"""Доставка рассылки и выгрузка ответов в CSV."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from app.db.broadcasts import AnswerRow

log = logging.getLogger(__name__)

PAUSE_SECONDS = 0.05
"""Около 20 сообщений в секунду — ниже лимита Telegram в 30, с запасом на
обычную работу бота, которая идёт параллельно."""


@dataclass(slots=True)
class DeliveryReport:
    total: int = 0
    delivered: int = 0
    blocked: int = 0
    failed: int = 0

    def summary(self) -> str:
        lines = [f"Доставлено: {self.delivered} из {self.total}"]
        if self.blocked:
            lines.append(f"Заблокировали бота: {self.blocked}")
        if self.failed:
            lines.append(f"Ошибок: {self.failed} (подробности в логе)")
        return "\n".join(lines)


async def deliver(
    bot: Bot,
    chat_ids: Iterable[int],
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    pause: float = PAUSE_SECONDS,
) -> DeliveryReport:
    report = DeliveryReport()
    for chat_id in chat_ids:
        report.total += 1
        for attempt in (1, 2):
            try:
                # Текст владельца уходит как есть: разметка HTML в нём не
                # разбирается, и «<3» не сломает всю рассылку.
                await bot.send_message(chat_id, text, parse_mode=None, reply_markup=reply_markup)
                report.delivered += 1
            except TelegramRetryAfter as exc:
                if attempt == 1:
                    await asyncio.sleep(exc.retry_after)
                    continue
                report.failed += 1
            except TelegramForbiddenError:
                report.blocked += 1
            except TelegramAPIError as exc:
                log.warning("Рассылка: не доставлено %s: %s", chat_id, exc)
                report.failed += 1
            break
        await asyncio.sleep(pause)
    return report


def answers_csv(rows: Iterable[AnswerRow]) -> bytes:
    """CSV для Excel: точка с запятой и BOM — иначе русский Excel покажет
    кракозябры в одну колонку. Google Таблицы такой файл тоже понимают."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["ID пользователя", "Вопрос", "Ответ"])
    for row in rows:
        writer.writerow([row.user_id, row.question, row.answer])
    return buffer.getvalue().encode("utf-8-sig")
