# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты handlers: меню /stats — кнопка ❌ Закрыть, reply, удаление меню+команды."""

from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest

import handlers
from report_delivery import ReportDeliveryResult
from report_model import plain_report


def test_stats_menu_kb_has_close_button():
    """Меню /stats содержит кнопку ❌ Закрыть с callback_data 'stats:close'."""

    kb = handlers._stats_menu_kb()
    buttons = [b for row in kb.inline_keyboard for b in row]
    close = [b for b in buttons if b.callback_data == "stats:close"]
    assert len(close) == 1, "ожидал ровно одну кнопку закрытия"
    assert "Закры" in close[0].text


@pytest.mark.asyncio
async def test_cmd_stats_menu_is_reply():
    """Меню /stats шлётся ответом (reply) на команду — иначе ❌ Закрыть
    не сможет удалить саму команду (рвётся reply_to_message)."""

    message = AsyncMock()
    message.text = "/stats"

    await handlers.cmd_stats(message)

    message.reply.assert_awaited_once()
    message.answer.assert_not_called()
    reply_markup = message.reply.await_args.kwargs["reply_markup"]
    assert reply_markup == handlers._stats_menu_kb()
    assert any(
        button.callback_data == "stats:close"
        for row in reply_markup.inline_keyboard
        for button in row
    )


@pytest.mark.asyncio
async def test_stats_menu_close_answers_and_delegates_cleanup(monkeypatch):
    cleanup = AsyncMock()
    monkeypatch.setattr(handlers, "_cleanup_inline_menu", cleanup)
    callback = AsyncMock()
    callback.data = "stats:close"
    menu = callback.message

    await handlers.stats_menu_cb(callback)

    callback.answer.assert_awaited_once_with()
    cleanup.assert_awaited_once_with(menu)
    callback.message.answer.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_stats_all_keeps_direct_path_and_default_preview_policy(monkeypatch):
    report = plain_report("all-report")
    monkeypatch.setattr(handlers, "_stats_report_all", AsyncMock(return_value=report))
    delivery = AsyncMock(return_value=ReportDeliveryResult(True, 1, 1))
    monkeypatch.setattr(handlers, "deliver_report", delivery)
    message = MagicMock()
    message.text = "/stats all"
    message.bot = MagicMock()
    message.chat.id = 777
    message.answer = AsyncMock()

    await handlers.cmd_stats(message)

    delivery.assert_awaited_once_with(
        message.bot,
        777,
        report,
        notify_partial=True,
    )
    message.reply.assert_not_called()
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_stats_menu_selection_keeps_cleanup_and_shared_delivery(monkeypatch):
    report = plain_report("current-report")
    builder = AsyncMock(return_value=report)
    monkeypatch.setitem(handlers._STATS_BUILDERS, "current", builder)
    delivery = AsyncMock(return_value=ReportDeliveryResult(True, 1, 1))
    monkeypatch.setattr(handlers, "deliver_report", delivery)
    callback = MagicMock()
    callback.data = "stats:current"
    callback.answer = AsyncMock()
    callback.message.delete = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    callback.message.bot = MagicMock()
    callback.message.chat.id = 888
    callback.message.answer = AsyncMock()

    await handlers.stats_menu_cb(callback)

    callback.answer.assert_awaited_once_with()
    callback.message.delete.assert_awaited_once_with()
    builder.assert_awaited_once_with()
    delivery.assert_awaited_once_with(
        callback.message.bot,
        888,
        report,
        notify_partial=True,
    )
