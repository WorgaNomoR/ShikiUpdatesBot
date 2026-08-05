# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Сборка приложения и lifecycle frozen-запуска."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main


@pytest.mark.asyncio
async def test_frozen_main_wires_updates_and_cleans_console_guard(monkeypatch):
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    dispatcher = SimpleNamespace(
        message=SimpleNamespace(register=MagicMock()),
        callback_query=SimpleNamespace(register=MagicMock()),
        shutdown=SimpleNamespace(register=MagicMock()),
        start_polling=AsyncMock(side_effect=RuntimeError("polling stopped")),
        stop_polling=AsyncMock(),
    )
    probe = AsyncMock()
    health = AsyncMock()
    start_updates = MagicMock(return_value=True)
    guard = MagicMock()
    guard_factory = MagicMock(return_value=guard)

    monkeypatch.setattr(main, "Bot", lambda token: bot)
    monkeypatch.setattr(main, "Dispatcher", lambda storage: dispatcher)
    monkeypatch.setattr(main, "MemoryStorage", lambda: object())
    monkeypatch.setattr(main, "probe_owner_and_start", probe)
    monkeypatch.setattr(main, "start_health_server", health)
    monkeypatch.setattr(main, "start_update_loop", start_updates)
    monkeypatch.setattr(main, "WindowsConsoleCloseGuard", guard_factory)
    monkeypatch.setattr(main, "IS_FROZEN", True)

    with pytest.raises(RuntimeError, match="polling stopped"):
        await main.main()

    bot.set_my_commands.assert_awaited_once()
    dispatcher.shutdown.register.assert_called_once_with(main._shutdown_backup)
    probe.assert_awaited_once_with(bot)
    start_updates.assert_called_once_with(bot)
    health.assert_not_awaited()
    guard.install.assert_called_once_with()
    dispatcher.start_polling.assert_awaited_once_with(
        bot,
        allowed_updates=["message", "callback_query"],
    )
    guard.complete.assert_called_once_with()
    guard.uninstall.assert_called_once_with()
