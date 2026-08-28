# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Owner-only команды /block, /unblock и /blocklist."""

import re
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.enums import ParseMode

import handlers
from storage import BlockedUsersStateError

_EXPECTED_BLOCKLIST_HINT = (
    "Подсказка: <code>/block 123456789</code> — заблокировать; "
    "<code>/unblock 123456789</code> — разблокировать."
)
_EXPECTED_TELEGRAM_MESSAGE_LIMIT = 4096


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
    add = AsyncMock(side_effect=BlockedUsersStateError("private storage detail"))
    monkeypatch.setattr(handlers, "add_blocked_user", add)
    message = _message("/block 77")

    await handlers.cmd_block(message)

    add.assert_awaited_once_with(77)
    assert message.answer.await_args.args == (
        "❌ Не удалось безопасно изменить список блокировок. "
        "Подробности записаны в лог.",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [handlers.OWNER_ID + 1, None])
async def test_blocklist_denies_before_storage_access(monkeypatch, user_id):
    list_blocked = MagicMock()
    monkeypatch.setattr(handlers, "list_blocked_users", list_blocked)
    message = _message("/blocklist", user_id)
    if user_id is None:
        message.from_user = None

    await handlers.cmd_blocklist(message)

    list_blocked.assert_not_called()
    assert message.answer.await_count == 1
    assert message.answer.await_args.args == (
        "🚫 Эта команда только для владельца бота.",
    )
    assert message.answer.await_args.kwargs == {"parse_mode": ParseMode.HTML}


@pytest.mark.asyncio
async def test_blocklist_empty_state_has_one_management_hint(monkeypatch):
    monkeypatch.setattr(handlers, "list_blocked_users", MagicMock(return_value=set()))
    message = _message("/blocklist")

    await handlers.cmd_blocklist(message)

    assert message.answer.await_count == 1
    text = message.answer.await_args.args[0]
    assert text.startswith("📭 Список блокировок пуст.")
    assert text.count(_EXPECTED_BLOCKLIST_HINT) == 1
    assert message.answer.await_args.kwargs == {"parse_mode": ParseMode.HTML}


@pytest.mark.asyncio
async def test_blocklist_sorts_and_renders_every_id_without_mutation(monkeypatch):
    list_blocked = MagicMock(return_value={987654321, 12, 300})
    add = AsyncMock()
    remove = AsyncMock()
    monkeypatch.setattr(handlers, "list_blocked_users", list_blocked)
    monkeypatch.setattr(handlers, "add_blocked_user", add)
    monkeypatch.setattr(handlers, "remove_blocked_user", remove)
    message = _message("/blocklist")

    await handlers.cmd_blocklist(message)

    assert message.answer.await_count == 1
    text = message.answer.await_args.args[0]
    assert text.startswith("🚫 <b>Заблокированные пользователи: 3</b>")
    assert re.findall(r"• <code>(\d+)</code>", text) == ["12", "300", "987654321"]
    assert text.count(_EXPECTED_BLOCKLIST_HINT) == 1
    list_blocked.assert_called_once_with()
    add.assert_not_awaited()
    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocklist_splits_at_id_boundaries_and_hints_only_at_end(monkeypatch):
    blocked_user_ids = set(range(1, 1001))
    monkeypatch.setattr(
        handlers,
        "list_blocked_users",
        MagicMock(return_value=blocked_user_ids),
    )
    message = _message("/blocklist")

    await handlers.cmd_blocklist(message)

    calls = message.answer.await_args_list
    assert len(calls) > 1
    texts = [call.args[0] for call in calls]
    assert all(len(text) <= _EXPECTED_TELEGRAM_MESSAGE_LIMIT for text in texts)
    assert all(call.kwargs == {"parse_mode": ParseMode.HTML} for call in calls)
    assert texts[0].startswith(
        "🚫 <b>Заблокированные пользователи: 1000</b>"
    )
    assert sum(text.count(_EXPECTED_BLOCKLIST_HINT) for text in texts) == 1
    assert _EXPECTED_BLOCKLIST_HINT not in texts[-2]
    assert texts[-1].endswith(_EXPECTED_BLOCKLIST_HINT)
    rendered_ids = [
        int(user_id)
        for text in texts
        for user_id in re.findall(r"• <code>(\d+)</code>", text)
    ]
    assert rendered_ids == sorted(blocked_user_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        BlockedUsersStateError("private malformed detail"),
        OSError("C:/private/state/path"),
    ],
)
async def test_blocklist_storage_failure_is_stable_and_does_not_leak(monkeypatch, error):
    monkeypatch.setattr(
        handlers,
        "list_blocked_users",
        MagicMock(side_effect=error),
    )
    message = _message("/blocklist")

    await handlers.cmd_blocklist(message)

    assert message.answer.await_count == 1
    text = message.answer.await_args.args[0]
    assert text == (
        "❌ Не удалось безопасно прочитать список блокировок. "
        "Подробности записаны в лог."
    )
    assert str(error) not in text
    assert message.answer.await_args.kwargs == {"parse_mode": ParseMode.HTML}
