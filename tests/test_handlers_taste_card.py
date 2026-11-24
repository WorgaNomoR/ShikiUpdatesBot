# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Публичная навигация карточки, session guards и жизненный цикл прогресса."""

from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat

import handlers
from taste_card_delivery import TasteDeliveryResult


class _State:
    def __init__(self, screen="stats"):
        self.state = handlers.MainMenuStates.active.state
        self.data = {
            "main_menu_user_id": 777, "main_menu_chat_id": 55,
            "main_menu_message_id": 200, "main_menu_screen": screen,
            "main_menu_origin": "stats",
        }

    async def get_state(self):
        return self.state

    async def set_state(self, value):
        self.state = value

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.state = None
        self.data.clear()


def _callback(action):
    callback = MagicMock()
    callback.data = action
    callback.from_user.id = 777
    callback.answer = AsyncMock()
    message = callback.message
    message.chat = Chat(id=55, type=ChatType.PRIVATE)
    message.message_id = 200
    message.photo = []
    message.delete = AsyncMock()
    message.edit_text = AsyncMock(return_value=message)
    message.edit_caption = AsyncMock(return_value=message)
    message.edit_reply_markup = AsyncMock(return_value=message)
    message.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_category_menu_and_back_are_public_and_do_not_read_subscriptions(monkeypatch):
    state = _State()
    callback = _callback("menu:stats:all")
    monkeypatch.setattr("handlers.load_subscribers", lambda: pytest.fail("subscription read"))
    await handlers.main_menu_cb(callback, state)
    assert state.data["main_menu_screen"] == "stats:all"
    keyboard = callback.message.edit_text.await_args.kwargs["reply_markup"]
    buttons = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert buttons == {"menu:stats:short", "menu:stats:detail", "menu:stats"}
    callback.data = "menu:stats:short"
    await handlers.main_menu_cb(callback, state)
    assert state.data["main_menu_screen"] == "stats:short"
    keyboard = callback.message.edit_text.await_args.kwargs["reply_markup"]
    buttons = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert buttons == {"menu:stats:short:anime", "menu:stats:short:manga",
                       "menu:stats:short:ranobe", "menu:stats:all"}
    callback.data = "menu:stats:all"
    await handlers.main_menu_cb(callback, state)
    assert state.data["main_menu_screen"] == "stats:all"
    callback.data = "menu:stats"
    await handlers.main_menu_cb(callback, state)
    assert state.data["main_menu_screen"] == "stats"


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["anime", "manga", "ranobe"])
async def test_selection_is_consumed_before_work_and_replay_is_inert(monkeypatch, category):
    state = _State("stats:short")
    callback = _callback(f"menu:stats:short:{category}")
    monkeypatch.setattr("handlers._cleanup_inline_command", AsyncMock())
    monkeypatch.setattr("handlers.load_subscribers", lambda: pytest.fail("subscription read"))

    async def deliver(*args):
        assert state.state is None
        assert "карточку статистики" in callback.message.edit_text.await_args.args[0]
        callback.message.delete.assert_not_awaited()
        return TasteDeliveryResult("sent")

    delivery = AsyncMock(side_effect=deliver)
    monkeypatch.setattr("handlers.deliver_taste_card", delivery)
    await handlers.main_menu_cb(callback, state)
    delivery.assert_awaited_once_with(callback.message.bot, 55, category)
    callback.message.delete.assert_awaited_once()
    await handlers.main_menu_cb(callback, state)
    delivery.assert_awaited_once()
    assert "Меню устарело" in callback.answer.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["sender", "chat", "control", "expired", "screen", "category"])
