# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты handlers: меню /stats — кнопка ❌ Закрыть, reply, удаление меню+команды."""

from unittest.mock import AsyncMock

import pytest

import handlers


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


@pytest.mark.asyncio
async def test_stats_menu_close_deletes_menu_and_command():
    """stats:close удаляет и меню, и команду /stats (reply_to_message)."""

    callback = AsyncMock()
    callback.data = "stats:close"
    callback.message = AsyncMock()
    callback.message.reply_to_message = AsyncMock()

    await handlers.stats_menu_cb(callback)

    callback.answer.assert_awaited_once_with()
    callback.message.delete.assert_awaited_once()
    callback.message.reply_to_message.delete.assert_awaited_once()
    callback.message.answer.assert_not_called()


@pytest.mark.asyncio
async def test_stats_menu_close_without_reply_does_not_crash():
    """reply_to_message=None → закрытие удаляет только меню, без падения."""

    callback = AsyncMock()
    callback.data = "stats:close"
    callback.message = AsyncMock()
    callback.message.reply_to_message = None

    await handlers.stats_menu_cb(callback)

    callback.message.delete.assert_awaited_once()
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_stats_menu_close_handles_none_message():
    """callback.message=None (сообщение старше 48 ч) → close не падает."""

    callback = AsyncMock()
    callback.data = "stats:close"
    callback.message = None

    await handlers.stats_menu_cb(callback)
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_stats_menu_close_falls_back_to_edit_markup_when_delete_fails():
    """Если msg.delete() падает — убираем кнопки через edit_reply_markup(None)."""
    callback = AsyncMock()
    callback.data = "stats:close"
    callback.message = AsyncMock()
    callback.message.reply_to_message = None
    callback.message.delete = AsyncMock(side_effect=Exception("too old"))

    await handlers.stats_menu_cb(callback)

    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    callback.answer.assert_awaited_once_with()
