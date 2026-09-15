# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Owner orchestration и reusable delivery use-case каталога пользователей."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import handlers
import storage
import user_directory_delivery
from report_delivery import ReportDeliveryResult


def _message(
    user_id: int | None,
    *,
    chat_type=handlers.ChatType.PRIVATE,
):
    return SimpleNamespace(
        from_user=(SimpleNamespace(id=user_id) if user_id is not None else None),
        chat=SimpleNamespace(id=777, type=chat_type),
        bot=SimpleNamespace(),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, 10])
async def test_cmd_users_rejects_before_directory_access(monkeypatch, user_id):
    monkeypatch.setattr(handlers, "OWNER_ID", 999)
    deliver = AsyncMock()
    monkeypatch.setattr(handlers, "deliver_user_directory", deliver)
    message = _message(user_id)

    await handlers.cmd_users(message)

    deliver.assert_not_awaited()
    message.answer.assert_awaited_once_with(
        "🚫 Эта команда только для владельца бота.",
        parse_mode=handlers.ParseMode.HTML,
    )


@pytest.mark.asyncio
async def test_cmd_users_rejects_owner_outside_private_chat(monkeypatch):
    monkeypatch.setattr(handlers, "OWNER_ID", 999)
    deliver = AsyncMock()
    monkeypatch.setattr(handlers, "deliver_user_directory", deliver)
    message = _message(999, chat_type=handlers.ChatType.GROUP)

    await handlers.cmd_users(message)

    deliver.assert_not_awaited()
    message.answer.assert_awaited_once_with(
        "🔒 Каталог пользователей доступен только в личном чате с ботом.",
        parse_mode=handlers.ParseMode.HTML,
    )


@pytest.mark.asyncio
async def test_cmd_users_delegates_owner_to_reusable_use_case(monkeypatch):
    monkeypatch.setattr(handlers, "OWNER_ID", 999)
    deliver = AsyncMock()
    monkeypatch.setattr(handlers, "deliver_user_directory", deliver)
    message = _message(999)

    await handlers.cmd_users(message)

    deliver.assert_awaited_once_with(message.bot, 777)
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["known_users", "subscribers", "blocked_users"])
async def test_delivery_use_case_reports_one_stable_source_failure(
    monkeypatch,
    source,
):
    snapshot = AsyncMock(
        side_effect=storage.UserDirectorySnapshotError(source),
    )
    deliver = AsyncMock()
    monkeypatch.setattr(
        user_directory_delivery,
        "load_user_directory_snapshot",
        snapshot,
    )
    monkeypatch.setattr(user_directory_delivery, "deliver_report", deliver)
    bot = SimpleNamespace(send_message=AsyncMock())

    result = await user_directory_delivery.deliver_user_directory(bot, 777)

    assert result is None
    deliver.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=777,
        text=user_directory_delivery.USER_DIRECTORY_STATE_FAILURE,
        parse_mode=user_directory_delivery.ParseMode.HTML,
    )


@pytest.mark.asyncio
async def test_use_case_releases_lock_before_build_and_delivery(
    backup_env,
    monkeypatch,
):
    monkeypatch.setattr(user_directory_delivery, "OWNER_ID", 999)
    storage.save_known_users({
        10: storage.KnownUser(
            10,
            "Neo",
            "the_one",
            "2026-09-03T10:20:30Z",
        ),
    })
    storage.save_subscribers({10: "Old label"})
    storage.save_blocked_users(set())
    before = {
        path: path.read_bytes()
        for path in (
            storage.KNOWN_USERS_FILE,
            storage.SUBS_FILE,
            storage.BLOCKED_USERS_FILE,
        )
    }
    original_build = user_directory_delivery.build_user_directory

    def checked_build(snapshot, *, owner_id):
        assert not storage._restorable_state_lock().locked()
        return original_build(snapshot, owner_id=owner_id)

    async def checked_deliver(_bot, _chat_id, _report, **kwargs):
        assert not storage._restorable_state_lock().locked()
        assert kwargs == {"disable_preview": True, "notify_partial": True}
        return ReportDeliveryResult(True, 1, 1, next_unit=1)

    monkeypatch.setattr(
        user_directory_delivery,
        "build_user_directory",
        checked_build,
    )
    monkeypatch.setattr(
        user_directory_delivery,
        "deliver_report",
        checked_deliver,
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    result = await user_directory_delivery.deliver_user_directory(bot, 777)

    assert result == ReportDeliveryResult(True, 1, 1, next_unit=1)
    bot.send_message.assert_not_awaited()
    assert {
        path: path.read_bytes()
        for path in before
    } == before


@pytest.mark.asyncio
async def test_use_case_preserves_delivery_failure_result(monkeypatch):
    snapshot = storage.UserDirectorySnapshot((), (), frozenset())
    monkeypatch.setattr(
        user_directory_delivery,
        "load_user_directory_snapshot",
        AsyncMock(return_value=snapshot),
    )
    failure = ReportDeliveryResult(False, 0, 1, RuntimeError("offline"))
    monkeypatch.setattr(
        user_directory_delivery,
        "deliver_report",
        AsyncMock(return_value=failure),
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    result = await user_directory_delivery.deliver_user_directory(bot, 777)

    assert result is failure
    bot.send_message.assert_not_awaited()
