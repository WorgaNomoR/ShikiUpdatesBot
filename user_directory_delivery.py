# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Переиспользуемый owner use-case доставки каталога пользователей."""

from aiogram import Bot
from aiogram.enums import ParseMode

from config import (
    OWNER_ID,
    log,
)
from report_delivery import (
    ReportDeliveryResult,
    deliver_report,
)
from storage import (
    UserDirectorySnapshotError,
    load_user_directory_snapshot,
)
from user_directory import (
    build_user_directory,
    build_user_directory_report,
)

USER_DIRECTORY_STATE_FAILURE = (
    "❌ Не удалось безопасно прочитать каталог пользователей. "
    "Подробности записаны в лог."
)


async def _send_state_failure(bot: Bot, chat_id: int) -> None:
    """Сообщить о полном отказе уже вне storage-транзакции."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=USER_DIRECTORY_STATE_FAILURE,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.warning(
            "user-directory: не удалось доставить сообщение об ошибке (%s)",
            type(e).__name__,
        )


async def deliver_user_directory(
    bot: Bot,
    chat_id: int,
) -> ReportDeliveryResult | None:
    """Снять coherent snapshot, построить и доставить каталог без мутаций."""
    try:
        snapshot = await load_user_directory_snapshot()
    except UserDirectorySnapshotError as e:
        log.error("user-directory: недоступен источник %s", e.source)
        await _send_state_failure(bot, chat_id)
        return None

    try:
        directory = build_user_directory(snapshot, owner_id=OWNER_ID)
        report = build_user_directory_report(directory)
    except Exception as e:
        log.error(
            "user-directory: не удалось построить отчёт (%s)",
            type(e).__name__,
        )
        await _send_state_failure(bot, chat_id)
        return None

    result = await deliver_report(
        bot,
        chat_id,
        report,
        disable_preview=True,
        notify_partial=True,
    )
    if not result.delivered:
        log.warning(
            "user-directory: доставка каталога завершилась ошибкой (%s)",
            type(result.error).__name__ if result.error is not None else "unknown",
        )
    return result
