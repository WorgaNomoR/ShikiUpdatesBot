# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Авторитетные тесты общей очистки inline-меню и исходной команды."""

from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest

import handlers


@pytest.mark.asyncio
async def test_cleanup_inline_menu_deletes_menu_and_command():
    command = MagicMock()
    command.delete = AsyncMock()
    menu = MagicMock()
    menu.delete = AsyncMock()
    menu.reply_to_message = command

    await handlers._cleanup_inline_menu(menu)

    menu.delete.assert_awaited_once_with()
    menu.edit_reply_markup.assert_not_called()
    command.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cleanup_inline_menu_falls_back_to_removing_keyboard():
    menu = MagicMock()
    menu.delete = AsyncMock(side_effect=RuntimeError("message is inaccessible"))
    menu.edit_reply_markup = AsyncMock()
    menu.reply_to_message = None

    await handlers._cleanup_inline_menu(menu)

    menu.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_cleanup_inline_menu_tolerates_missing_message():
    await handlers._cleanup_inline_menu(None)


@pytest.mark.asyncio
async def test_cleanup_inline_menu_tolerates_all_telegram_failures():
    command = MagicMock()
    command.delete = AsyncMock(side_effect=RuntimeError("cannot delete command"))
    menu = MagicMock()
    menu.delete = AsyncMock(side_effect=RuntimeError("cannot delete menu"))
    menu.edit_reply_markup = AsyncMock(
        side_effect=RuntimeError("cannot edit markup"),
    )
    menu.reply_to_message = command

    await handlers._cleanup_inline_menu(menu)

    menu.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    command.delete.assert_awaited_once_with()
