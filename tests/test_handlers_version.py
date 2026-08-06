# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Владелецкая команда /version."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ParseMode

import handlers


@pytest.mark.asyncio
async def test_version_rejects_non_owner():
    message = AsyncMock()
    message.from_user = MagicMock(id=handlers.OWNER_ID + 1)
    await handlers.cmd_version(message)
    assert "только для владельца" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_version_refreshes_and_renders(monkeypatch):
    state = {"latest_version": "v1.2.0", "release_url": "https://release"}
    refresh = AsyncMock(return_value=state)
    monkeypatch.setattr(handlers, "refresh_update_state", refresh)
    message = AsyncMock()
    message.from_user = MagicMock(id=handlers.OWNER_ID)

    await handlers.cmd_version(message)

    refresh.assert_awaited_once_with(force=True)
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert "<b>ShikiUpdatesBot</b>" in text
    assert "v1.2.0" in text
    assert message.answer.await_args.kwargs["parse_mode"] == ParseMode.HTML
    assert keyboard.inline_keyboard[0][-1].url == "https://release"
