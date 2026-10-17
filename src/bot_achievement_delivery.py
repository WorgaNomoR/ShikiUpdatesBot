# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Единая публикация семей бота и повторное использование владельческой доставки."""

import asyncio
import time
import weakref

import bot_achievement_catalog as bot_catalog
from achievement_delivery import dispatch_owner_achievements
from bot_achievements import capture_bot_achievements
from config import (
    ERROR_NOTIFY_INTERVAL,
    OWNER_ID,
    SHIKI_USER,
    log,
)
from shiki_api import ProfilePrivacyError
from storage import (
    load_bot_achievement_state,
    restorable_restore_generation,
    restorable_state_transaction,
    save_bot_achievement_state,
)
from telegram_delivery import (
    RetryPolicy,
    send_with_retry,
)

_locks = weakref.WeakKeyDictionary()
_diagnostics = weakref.WeakKeyDictionary()


async def capture_bot_achievement_sync(inputs, *, generation):
    """Явные входы калькуляторов, привязанные к поколению до получения данных."""
    async with restorable_state_transaction():
        if generation != restorable_restore_generation():
            return False
        expected = load_bot_achievement_state()
    def failed(family, error):
        if isinstance(error, ProfilePrivacyError):
            raise error
        log.warning("Достижения бота: расчёт семьи %s пропущен (%s).", family, type(error).__name__)

    candidate = capture_bot_achievements(
        expected, inputs, catalog=bot_catalog.BOT_ACHIEVEMENTS,
        profile=SHIKI_USER, owner_id=OWNER_ID, now=time.time(), on_error=failed,
    )
    async with restorable_state_transaction():
        if generation != restorable_restore_generation() or load_bot_achievement_state() != expected:
            return False
        if candidate != expected:
            save_bot_achievement_state(candidate)
    return True


async def dispatch_bot_achievements(bot):
    """Независимая очередь в той же паузе поллинга; ошибка источника ей не мешает."""
    lock = _locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with lock:
        return await dispatch_owner_achievements(
            bot, load_state=load_bot_achievement_state, save_state=save_bot_achievement_state,
        )


async def bot_achievement_diagnostic(bot, error):
    """Своя редкая статическая диагностика без исходных данных и адресов."""
    log.warning("Достижения бота: работа приостановлена (%s).", type(error).__name__)
    loop = asyncio.get_running_loop()
    now = time.monotonic()
    previous = _diagnostics.get(loop)
    if previous is not None and now - previous < ERROR_NOTIFY_INTERVAL:
        return
    _diagnostics[loop] = now
    await send_with_retry(
        lambda: bot.send_message(OWNER_ID,
            "⚠️ Достижения бота временно не обрабатываются: сохранённое состояние "
            "не удалось проверить или обновить. Данные оставлены на месте. Проверь логи и резервную копию."),
        policy=RetryPolicy.SAFE_ONLY, max_attempts=1,
    )
