# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Общая Telegram boundary для доставки типизированных отчётов."""

import asyncio
from collections import defaultdict
from collections.abc import (
    Awaitable,
    Callable,
    Sequence,
)
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNotFound
from aiogram.methods import SendRichMessage

from config import log
from report_assets import (
    ReportAssetError,
    materialize_rich_message,
)
from report_model import (
    RenderedChunk,
    Report,
    render_report,
)
from report_plan import (
    downgrade_rich_units,
    html_transport_unit,
    rich_transport_unit,
    validate_frozen_report_units,
)
from rich_report import (
    RenderedRichUnit,
    render_rich_report,
    report_has_rich_features,
)
from telegram_delivery import send_with_retry

PARTIAL_REPORT_NOTICE = (
    "⚠️ Отчёт доставлен не полностью. Попробуй отправить его ещё раз позже."
)
FAILED_REPORT_NOTICE = (
    "⚠️ Не удалось доставить отчёт. Попробуй отправить его ещё раз позже."
)
_DELIVERY_GAP = 0.3
_UNSUPPORTED_METHOD_DESCRIPTION = "Not Found"


@dataclass(frozen=True)
class ReportDeliveryResult:
    """Явный итог последовательной доставки отчёта."""

    delivered: bool
    delivered_units: int
    total_units: int
    error: Exception | None = None
    partial_notice_delivered: bool = False
    next_unit: int = 0


def is_rich_method_unsupported(exc: Exception) -> bool:
    """Только однозначный 404 именно для sendRichMessage разрешает fallback."""
    return (
        isinstance(exc, TelegramNotFound)
        and isinstance(exc.method, SendRichMessage)
        and exc.message == _UNSUPPORTED_METHOD_DESCRIPTION
    )


def is_local_rich_asset_error(exc: Exception) -> bool:
    """Распознать безопасную для frozen HTML fallback ошибку asset."""
    return isinstance(exc, ReportAssetError)


def _freeze_rich_plan(
    report: Report,
    ordinary: Sequence[RenderedChunk],
    rendered_rich: Sequence[RenderedRichUnit],
    *,
    disable_preview: bool,
) -> list[dict]:
    """Сопоставить Rich units с обязательным полным HTML fallback."""
    ordinary_by_unit: dict[int, list[str]] = defaultdict(list)
    for chunk in ordinary:
        ordinary_by_unit[chunk.unit_index].append(chunk.html)
    rich_by_unit = {unit.unit_index: unit for unit in rendered_rich}
    valid_indices = set(range(len(report.units)))
    if len(rich_by_unit) != len(rendered_rich):
        raise ValueError("rich_unit_index_duplicate")
    if not set(rich_by_unit) <= valid_indices:
        raise ValueError("rich_unit_index_out_of_range")
    if not set(ordinary_by_unit) <= valid_indices:
        raise ValueError("html_unit_index_out_of_range")
    frozen = []
    for unit_index in range(len(report.units)):
        fallbacks = ordinary_by_unit.get(unit_index, [])
        rich = rich_by_unit.get(unit_index)
        if rich is not None:
            if not fallbacks:
                raise ValueError("rich_unit_without_html_fallback")
            frozen.append(rich_transport_unit(
                rich.payload,
                fallbacks,
                fallback_disable_preview=disable_preview,
            ))
        else:
            frozen.extend(
                html_transport_unit(
                    message,
                    disable_preview=disable_preview,
                )
                for message in fallbacks
            )
    validate_frozen_report_units(frozen)
    return frozen


def freeze_report(
    report: Report,
    *,
    disable_preview: bool = False,
) -> list[dict]:
    """До первого await зафиксировать transport и оба безопасных представления."""
    ordinary = render_report(report)

    if not report_has_rich_features(report):
        return [
            html_transport_unit(chunk.html, disable_preview=disable_preview)
            for chunk in ordinary
        ]

    try:
        rendered_rich = render_rich_report(report)
        for unit in rendered_rich:
            # Проверяем доступность versioned assets до первого Telegram await.
            materialize_rich_message(unit.payload)
        return _freeze_rich_plan(
            report,
            ordinary,
            rendered_rich,
            disable_preview=disable_preview,
        )
    except Exception as exc:
        log.warning(
            "freeze_report: rich-представление недоступно, откат на HTML (%s)",
            type(exc).__name__,
        )
        return [
            html_transport_unit(chunk.html, disable_preview=disable_preview)
            for chunk in ordinary
        ]


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


