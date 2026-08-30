# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Центральная проверка списка блокировок до handlers и переходов FSM."""

from types import SimpleNamespace
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    Message,
)

import access_control
from storage import BlockedUsersStateError


def _message_update(user_id=777):
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=user_id) if user_id is not None else None
    message.answer = AsyncMock()
    return SimpleNamespace(message=message, callback_query=None), message


def _callback_update(user_id=777, *, data="stats:all", with_message=True):
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id) if user_id is not None else None
    callback.data = data
    callback.message = MagicMock() if with_message else None
    callback.answer = AsyncMock()
    return SimpleNamespace(message=None, callback_query=callback), callback


def _inline_update(user_id=777):
    inline_query = MagicMock(spec=InlineQuery)
    inline_query.from_user = SimpleNamespace(id=user_id) if user_id is not None else None
    inline_query.answer = AsyncMock()
    return SimpleNamespace(
        message=None,
        callback_query=None,
        inline_query=inline_query,
    ), inline_query


@pytest.mark.asyncio
async def test_blocked_message_stops_before_handler_and_uses_safe_html_denial(monkeypatch):
    monkeypatch.setattr(access_control, "is_user_blocked", lambda user_id: True)
    update, message = _message_update()
    handler = AsyncMock()

    result = await access_control.AccessControlMiddleware()(handler, update, {})

    assert result is None
    handler.assert_not_awaited()
    message.answer.assert_awaited_once_with(
        access_control.ACCESS_DENIED_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["stats:all", "forged", None])
async def test_blocked_stale_or_malformed_callback_gets_alert_without_edit(
    monkeypatch,
    data,
):
    monkeypatch.setattr(access_control, "is_user_blocked", lambda user_id: True)
    update, callback = _callback_update(data=data)
    handler = AsyncMock()

    await access_control.AccessControlMiddleware()(handler, update, {})

    handler.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        access_control.ACCESS_DENIED_TEXT,
        show_alert=True,
    )
    callback.message.edit_text.assert_not_called()
    callback.message.edit_reply_markup.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_callback_without_message_is_still_denied(monkeypatch):
    monkeypatch.setattr(access_control, "is_user_blocked", lambda user_id: True)
    update, callback = _callback_update(with_message=False)

    await access_control.AccessControlMiddleware()(AsyncMock(), update, {})

    callback.answer.assert_awaited_once_with(
        access_control.ACCESS_DENIED_TEXT,
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_blocked_inline_query_gets_immediate_empty_uncached_answer(monkeypatch):
    monkeypatch.setattr(access_control, "is_user_blocked", lambda user_id: True)
    update, inline_query = _inline_update()
    handler = AsyncMock()

    await access_control.AccessControlMiddleware()(handler, update, {})

    handler.assert_not_awaited()
    inline_query.answer.assert_awaited_once_with([], cache_time=0)


@pytest.mark.asyncio
async def test_blocked_update_cannot_trigger_fsm_transition_or_side_effect(monkeypatch):
    monkeypatch.setattr(access_control, "is_user_blocked", lambda user_id: True)
    update, _message = _message_update()
    state = AsyncMock()
    side_effect = AsyncMock()

    async def handler(event, data):
        await state.set_state("forbidden")
        await side_effect()

    await access_control.AccessControlMiddleware()(handler, update, {})

    state.set_state.assert_not_awaited()
    side_effect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_kind", ["message", "callback", "inline"])
@pytest.mark.parametrize("state_kind", ["blocked", "malformed"])
async def test_fact_events_stop_before_selection_for_blocked_or_malformed_state(
    monkeypatch,
    event_kind,
    state_kind,
):
    if state_kind == "blocked":
        check = MagicMock(return_value=True)
    else:
        check = MagicMock(side_effect=BlockedUsersStateError("broken"))
    monkeypatch.setattr(access_control, "is_user_blocked", check)
    if event_kind == "message":
        update, event = _message_update()
    elif event_kind == "callback":
        update, event = _callback_update(data="fact:next:anime-word")
    else:
        update, event = _inline_update()
    selector = MagicMock()

    async def handler(_event, _data):
        selector()

    await access_control.AccessControlMiddleware()(handler, update, {})

    selector.assert_not_called()
    event.answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [777, access_control.OWNER_ID])
async def test_ordinary_user_and_owner_reach_handler(monkeypatch, user_id):
    check = MagicMock(return_value=False)
    monkeypatch.setattr(access_control, "is_user_blocked", check)
    update, _message = _message_update(user_id)
    handler = AsyncMock(return_value="ok")

    assert await access_control.AccessControlMiddleware()(handler, update, {}) == "ok"

    handler.assert_awaited_once_with(update, {})
    if user_id == access_control.OWNER_ID:
        check.assert_not_called()
    else:
        check.assert_called_once_with(user_id)


@pytest.mark.asyncio
async def test_corrupted_blocked_users_state_fails_closed_for_non_owner(monkeypatch):
    def broken(_user_id):
        raise BlockedUsersStateError("secret local path")

    monkeypatch.setattr(access_control, "is_user_blocked", broken)
    update, message = _message_update()
    handler = AsyncMock()

    await access_control.AccessControlMiddleware()(handler, update, {})

    handler.assert_not_awaited()
    assert "secret" not in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_missing_sender_or_supported_event_does_not_crash(monkeypatch):
    check = MagicMock()
    monkeypatch.setattr(access_control, "is_user_blocked", check)
    update, message = _message_update(user_id=None)
    handler = AsyncMock(return_value="passed")

    assert await access_control.AccessControlMiddleware()(handler, update, {}) is None
    handler.assert_not_awaited()
    message.answer.assert_not_awaited()

    unsupported = SimpleNamespace(message=None, callback_query=None)
    assert (
        await access_control.AccessControlMiddleware()(handler, unsupported, {})
        == "passed"
    )
    check.assert_not_called()


def test_inline_entitlement_uses_owner_or_matching_personal_subscription(monkeypatch):
    blocked = MagicMock(return_value=False)
    monkeypatch.setattr(access_control, "is_user_blocked", blocked)
    monkeypatch.setattr(
        access_control,
        "load_subscribers",
        lambda: {777: "Private", -100777: "Group"},
    )

    assert access_control.inline_access_status(access_control.OWNER_ID) == (
        access_control.INLINE_ACCESS_ALLOWED
    )
    assert access_control.inline_access_status(777) == access_control.INLINE_ACCESS_ALLOWED
    assert access_control.inline_access_status(100777) == (
        access_control.INLINE_ACCESS_UNSUBSCRIBED
    )


def test_inline_entitlement_reflects_unsubscribe_on_next_check(monkeypatch):
    subscribers = {777: "Private"}
    monkeypatch.setattr(access_control, "is_user_blocked", lambda _user_id: False)
    monkeypatch.setattr(access_control, "load_subscribers", lambda: subscribers.copy())

    assert access_control.inline_access_status(777) == access_control.INLINE_ACCESS_ALLOWED
    subscribers.clear()
    assert access_control.inline_access_status(777) == (
        access_control.INLINE_ACCESS_UNSUBSCRIBED
    )


def test_inline_entitlement_fails_closed_on_malformed_blacklist(monkeypatch):
    def broken(_user_id):
        raise BlockedUsersStateError("broken")

    load = MagicMock(return_value={777: "Private"})
    monkeypatch.setattr(access_control, "is_user_blocked", broken)
    monkeypatch.setattr(access_control, "load_subscribers", load)

    assert access_control.inline_access_status(777) == access_control.INLINE_ACCESS_BLOCKED
    load.assert_not_called()
