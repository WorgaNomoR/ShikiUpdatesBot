# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Owner-only команды /block и /unblock."""

from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.enums import ParseMode

import handlers
from storage import BlockedUsersStateError


def _message(text, user_id=None):
    message = MagicMock()
    message.text = text
    message.from_user = MagicMock(
        id=handlers.OWNER_ID if user_id is None else user_id
    )
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name, command", [("cmd_block", "/block 77"), ("cmd_unblock", "/unblock 77")])
async def test_access_commands_reject_forged_non_owner_calls(
    monkeypatch,
    handler_name,
    command,
):
    add = AsyncMock()
    remove = AsyncMock()
    monkeypatch.setattr(handlers, "add_blocked_user", add)
    monkeypatch.setattr(handlers, "remove_blocked_user", remove)
    message = _message(command, handlers.OWNER_ID + 1)

    await getattr(handlers, handler_name)(message)

    add.assert_not_awaited()
    remove.assert_not_awaited()
    assert message.answer.await_args.args == (
        "🚫 Эта команда только для владельца бота.",
    )
    assert message.answer.await_args.kwargs["parse_mode"] == ParseMode.HTML


@pytest.mark.asyncio
async def test_block_accepts_previously_unknown_numeric_id(monkeypatch):
    add = AsyncMock(return_value=(True, False))
    monkeypatch.setattr(handlers, "add_blocked_user", add)
    message = _message("/block 987654321")

    await handlers.cmd_block(message)

    add.assert_awaited_once_with(987654321)
    assert "<code>987654321</code>" in message.answer.await_args.args[0]
    assert message.answer.await_args.kwargs == {"parse_mode": ParseMode.HTML}


@pytest.mark.asyncio
async def test_block_reports_atomic_subscriber_removal(monkeypatch):
    monkeypatch.setattr(handlers, "add_blocked_user", AsyncMock(return_value=(True, True)))
    message = _message("/block 77")

    await handlers.cmd_block(message)

    assert "удалён из подписчиков" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_unblock_reports_non_restored_subscription(monkeypatch):
    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(handlers, "remove_blocked_user", remove)
    message = _message("/unblock 77")

    await handlers.cmd_unblock(message)

    remove.assert_awaited_once_with(77)
    assert "Подписка не восстановлена" in message.answer.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name,text",
    [
        ("cmd_block", "/block"),
        ("cmd_block", "/block abc"),
        ("cmd_block", "/block -1"),
        ("cmd_block", "/block 0"),
        ("cmd_block", "/block 1 2"),
        ("cmd_block", "/block 9223372036854775808"),
        ("cmd_unblock", "/unblock +7"),
        ("cmd_unblock", None),
    ],
)
async def test_access_commands_reject_malformed_ids(monkeypatch, handler_name, text):
    add = AsyncMock()
    remove = AsyncMock()
    monkeypatch.setattr(handlers, "add_blocked_user", add)
    monkeypatch.setattr(handlers, "remove_blocked_user", remove)
    message = _message(text)

    await getattr(handlers, handler_name)(message)

    add.assert_not_awaited()
    remove.assert_not_awaited()
    assert "<code>/" in message.answer.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name,command", [("cmd_block", "/block"), ("cmd_unblock", "/unblock")])
async def test_owner_id_is_rejected_before_storage(
    monkeypatch,
    handler_name,
    command,
):
    add = AsyncMock()
    remove = AsyncMock()
    monkeypatch.setattr(handlers, "add_blocked_user", add)
    monkeypatch.setattr(handlers, "remove_blocked_user", remove)
    message = _message(f"{command} {handlers.OWNER_ID}")

    await getattr(handlers, handler_name)(message)

    add.assert_not_awaited()
    remove.assert_not_awaited()
    assert "владел" in message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_storage_failure_returns_stable_text_without_raw_exception(monkeypatch):
    monkeypatch.setattr(
        handlers,
        "add_blocked_user",
        AsyncMock(side_effect=BlockedUsersStateError("private storage detail")),
    )
    message = _message("/block 77")

    await handlers.cmd_block(message)

    assert message.answer.await_args.args == (
        "❌ Не удалось безопасно изменить список блокировок. "
        "Подробности записаны в лог.",
    )
