# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Регистрация разрешённых Telegram-пользователей перед запуском handler."""

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
)

from config import (
    OWNER_ID,
    log,
)
from storage import (
    KnownUser,
    KnownUsersStateError,
    register_known_user,
)
from utils import (
    _subscriber_link,
    h,
)

USER_ALERTS_HINT = (
    "Подсказка: <code>/useralerts off</code> — отключить такие уведомления; "
    "<code>/useralerts on</code> — включить снова."
)


def _event_identity(event: Message | CallbackQuery) -> tuple[int, str, str | None] | None:
    """Извлечь пригодную первоначальную личность из поддерживаемого события."""
    user = getattr(event, "from_user", None)
    user_id = getattr(user, "id", None)
    display_name = getattr(user, "full_name", None)
    username = getattr(user, "username", None)
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or not isinstance(display_name, str)
        or not display_name.strip()
    ):
        return None
    if username is not None and (
        not isinstance(username, str) or not username.strip()
    ):
        username = None
    return user_id, display_name, username


def build_new_user_alert(user: KnownUser) -> str:
    """Собрать безопасное уведомление владельцу о новом пользователе."""
    lines = [
        "👤 <b>Новый пользователь бота</b>",
        "",
        f"Пользователь: {_subscriber_link(user.user_id, user.display_name)}",
    ]
    if user.username is not None:
        lines.append(f"Username: @{h(user.username)}")
    lines.extend(("", USER_ALERTS_HINT))
    return "\n".join(lines)


async def _send_new_user_alert(bot: Any, user: KnownUser) -> None:
    """Один раз попытаться отправить alert, не меняя durable state."""
    if bot is None or not callable(getattr(bot, "send_message", None)):
        log.warning("user-registry: bot недоступен для уведомления владельца")
        return
    try:
        await bot.send_message(
            OWNER_ID,
            build_new_user_alert(user),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.warning(
            "user-registry: не удалось уведомить владельца (%s)",
            type(e).__name__,
        )


class UserRegistryMiddleware(BaseMiddleware):
    """Зарегистрировать sender уже разрешённого и сопоставленного события."""

    async def __call__(
        self,
        handler: Callable[[Message | CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        identity = _event_identity(event)
        registration = None
        if identity is not None and identity[0] != OWNER_ID:
            try:
                registration = await register_known_user(*identity)
            except (KnownUsersStateError, OSError, ValueError) as e:
                log.warning(
                    "user-registry: регистрация пропущена (%s)",
                    type(e).__name__,
                )

        try:
            return await handler(event, data)
        finally:
            if registration is not None and registration.should_alert:
                await _send_new_user_alert(data.get("bot"), registration.user)
