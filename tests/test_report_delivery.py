# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контракты общей последовательной доставки типизированных отчётов."""

from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.exceptions import TelegramServerError
from aiogram.methods import SendMessage

import telegram_delivery
from report_delivery import (
    FAILED_REPORT_NOTICE,
    PARTIAL_REPORT_NOTICE,
    deliver_rendered_report,
    deliver_report,
)
from report_model import (
    Report,
    line,
    section,
    unit,
)

_METHOD = SendMessage(chat_id=1, text="test")


def _three_unit_report() -> Report:
    return Report(tuple(unit(section(line(f"unit-{index}"))) for index in range(3)))


@pytest.mark.asyncio
async def test_transient_retry_succeeds_and_delivery_continues(monkeypatch):
    transient = TelegramServerError(method=_METHOD, message="temporary")
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[transient, object(), object(), object()])
    retry_sleep = AsyncMock()
    gap_sleep = AsyncMock()
    monkeypatch.setattr(telegram_delivery, "_sleep", retry_sleep)

    result = await deliver_report(bot, 7, _three_unit_report(), sleep=gap_sleep)

    assert result.delivered is True
    assert result.delivered_units == result.total_units == 3
    assert bot.send_message.await_count == 4
    assert retry_sleep.await_count == 1
    assert gap_sleep.await_count == 2


@pytest.mark.asyncio
async def test_permanent_failure_stops_later_units_and_reports_partial_delivery():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[object(), RuntimeError("permanent"), object()])

    result = await deliver_report(
        bot,
        7,
        _three_unit_report(),
        notify_partial=True,
        sleep=AsyncMock(),
    )

    assert result.delivered is False
    assert result.delivered_units == 1
    assert result.total_units == 3
    assert result.partial_notice_delivered is True
    assert bot.send_message.await_count == 3
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == [
        "unit-0",
        "unit-1",
        PARTIAL_REPORT_NOTICE,
    ]


@pytest.mark.asyncio
async def test_exhausted_transient_failure_stops_before_next_unit(monkeypatch):
    transient = TelegramServerError(method=_METHOD, message="temporary")
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[transient, transient, transient, object()])
    monkeypatch.setattr(telegram_delivery, "_sleep", AsyncMock())

    result = await deliver_report(
        bot,
        7,
        _three_unit_report(),
        notify_partial=True,
        sleep=AsyncMock(),
    )

    assert result.delivered is False
    assert result.delivered_units == 0
    assert bot.send_message.await_count == 4
    texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert texts == ["unit-0", "unit-0", "unit-0", FAILED_REPORT_NOTICE]


@pytest.mark.asyncio
async def test_renderer_failure_returns_explicit_result_and_notice():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(bot, 7, object(), notify_partial=True)

    assert result.delivered is False
    assert result.delivered_units == result.total_units == 0
    assert isinstance(result.error, AttributeError)
    assert result.partial_notice_delivered is True
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == FAILED_REPORT_NOTICE


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_preview", [False, True])
async def test_preview_policy_is_applied_to_every_report_unit(disable_preview):
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(
        bot,
        7,
        _three_unit_report(),
        disable_preview=disable_preview,
        sleep=AsyncMock(),
    )

    assert result.delivered is True
    assert bot.send_message.await_count == 3
    assert {
        call.kwargs["disable_web_page_preview"]
        for call in bot.send_message.await_args_list
    } == {disable_preview}


@pytest.mark.asyncio
async def test_rendered_report_skips_empty_messages_and_propagates_preview_policy():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_rendered_report(
        bot,
        7,
        ("", "first", "   ", "second"),
        disable_preview=True,
        sleep=AsyncMock(),
    )

    assert result.delivered is True
    assert result.delivered_units == result.total_units == 2
    assert bot.send_message.await_count == 2
    assert [
        call.kwargs["text"]
        for call in bot.send_message.await_args_list
    ] == ["first", "second"]
    assert all(
        call.kwargs["disable_web_page_preview"] is True
        for call in bot.send_message.await_args_list
    )