async def test_invalid_controls_cannot_read_data_or_change_session(monkeypatch, invalid):
    state = _State("stats:short")
    callback = _callback("menu:stats:short:anime")
    if invalid == "sender":
        callback.from_user.id = 778
    elif invalid == "chat":
        callback.message.chat = Chat(id=56, type=ChatType.PRIVATE)
    elif invalid == "control":
        callback.message.message_id = 201
    elif invalid == "expired":
        state.state = None
    elif invalid == "screen":
        state.data["main_menu_screen"] = "stats"
    else:
        callback.data = "menu:stats:short:unknown"
    before_state = state.state
    before_data = state.data.copy()
    delivery = AsyncMock()
    monkeypatch.setattr("handlers.deliver_taste_card", delivery)
    await handlers.main_menu_cb(callback, state)
    assert state.state == before_state
    assert state.data == before_data
    delivery.assert_not_awaited()
    callback.message.delete.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_progress_failure_does_not_suppress_delivery(monkeypatch):
    callback = _callback("menu:stats:short:anime")
    callback.message.edit_text.side_effect = RuntimeError("edit failed")
    callback.message.answer.side_effect = RuntimeError("progress failed")
    monkeypatch.setattr("handlers._cleanup_inline_command", AsyncMock())
    delivery = AsyncMock(return_value=TasteDeliveryResult("sent"))
    monkeypatch.setattr("handlers.deliver_taste_card", delivery)
    await handlers.main_menu_cb(callback, _State("stats:short"))
    delivery.assert_awaited_once()
    callback.message.edit_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_acknowledgement_failure_does_not_suppress_selected_card(monkeypatch):
    callback = _callback("menu:stats:short:anime")
    callback.answer.side_effect = RuntimeError("expired callback")
    monkeypatch.setattr("handlers._cleanup_inline_command", AsyncMock())
    delivery = AsyncMock(return_value=TasteDeliveryResult("sent"))
    monkeypatch.setattr("handlers.deliver_taste_card", delivery)
    state = _State("stats:short")
    await handlers.main_menu_cb(callback, state)
    delivery.assert_awaited_once()
    assert state.state is None
    callback.message.delete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [False, True])
async def test_category_picker_reuses_statistics_artwork_and_has_text_fallback(monkeypatch, reject):
    state = _State("stats:all")
    callback = _callback("menu:stats:short")
    callback.message.photo = [MagicMock(file_id="cached-stats")]
    callback.message.edit_media = AsyncMock(return_value=callback.message)
    callback.message.bot = AsyncMock()
    callback.message.bot.send_message.return_value = MagicMock(message_id=201)
    monkeypatch.setattr("handlers._main_menu_file_ids", {"stats": "cached-stats"})
    asset_dir = MagicMock()
    monkeypatch.setattr("handlers.MAIN_MENU_ASSET_DIR", asset_dir)
    if reject:
        callback.message.edit_media.side_effect = TelegramBadRequest(
            method=MagicMock(), message="wrong file identifier",
        )

    await handlers.main_menu_cb(callback, state)
    asset_dir.__truediv__.assert_not_called()
    assert state.data["main_menu_screen"] == "stats:short"
    assert callback.message.edit_media.await_args.kwargs["media"].media == "cached-stats"
    if reject:
        control = callback.message.bot.send_message.await_args
        assert "Краткая карточка" in control.args[1]
        assert "Выбери категорию" in control.args[1]
        assert state.data["main_menu_message_id"] == 201
        assert "stats" not in handlers._main_menu_file_ids
        callback.message.bot.delete_message.assert_awaited_once_with(55, 200)
    else:
        callback.message.bot.send_message.assert_not_awaited()
        assert handlers._main_menu_file_ids["stats"] == "cached-stats"
        control = callback.message.edit_media.await_args
    buttons = {
        button.callback_data
        for row in control.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert buttons == {"menu:stats:short:anime", "menu:stats:short:manga",
                       "menu:stats:short:ranobe", "menu:stats:all"}


@pytest.mark.asyncio
async def test_detailed_report_retains_existing_builder_and_validates_screen(monkeypatch):
    callback = _callback("menu:stats:detail")
    delivery = AsyncMock()
    monkeypatch.setattr("handlers._deliver_main_report", delivery)
    await handlers.main_menu_cb(callback, _State("stats"))
    delivery.assert_not_awaited()
    state = _State("stats:all")
    await handlers.main_menu_cb(callback, state)
    delivery.assert_awaited_once_with(callback, state, handlers._stats_report_all, label="stats-all")
