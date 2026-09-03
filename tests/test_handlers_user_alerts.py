# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Скрытая owner-only настройка уведомлений о новых пользователях."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ParseMode

import handlers
from storage import UserAlertsStateError


def _message(user_id):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id) if user_id is not None else None,
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, handlers.OWNER_ID + 1])
async def test_useralerts_rejects_non_owner_before_storage(monkeypatch, user_id):
    change = AsyncMock()
    monkeypatch.setattr(handlers, "set_user_alerts_enabled", change)
    message = _message(user_id)

    await handlers.cmd_useralerts(message, SimpleNamespace(args="off"))

    change.assert_not_awaited()
    message.answer.assert_awaited_once_with(
        "🚫 Эта команда только для владельца бота.",
        parse_mode=ParseMode.HTML,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [None, "", "status", "on now"])
async def test_useralerts_rejects_invalid_usage_without_storage(monkeypatch, args):
    change = AsyncMock()
    monkeypatch.setattr(handlers, "set_user_alerts_enabled", change)
    message = _message(handlers.OWNER_ID)

    await handlers.cmd_useralerts(message, SimpleNamespace(args=args))

    change.assert_not_awaited()
    reply = message.answer.await_args.args[0]
    assert "/useralerts on" in reply
    assert "/useralerts off" in reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "enabled", "changed", "prefix", "opposite"),
    [
        (" ON ", True, True, "✅", "/useralerts off"),
        ("off", False, True, "✅", "/useralerts on"),
        ("on", True, False, "ℹ️", "/useralerts off"),
        ("OFF", False, False, "ℹ️", "/useralerts on"),
    ],
)
async def test_useralerts_sets_and_confirms_result(
    monkeypatch,
    args,
    enabled,
    changed,
    prefix,
    opposite,
):
    change = AsyncMock(return_value=changed)
    monkeypatch.setattr(handlers, "set_user_alerts_enabled", change)
    message = _message(handlers.OWNER_ID)

    await handlers.cmd_useralerts(message, SimpleNamespace(args=args))

    change.assert_awaited_once_with(enabled)
    reply = message.answer.await_args.args[0]
    assert reply.startswith(prefix)
    assert opposite in reply
    assert message.answer.await_args.kwargs == {"parse_mode": ParseMode.HTML}


@pytest.mark.asyncio
async def test_useralerts_malformed_state_reports_recovery_without_overwrite(
    monkeypatch,
):
    change = AsyncMock(side_effect=UserAlertsStateError("private path"))
    monkeypatch.setattr(handlers, "set_user_alerts_enabled", change)
    message = _message(handlers.OWNER_ID)

    await handlers.cmd_useralerts(message, SimpleNamespace(args="off"))

    change.assert_awaited_once_with(False)
    reply = message.answer.await_args.args[0]
    assert "/backup" in reply
    assert "private path" not in reply


@pytest.mark.asyncio
async def test_useralerts_write_failure_is_reported_without_crashing(monkeypatch):
    change = AsyncMock(side_effect=OSError("private path"))
    monkeypatch.setattr(handlers, "set_user_alerts_enabled", change)
    message = _message(handlers.OWNER_ID)

    await handlers.cmd_useralerts(message, SimpleNamespace(args="off"))

    change.assert_awaited_once_with(False)
    reply = message.answer.await_args.args[0]
    assert "/backup" in reply
    assert "private path" not in reply
