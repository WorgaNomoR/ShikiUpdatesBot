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
