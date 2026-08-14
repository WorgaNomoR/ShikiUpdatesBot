# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контракты общего ограниченного повтора Telegram-доставки."""

from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiogram.exceptions import (
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage

from telegram_delivery import is_blocked_error, send_with_retry

_METHOD = SendMessage(chat_id=1, text="test")


@pytest.mark.asyncio
async def test_send_with_retry_returns_first_success_without_sleep():
    operation = AsyncMock(return_value="sent")
    sleep = AsyncMock()

    assert await send_with_retry(operation, sleep=sleep) == "sent"

    operation.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_with_retry_retries_transient_client_failure():
    operation = AsyncMock(
        side_effect=[aiohttp.ClientOSError(104, "Connection reset by peer"), "sent"]
    )
    sleep = AsyncMock()

    assert await send_with_retry(operation, sleep=sleep) == "sent"

    assert operation.await_count == 2
    sleep.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
async def test_send_with_retry_exhausts_two_retries():
    error = TelegramServerError(method=_METHOD, message="server unavailable")
    operation = AsyncMock(side_effect=error)
    sleep = AsyncMock()

    with pytest.raises(TelegramServerError):
        await send_with_retry(operation, sleep=sleep)

    assert operation.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]


@pytest.mark.asyncio
async def test_send_with_retry_honours_retry_after_delay():
    error = TelegramRetryAfter(
        method=_METHOD,
        message="flood control",
        retry_after=7,
    )
    operation = AsyncMock(side_effect=[error, "sent"])
    sleep = AsyncMock()

    assert await send_with_retry(operation, sleep=sleep) == "sent"

    sleep.assert_awaited_once_with(7.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TelegramForbiddenError(method=_METHOD, message="bot was blocked"),
        TelegramEntityTooLarge(method=_METHOD, message="file is too large"),
        RuntimeError("permanent failure"),
    ],
)
async def test_send_with_retry_does_not_retry_permanent_errors(error):
    operation = AsyncMock(side_effect=error)
    sleep = AsyncMock()

    with pytest.raises(type(error)):
        await send_with_retry(operation, sleep=sleep)

    operation.assert_awaited_once()
    sleep.assert_not_awaited()


def test_is_blocked_error_accepts_aiogram_forbidden_error():
    error = TelegramForbiddenError(method=_METHOD, message="forbidden")

    assert is_blocked_error(error) is True
