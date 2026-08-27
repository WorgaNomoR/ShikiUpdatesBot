# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Центральная проверка Telegram updates по постоянному списку блокировок."""

from collections.abc import (
    Awaitable,
    Callable,
)
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    Message,
    Update,
)

from config import (
    OWNER_ID,
    log,
)
from storage import (
    BlockedUsersStateError,
    is_user_blocked,
)

ACCESS_DENIED_TEXT = "🚫 Доступ к боту закрыт."


def _update_sender(update: Update) -> tuple[Message | CallbackQuery | None, int | None]:
    """Безопасно извлечь поддерживаемое событие и положительный user ID."""
    event = getattr(update, "message", None) or getattr(update, "callback_query", None)
    user = getattr(event, "from_user", None)
    user_id = getattr(user, "id", None)
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return event, None
    return event, user_id


async def _deny_update(event: Message | CallbackQuery) -> None:
    """Отправить стабильный отказ без изменения старого сообщения или FSM."""
    try:
        if isinstance(event, CallbackQuery):
            await event.answer(ACCESS_DENIED_TEXT, show_alert=True)
        else:
            await event.answer(
                ACCESS_DENIED_TEXT,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
    except Exception as e:
        log.warning(
            "access-control: не удалось доставить отказ (%s)",
            type(e).__name__,
        )


class AccessControlMiddleware(BaseMiddleware):
    """Остановить запрещённые message/callback updates до диспетчеризации."""

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        subject, user_id = _update_sender(event)
        if subject is None:
            return await handler(event, data)
        if user_id is None:
            log.warning("access-control: update без корректного from_user остановлен")
            return None
        if user_id == OWNER_ID:
            return await handler(event, data)
        try:
            blocked = is_user_blocked(user_id)
        except (BlockedUsersStateError, ValueError):
            blocked = True
        if not blocked:
            return await handler(event, data)
        await _deny_update(subject)
        return None
