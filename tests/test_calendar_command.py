"""Проверки текста `/calendar`."""

from __future__ import annotations

import base64
import os

from app.bot.handlers.calendar import build_calendar_message, keyboard
from app.core.config import Settings
from app.db.models import User


def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        BOT_TOKEN="1:x",
        CREDENTIALS_KEY=base64.urlsafe_b64encode(os.urandom(32)).decode(),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        PUBLIC_BASE_URL="https://schedule.example.ru",
    )


def test_message_gives_webcal_for_apple_and_https_for_google():
    """`webcal://` — иначе iOS скачает файл вместо подписки; Google понимает только https."""
    user = User(telegram_id=1, feed_token="tok-abc")

    text = build_calendar_message(user, settings())

    assert "webcal://schedule.example.ru/feed/tok-abc.ics" in text
    assert "https://schedule.example.ru/feed/tok-abc.ics" in text


def test_message_warns_that_link_is_a_secret():
    text = build_calendar_message(User(telegram_id=1, feed_token="t"), settings())

    assert "личная" in text.lower()
    assert "Новая ссылка" in text


def test_keyboard_offers_file_and_rotation():
    buttons = [b.callback_data for row in keyboard().as_markup().inline_keyboard for b in row]

    assert buttons == ["calendar:file", "calendar:rotate"]
