"""Проверки повторов исходящих вызовов к Telegram."""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetMe

from app.bot.retry import RetryOnNetworkError


class FakeNext:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self, bot, method):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=GetMe(), message="ClientOSError")


async def test_network_error_is_retried_until_success():
    nxt = FakeNext([network_error(), network_error(), "ok"])

    result = await RetryOnNetworkError(attempts=4, base_delay=0)(nxt, None, GetMe())

    assert result == "ok"
    assert nxt.calls == 3


async def test_gives_up_after_attempts():
    nxt = FakeNext([network_error()] * 3)

    with pytest.raises(TelegramNetworkError):
        await RetryOnNetworkError(attempts=3, base_delay=0)(nxt, None, GetMe())

    assert nxt.calls == 3


async def test_api_errors_are_not_retried():
    """400 от Telegram — это ответ, а не обрыв; повторять его вредно."""
    nxt = FakeNext([TelegramBadRequest(method=GetMe(), message="chat not found")])

    with pytest.raises(TelegramBadRequest):
        await RetryOnNetworkError(attempts=4, base_delay=0)(nxt, None, GetMe())

    assert nxt.calls == 1
