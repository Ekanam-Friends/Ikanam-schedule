"""Отметка посещаемости по QR из СДО (обкатка на владельце).

Пользователь на очной паре ловит QR преподавателя, шлёт боту фото (или саму
ссылку), а бот открывает её уже вошедшим в СДО и засчитывает присутствие. Смысл
в скорости: хеш в QR живёт секунды, поэтому весь путь — декод фото → готовая
тёплая сессия → один GET — укладывается в это окно, чего ручной вход с телефона
(сканер, выключить VPN, залогиниться, обновить) обычно не успевает.

Пока фича включается только для владельца (`OWNER_CHAT_ID`) и берёт логин/пароль
СДО из окружения, а не из общей базы: массовое хранение чужих паролей — отдельное
осознанное решение под подписку. Фото не от владельца просто игнорируем.
"""

from __future__ import annotations

import asyncio
import io
import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

from app.core.config import Settings
from app.ranepa.lms import (
    LmsAuthError,
    LmsClient,
    LmsTemporaryError,
    MarkResult,
    extract_qr_hash,
)

log = logging.getLogger(__name__)

router = Router(name="attendance")

# Одна тёплая сессия на процесс: пользователь-владелец один, вход дорогой, а
# держать сессию между отметками — весь смысл скорости. Лочим обращения, чтобы
# два быстрых фото подряд не устроили гонку перелогина.
_client: LmsClient | None = None
_lock = asyncio.Lock()


def _get_client(settings: Settings) -> LmsClient | None:
    global _client
    if settings.lms_login is None or settings.lms_password is None:
        return None
    if _client is None:
        _client = LmsClient(settings.lms_login, settings.lms_password.get_secret_value())
    return _client


def _is_owner(message: Message, settings: Settings) -> bool:
    return (
        settings.owner_chat_id is not None
        and message.from_user is not None
        and message.from_user.id == settings.owner_chat_id
    )


def _decode_qr(data: bytes) -> str | None:
    """Достать хеш отметки из картинки QR.

    `pyzbar` импортируем внутри — он тянет системную `libzbar0`, которой нет в
    окружении разработки и тестов; модуль должен грузиться и без неё.
    Пробуем исходник, ч/б и с автоконтрастом: фото экрана бывает бледным.
    """
    try:
        from PIL import Image, ImageOps
        from pyzbar.pyzbar import decode as zbar_decode
    except Exception as exc:  # noqa: BLE001 — нет библиотеки декодера
        log.error("Декодер QR недоступен: %s", exc)
        return None

    try:
        base = Image.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — битый файл, не картинка
        log.warning("Не удалось открыть присланное изображение: %s", exc)
        return None

    gray = ImageOps.grayscale(base)
    for variant in (base, gray, ImageOps.autocontrast(gray)):
        for found in zbar_decode(variant):
            candidate = extract_qr_hash(found.data.decode("utf-8", "ignore"))
            if candidate:
                return candidate
    return None


async def _mark_and_reply(message: Message, settings: Settings, qr_hash: str) -> None:
    client = _get_client(settings)
    if client is None:
        await message.answer(
            "Отметка по QR не настроена: нет LMS_LOGIN/LMS_PASSWORD в окружении."
        )
        return

    progress = await message.answer("Отмечаю…")
    try:
        async with _lock:
            marked = await client.mark(qr_hash)
    except LmsAuthError:
        await progress.edit_text(
            "СДО не приняла логин или пароль. Проверьте LMS_LOGIN/LMS_PASSWORD."
        )
        return
    except LmsTemporaryError as exc:
        log.warning("СДО недоступна при отметке: %s", exc)
        await progress.edit_text("СДО сейчас не отвечает. Попробуйте ещё раз.")
        return

    if marked.result is MarkResult.MARKED:
        await progress.edit_text(f"✅ Отмечено!\n\n{marked.message}")
    elif marked.result is MarkResult.EXPIRED:
        await progress.edit_text(
            "⌛ QR уже сменился, пока фото ехало. Сфоткайте текущий код и пришлите снова."
        )
    else:
        await progress.edit_text(
            "СДО ответила непонятно — присутствие могло не засчитаться. "
            f"Проверьте в СДО.\n\n{marked.message}"
        )


@router.message(F.photo)
async def on_qr_photo(message: Message, bot: Bot, settings: Settings) -> None:
    # Фото от посторонних игнорируем молча: у бота нет других фото-функций,
    # а объяснять чужим людям про отметку незачем.
    if not _is_owner(message, settings):
        return

    buffer = io.BytesIO()
    await bot.download(message.photo[-1], destination=buffer)
    qr_hash = _decode_qr(buffer.getvalue())
    if qr_hash is None:
        await message.answer(
            "Не разглядел QR на фото. Снимите ближе и ровнее, "
            "или пришлите саму ссылку из QR текстом."
        )
        return
    await _mark_and_reply(message, settings, qr_hash)


@router.message(F.text.contains("qrcode.php"))
async def on_qr_link(message: Message, settings: Settings) -> None:
    if not _is_owner(message, settings):
        return
    qr_hash = extract_qr_hash(message.text or "")
    if qr_hash is None:
        await message.answer("В ссылке не нашёл код отметки.")
        return
    await _mark_and_reply(message, settings, qr_hash)
