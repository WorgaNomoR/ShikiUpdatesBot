# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Общая Telegram boundary для доставки типизированных отчётов."""

import asyncio
from collections.abc import (
    Awaitable,
    Callable,
    Sequence,
)
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ParseMode

from report_model import (
    RenderedChunk,
    Report,
    render_report,
)
from telegram_delivery import send_with_retry

PARTIAL_REPORT_NOTICE = (
    "⚠️ Отчёт доставлен не полностью. Попробуй отправить его ещё раз позже."
)
FAILED_REPORT_NOTICE = (
    "⚠️ Не удалось доставить отчёт. Попробуй отправить его ещё раз позже."
)
_DELIVERY_GAP = 0.3


@dataclass(frozen=True)
class ReportDeliveryResult:
    """Явный итог последовательной доставки отчёта."""

    delivered: bool
    delivered_units: int
    total_units: int
    error: Exception | None = None
    partial_notice_delivered: bool = False


async def _try_failure_notice(bot: Bot, chat_id: int, delivered_units: int) -> bool:
    """Best-effort сообщить caller о полной или частичной ошибке доставки."""
    text = PARTIAL_REPORT_NOTICE if delivered_units else FAILED_REPORT_NOTICE
    try:
        await send_with_retry(
            lambda: bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        )
        return True
    except Exception:
        return False


async def _deliver_chunks(
    bot: Bot,
    chat_id: int,
    chunks: Sequence[RenderedChunk],
    *,
    disable_preview: bool,
    notify_partial: bool,
    sleep: Callable[[float], Awaitable[None]],
) -> ReportDeliveryResult:
    delivered_units = 0
    for index, chunk in enumerate(chunks):
        try:
            await send_with_retry(
                lambda chunk=chunk: bot.send_message(
                    chat_id=chat_id,
                    text=chunk.html,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=disable_preview,
                )
            )
        except Exception as exc:
            notice_delivered = (
                await _try_failure_notice(bot, chat_id, delivered_units)
                if notify_partial
                else False
            )
            return ReportDeliveryResult(
                delivered=False,
                delivered_units=delivered_units,
                total_units=len(chunks),
                error=exc,
                partial_notice_delivered=notice_delivered,
            )
        delivered_units += 1
        if index + 1 < len(chunks):
            await sleep(_DELIVERY_GAP)
    return ReportDeliveryResult(
        delivered=True,
        delivered_units=delivered_units,
        total_units=len(chunks),
    )


async def deliver_report(
    bot: Bot,
    chat_id: int,
    report: Report,
    *,
    disable_preview: bool = False,
    notify_partial: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ReportDeliveryResult:
    """Отобразить и последовательно доставить отчёт до первого failure."""
    try:
        chunks = render_report(report)
    except Exception as exc:
        notice_delivered = (
            await _try_failure_notice(bot, chat_id, 0)
            if notify_partial
            else False
        )
        return ReportDeliveryResult(
            delivered=False,
            delivered_units=0,
            total_units=0,
            error=exc,
            partial_notice_delivered=notice_delivered,
        )
    return await _deliver_chunks(
        bot,
        chat_id,
        chunks,
        disable_preview=disable_preview,
        notify_partial=notify_partial,
        sleep=sleep,
    )


async def deliver_rendered_report(
    bot: Bot,
    chat_id: int,
    messages: Sequence[str],
    *,
    disable_preview: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ReportDeliveryResult:
    """Доставить HTML chunks из текущего durable quarter-state через ту же boundary."""
    chunks = tuple(
        RenderedChunk(message, 0, index)
        for index, message in enumerate(messages)
        if message and message.strip()
    )
    return await _deliver_chunks(
        bot,
        chat_id,
        chunks,
        disable_preview=disable_preview,
        notify_partial=False,
        sleep=sleep,
    )
