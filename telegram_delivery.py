# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Ограниченные повторы отдельных операций доставки через Telegram."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aiohttp
from aiogram.exceptions import (
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

_ResultT = TypeVar("_ResultT")

_MAX_RETRIES = 2
_TRANSIENT_BACKOFF = (0.5, 1.0)


async def _sleep(delay: float) -> None:
    """Тестовый шов для ожидания между попытками."""
    await asyncio.sleep(delay)


def is_blocked_error(exc: Exception) -> bool:
    """Означает ли ошибка, что подписчик больше недоступен для бота."""
    if isinstance(exc, TelegramForbiddenError):
        return True
    error = str(exc).lower()
    return (
        "bot was blocked" in error
        or "user is deactivated" in error
        or "chat not found" in error
    )


def _retry_delay(exc: Exception, retry_index: int) -> float | None:
    """Задержка повтора для временной ошибки либо None для постоянной."""
    if isinstance(exc, TelegramRetryAfter):
        return max(0.0, float(exc.retry_after))
    if isinstance(exc, TelegramEntityTooLarge):
        return None
    if isinstance(
        exc,
        (
            TelegramNetworkError,
            TelegramServerError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
        ),
    ):
        return _TRANSIENT_BACKOFF[retry_index]
    return None


async def send_with_retry(
    operation: Callable[[], Awaitable[_ResultT]],
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> _ResultT:
    """Выполнить свежую операцию доставки, повторив только временные сбои."""
    sleeper = sleep or _sleep
    retries = 0

    while True:
        try:
            return await operation()
        except Exception as exc:
            if retries >= _MAX_RETRIES:
                raise
            delay = _retry_delay(exc, retries)
            if delay is None:
                raise
            retries += 1
            await sleeper(delay)
