# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Оркестрация публичной команды /fact и замены факта на месте."""

from types import SimpleNamespace
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.enums import ParseMode

import fact_bank
import handlers
from inline_facts import INLINE_FACTS


def _message(*, user_id=777, message_id=42, chat_type="private"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message_id=message_id,
        chat=SimpleNamespace(type=chat_type),
        answer=AsyncMock(),
    )


def _callback(
    current_id: str,
    *,
    initiator_id=777,
    user_id=777,
    chat_type="private",
):
    return SimpleNamespace(
        data=f"fact:next:{initiator_id}:{current_id}",
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        message=SimpleNamespace(
            chat=SimpleNamespace(type=chat_type),
            edit_text=AsyncMock(),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [777, handlers.OWNER_ID])
async def test_fact_is_public_for_non_subscriber_and_owner_without_entitlement(
    monkeypatch,
    user_id,
):
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        MagicMock(side_effect=AssertionError("fact прочитал подписчиков")),
    )
    message = _message(user_id=user_id)
    expected = handlers.select_fact(
        handlers._fact_message_seed(user_id, message.message_id)
    )

    await handlers.cmd_fact(message)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert text == handlers.build_fact_text(expected)
    kwargs = message.answer.await_args.kwargs
    assert kwargs["parse_mode"] == ParseMode.HTML
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert buttons[0].callback_data == f"fact:next:{user_id}:{expected.id}"
    assert buttons[1].switch_inline_query == f"fact:{expected.id}"


@pytest.mark.asyncio
async def test_next_fact_edits_one_message_in_place_and_acknowledges_callback():
    current = INLINE_FACTS[0]
    expected = INLINE_FACTS[1]
    callback = _callback(current.id)

    await handlers.fact_next_cb(callback)

    callback.answer.assert_awaited_once_with()
    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]
    assert current.text not in text
    assert expected.text in text
    kwargs = callback.message.edit_text.await_args.kwargs
    assert kwargs["parse_mode"] == ParseMode.HTML
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert buttons[0].callback_data == f"fact:next:777:{expected.id}"
    assert buttons[1].switch_inline_query == f"fact:{expected.id}"


@pytest.mark.asyncio
async def test_group_fact_cannot_be_updated_by_another_user():
    callback = _callback(
        INLINE_FACTS[0].id,
        initiator_id=777,
        user_id=888,
        chat_type="group",
    )

    await handlers.fact_next_cb(callback)

    callback.answer.assert_awaited_once_with(
        "Обновить этот факт может только тот, кто его запросил.",
        show_alert=True,
    )
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_forged_next_fact_callback_is_acknowledged_without_editing():
    callback = _callback("forged")

    await handlers.fact_next_cb(callback)

    callback.answer.assert_awaited_once_with(
        "Этот факт устарел. Отправь /fact ещё раз.",
        show_alert=True,
    )
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_and_next_button_use_combined_runtime_snapshot(fact_bank_env):
    document = fact_bank.parse_fact_bank_bytes(
        b'{"schema_version":1,"bank_version":"combined","facts":['
        b'{"id":"external-command","text":"Additional command fact."}]}'
    )
    fact_bank.activate_restored_fact_bank(document)
    message_id = None
    for candidate in range(1, 10_000):
        seed = handlers._fact_message_seed(777, candidate)
        if handlers.select_fact(seed).id == "external-command":
            message_id = candidate
            break
    if message_id is None:
        pytest.fail("не найдено зерно /fact для дополнительного факта")
    message = _message(message_id=message_id)

    await handlers.cmd_fact(message)

    assert "Additional command fact." in message.answer.await_args.args[0]

    callback = _callback(INLINE_FACTS[-1].id)
    await handlers.fact_next_cb(callback)
    assert "Additional command fact." in callback.message.edit_text.await_args.args[0]