async def deliver_frozen_report(
    bot: Bot,
    chat_id: int,
    units: Sequence[dict],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    start_unit: int = 0,
    before_send: Callable[[int], Awaitable[None]] | None = None,
    acknowledge: Callable[[int], Awaitable[None]] | None = None,
) -> ReportDeliveryResult:
    """Доставить exact frozen transport units до первой ошибки."""
    try:
        frozen = list(units)
        validate_frozen_report_units(frozen)
        if type(start_unit) is not int or not 0 <= start_unit <= len(frozen):
            raise ValueError("invalid_rendered_plan")
    except Exception as exc:
        return ReportDeliveryResult(
            False,
            0,
            len(units),
            exc,
            next_unit=start_unit,
        )

    try:
        if (
            start_unit < len(frozen)
            and frozen[start_unit]["transport"] == "rich"
        ):
            # Проверяем только текущую unit, чтобы ошибка сохранила точный next_unit.
            materialize_rich_message(frozen[start_unit]["content"])
    except Exception as exc:
        return ReportDeliveryResult(
            False,
            0,
            len(frozen),
            exc,
            next_unit=start_unit,
        )

    delivered_units = 0
    next_unit = start_unit
    for index in range(start_unit, len(frozen)):
        transport_unit = frozen[index]

        async def send_unit(index=index, transport_unit=transport_unit):
            # Проверяем durable state заново и перед retry того же transport.
            if before_send is not None:
                await before_send(index)
            if transport_unit["transport"] == "rich":
                # Asset перечитывается и проверяется перед каждой попыткой.
                rich_message = materialize_rich_message(transport_unit["content"])
                return await bot.send_rich_message(
                    chat_id=chat_id,
                    rich_message=rich_message,
                )
            return await bot.send_message(
                chat_id=chat_id,
                text=transport_unit["content"],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=transport_unit["disable_preview"],
            )

        try:
            await send_with_retry(send_unit)
            delivered_units += 1
            if acknowledge is not None:
                await acknowledge(index)
            next_unit = index + 1
        except Exception as exc:
            return ReportDeliveryResult(
                False,
                delivered_units,
                len(frozen),
                exc,
                next_unit=next_unit,
            )
        if index + 1 < len(frozen):
            await sleep(_DELIVERY_GAP)
    return ReportDeliveryResult(
        True,
        delivered_units,
        len(frozen),
        next_unit=next_unit,
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
    """Отобразить и доставить отчёт с узким unambiguous rich fallback."""
    try:
        frozen = freeze_report(report, disable_preview=disable_preview)
    except Exception as exc:
        notice_delivered = (
            await _try_failure_notice(bot, chat_id, 0)
            if notify_partial
            else False
        )
        return ReportDeliveryResult(
            False,
            0,
            0,
            exc,
            notice_delivered,
        )

    delivered_units = 0
    start_unit = 0
    while True:
        result = await deliver_frozen_report(
            bot,
            chat_id,
            frozen,
            sleep=sleep,
            start_unit=start_unit,
        )
        delivered_units += result.delivered_units
        if result.delivered:
            return ReportDeliveryResult(
                True,
                delivered_units,
                len(frozen),
                next_unit=result.next_unit,
            )
        if (
            is_rich_method_unsupported(result.error)
            or is_local_rich_asset_error(result.error)
        ):
            try:
                frozen = downgrade_rich_units(frozen, result.next_unit)
                start_unit = result.next_unit
            except Exception as exc:
                result = ReportDeliveryResult(
                    False,
                    delivered_units,
                    len(frozen),
                    exc,
                    next_unit=result.next_unit,
                )
            else:
                continue
        notice_delivered = (
            await _try_failure_notice(bot, chat_id, delivered_units)
            if notify_partial
            else False
        )
        return ReportDeliveryResult(
            False,
            delivered_units,
            len(frozen),
            result.error,
            notice_delivered,
            result.next_unit,
        )


async def deliver_rendered_report(
    bot: Bot,
    chat_id: int,
    messages: Sequence[str],
    *,
    disable_preview: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    start_unit: int = 0,
    before_send: Callable[[int], Awaitable[None]] | None = None,
    acknowledge: Callable[[int], Awaitable[None]] | None = None,
) -> ReportDeliveryResult:
    """Продолжить legacy version-1 frozen HTML plan без rerender."""
    if (
        type(start_unit) is not int
        or not 0 <= start_unit <= len(messages)
        or any(
            not isinstance(message, str) or not message.strip()
            for message in messages
        )
    ):
        return ReportDeliveryResult(
            False,
            0,
            len(messages),
            ValueError("invalid_rendered_plan"),
        )
    units = [
        html_transport_unit(message, disable_preview=disable_preview)
        for message in messages
    ]
    return await deliver_frozen_report(
        bot,
        chat_id,
        units,
        sleep=sleep,
        start_unit=start_unit,
        before_send=before_send,
        acknowledge=acknowledge,
    )
