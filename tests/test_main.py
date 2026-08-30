# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Сборка приложения и lifecycle frozen-запуска."""

from types import SimpleNamespace
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest

import main


def _patch_app_dependencies(monkeypatch, *, frozen: bool):
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    registration_order = []
    reconcile_access = AsyncMock(
        side_effect=lambda: registration_order.append("access-recovery")
    )
    message_register = MagicMock(
        side_effect=lambda *args, **kwargs: registration_order.append("handler")
    )
    outer_middleware = MagicMock(
        side_effect=lambda *args, **kwargs: registration_order.append("middleware")
    )
    dispatcher = SimpleNamespace(
        update=SimpleNamespace(outer_middleware=outer_middleware),
        message=SimpleNamespace(register=message_register),
        callback_query=SimpleNamespace(register=MagicMock()),
        inline_query=SimpleNamespace(register=MagicMock()),
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
    monkeypatch.setattr(main, "reconcile_blocked_subscribers", reconcile_access)
    monkeypatch.setattr(main, "probe_owner_and_start", probe)
    monkeypatch.setattr(main, "start_health_server", health)
    monkeypatch.setattr(main, "start_update_loop", start_updates)
    monkeypatch.setattr(main, "WindowsConsoleCloseGuard", guard_factory)
    monkeypatch.setattr(main, "IS_FROZEN", frozen)
    return SimpleNamespace(
        bot=bot,
        dispatcher=dispatcher,
        probe=probe,
        health=health,
        start_updates=start_updates,
        guard=guard,
        guard_factory=guard_factory,
        registration_order=registration_order,
        reconcile_access=reconcile_access,
    )


@pytest.mark.asyncio
async def test_frozen_main_wires_updates_without_shutdown_backup(monkeypatch):
    app = _patch_app_dependencies(monkeypatch, frozen=True)

    with pytest.raises(RuntimeError, match="polling stopped"):
        await main.main()

    app.bot.set_my_commands.assert_awaited_once()
    public_commands = [
        command.command
        for command in app.bot.set_my_commands.await_args.args[0]
    ]
    public_descriptions = {
        command.command: command.description
        for command in app.bot.set_my_commands.await_args.args[0]
    }
    assert "info" in public_commands
    assert "fact" in public_commands
    assert public_descriptions["fact"] == "Интересный факт 💡"
    assert "version" not in public_commands
    registered_messages = [
        call.args[0]
        for call in app.dispatcher.message.register.call_args_list
    ]
    assert main.cmd_info in registered_messages
    assert main.cmd_fact in registered_messages
    assert main.cmd_block in registered_messages
    assert main.cmd_unblock in registered_messages
    app.dispatcher.inline_query.register.assert_called_once_with(
        main.cmd_inline_search
    )
    blocklist_registrations = [
        call
        for call in app.dispatcher.message.register.call_args_list
        if call.args[0] is main.cmd_blocklist
    ]
    assert len(blocklist_registrations) == 1
    blocklist_filter = blocklist_registrations[0].args[1]
    assert isinstance(blocklist_filter, main.Command)
    assert blocklist_filter.commands == ("blocklist",)
    assert "blocklist" not in public_commands
    assert app.registration_order[:2] == ["access-recovery", "middleware"]
    app.reconcile_access.assert_awaited_once_with()
    middleware = app.dispatcher.update.outer_middleware.call_args.args[0]
    assert isinstance(middleware, main.AccessControlMiddleware)
    registered_callbacks = [
        call.args[0]
        for call in app.dispatcher.callback_query.register.call_args_list
    ]
    assert main.version_refresh_cb in registered_callbacks
    assert main.fact_next_cb in registered_callbacks
    fact_registration = next(
        call
        for call in app.dispatcher.callback_query.register.call_args_list
        if call.args[0] is main.fact_next_cb
    )
    fact_filter = fact_registration.args[1]
    assert fact_filter.resolve(SimpleNamespace(data="fact:next:777:anime-word")) is True
    assert fact_filter.resolve(SimpleNamespace(data="stats:all")) is False
    app.dispatcher.shutdown.register.assert_not_called()
    app.probe.assert_awaited_once_with(app.bot)
    app.start_updates.assert_called_once_with(app.bot)
    app.health.assert_not_awaited()
    app.guard.install.assert_called_once_with()
    app.dispatcher.start_polling.assert_awaited_once_with(
        app.bot,
        allowed_updates=["message", "callback_query", "inline_query"],
    )
    app.guard.complete.assert_called_once_with()
    app.guard.uninstall.assert_called_once_with()


@pytest.mark.asyncio
async def test_source_main_keeps_healthcheck_and_shutdown_backup(monkeypatch):
    app = _patch_app_dependencies(monkeypatch, frozen=False)

    with pytest.raises(RuntimeError, match="polling stopped"):
        await main.main()

    app.health.assert_awaited_once_with(check_interval=main.CHECK_INTERVAL)
    app.dispatcher.shutdown.register.assert_called_once_with(main._shutdown_backup)
    app.start_updates.assert_called_once_with(app.bot)
    app.guard_factory.assert_not_called()


@pytest.mark.asyncio
async def test_corrupted_blocked_state_keeps_owner_recovery_path(monkeypatch):
    app = _patch_app_dependencies(monkeypatch, frozen=True)
    app.reconcile_access.side_effect = main.BlockedUsersStateError("broken")

    with pytest.raises(RuntimeError, match="polling stopped"):
        await main.main()

    app.reconcile_access.assert_awaited_once_with()
    app.dispatcher.start_polling.assert_awaited_once()
