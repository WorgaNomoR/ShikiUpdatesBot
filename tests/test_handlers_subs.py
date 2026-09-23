# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Подписка через единое меню и hidden compatibility-команды."""

from types import SimpleNamespace
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.enums import (
    ChatType,
    ParseMode,
)
from aiogram.exceptions import TelegramBadRequest

import handlers
import storage


class _State:
    def __init__(self, state=None, data=None):
        self.state = state
        self.data = dict(data or {})

    async def get_state(self):
        return self.state

    async def set_state(self, state):
        self.state = state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)

    async def clear(self):
        self.state = None
        self.data = {}


def _control(*, chat_id=555, message_id=200, chat_type=ChatType.PRIVATE):
    control = MagicMock()
    control.chat.id = chat_id
    control.chat.type = chat_type
    control.message_id = message_id
    control.bot = AsyncMock()
    control.photo = [MagicMock(file_id="telegram-menu")]
    control.edit_caption = AsyncMock(return_value=control)
    control.edit_media = AsyncMock(return_value=control)
    control.edit_text = AsyncMock()
    control.delete = AsyncMock()
    control.edit_reply_markup = AsyncMock()
    control.reply_to_message = None
    return control


def _message(
    *,
    user_id=555,
    chat_id=555,
    chat_type=ChatType.PRIVATE,
    control=None,
):
    message = MagicMock()
    message.from_user = MagicMock(id=user_id, full_name="<Neo & Trinity>")
    message.chat.id = chat_id
    message.chat.type = chat_type
    message.message_id = 100
    message.bot = AsyncMock()
    message.answer = AsyncMock()
    message.reply = AsyncMock(
        return_value=control
        or _control(chat_id=chat_id, chat_type=chat_type)
    )
    message.reply_photo = AsyncMock(
        return_value=control
        or _control(chat_id=chat_id, chat_type=chat_type)
    )
    return message


def _callback(
    action,
    state,
    *,
    user_id=555,
    chat_id=555,
    chat_type=ChatType.PRIVATE,
):
    callback = MagicMock()
    callback.data = action
    callback.from_user = MagicMock(id=user_id, full_name="<Neo & Trinity>")
    callback.message = _control(
        chat_id=chat_id,
        message_id=state.data["main_menu_message_id"],
        chat_type=chat_type,
    )
    callback.answer = AsyncMock()
    return callback


def _callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


@pytest.mark.asyncio
async def test_cmd_subs_rejects_non_owner(monkeypatch):
    load = MagicMock(return_value={1: "X"})
    monkeypatch.setattr(handlers, "load_subscribers", load)
    message = _message(user_id=handlers.OWNER_ID + 1)

    await handlers.cmd_subs(message)

    assert "владельца" in message.answer.await_args.args[0]
    load.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_subs_empty_list(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    message = _message(user_id=handlers.OWNER_ID)

    await handlers.cmd_subs(message)

    assert "нет" in message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_cmd_subs_lists_and_escapes_all_subscribers(monkeypatch):
    monkeypatch.setattr(
        handlers,
        "load_subscribers",
        lambda: {111: "<b>A&B</b>", 222: "Bob"},
    )
    message = _message(user_id=handlers.OWNER_ID)

    await handlers.cmd_subs(message)

    text = message.answer.await_args.args[0]
    assert "<b>2</b>" in text
    assert "&lt;b&gt;A&amp;B&lt;/b&gt;" in text
    assert "<b>A&B</b>" not in text
    assert message.answer.await_args.kwargs["parse_mode"] == ParseMode.HTML


@pytest.mark.asyncio
async def test_plain_start_opens_home_without_mutation_or_backup(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    mutate = AsyncMock()
    backup = AsyncMock()
    monkeypatch.setattr(handlers, "mutate_subscription", mutate)
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)
    message = _message()
    state = _State()

    await handlers.cmd_start(message, state=state)

    message.reply_photo.assert_awaited_once()
    markup = message.reply_photo.await_args.kwargs["reply_markup"]
    assert _callbacks(markup)[:2] == [
        "menu:inline_search",
        "menu:subscription",
    ]
    text = message.reply_photo.await_args.kwargs["caption"]
    assert "Профиль Shikimori прямо в Telegram" in text
    assert "<code>а Фрирен</code>" in text
    assert "menu:owner" not in _callbacks(markup)
    assert handlers._main_menu_state_is_active(state.state)
    assert state.data["main_menu_screen"] == "home"
    mutate.assert_not_awaited()
    backup.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_falls_back_to_complete_text_when_photo_is_rejected(
    monkeypatch,
):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    handlers._main_menu_file_ids.clear()
    message = _message()
    message.reply_photo.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="wrong file identifier",
    )
    state = _State()

    await handlers.cmd_start(message, state=state)

    message.reply.assert_awaited_once()
    assert "Профиль Shikimori прямо в Telegram" in (
        message.reply.await_args.args[0]
    )
    assert state.data["main_menu_screen"] == "home"


@pytest.mark.asyncio
async def test_owner_start_rearms_before_failed_menu_read(monkeypatch):
    order = []
    monkeypatch.setattr(
        handlers,
        "start_polling_loop",
        MagicMock(side_effect=lambda _bot: order.append("polling") or True),
    )

    def fail_load():
        order.append("load")
        raise OSError("broken")

    monkeypatch.setattr(handlers, "load_subscribers", fail_load)
    message = _message(user_id=handlers.OWNER_ID, chat_id=handlers.OWNER_ID)

    await handlers.cmd_start(message, state=_State())

    assert order == ["polling", "load"]
    assert "состояние подписки недоступно" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_stop_opens_confirmation_without_mutation(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {555: "Neo"})
    mutate = AsyncMock()
    backup = AsyncMock()
    monkeypatch.setattr(handlers, "mutate_subscription", mutate)
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)
    message = _message()
    state = _State()

    await handlers.cmd_stop(message, state)

    assert state.data["main_menu_screen"] == "subscription"
    assert state.data["main_menu_target_subscription"] is False
    assert "menu:subscription:confirm:off" in _callbacks(
        message.reply_photo.await_args.kwargs["reply_markup"]
    )
    mutate.assert_not_awaited()
    backup.assert_not_awaited()


@pytest.mark.asyncio
async def test_back_from_confirmation_is_read_only(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    mutate = AsyncMock()
    backup = AsyncMock()
    monkeypatch.setattr(handlers, "mutate_subscription", mutate)
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)
    message = _message()
    state = _State()
    await handlers.cmd_stop(message, state)
    callback = _callback("menu:home", state)

    await handlers.main_menu_cb(callback, state)

    assert state.data["main_menu_screen"] == "home"
    mutate.assert_not_awaited()
    backup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed", "backup_calls"),
    [(True, 1), (False, 0)],
)
async def test_confirm_mutates_and_schedules_only_for_real_change(
    monkeypatch,
    changed,
    backup_calls,
):
    subscriber_state = {}

    def load():
        return dict(subscriber_state)

    async def mutate(_chat_id, _name, *, subscribed):
        if changed and subscribed:
            subscriber_state[555] = "Neo"
        return storage.SubscriptionMutation(changed, len(subscriber_state))

    monkeypatch.setattr(handlers, "load_subscribers", load)
    monkeypatch.setattr(handlers, "mutate_subscription", AsyncMock(side_effect=mutate))
    backup = AsyncMock()
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)
    message = _message()
    state = _State()
    await handlers.cmd_start(message, state=state)
    open_subscription = _callback("menu:subscription", state)
    await handlers.main_menu_cb(open_subscription, state)
    confirm = _callback("menu:subscription:confirm:on", state)

    await handlers.main_menu_cb(confirm, state)

    handlers.mutate_subscription.assert_awaited_once_with(
        555,
        "<Neo & Trinity>",
        subscribed=True,
    )
    assert backup.await_count == backup_calls
    assert state.data["main_menu_screen"] == "home"


@pytest.mark.asyncio
async def test_confirm_mutation_failure_keeps_confirmation_and_skips_backup(
    monkeypatch,
):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    mutate = AsyncMock(side_effect=RuntimeError("storage unavailable"))
    backup = AsyncMock()
    monkeypatch.setattr(handlers, "mutate_subscription", mutate)
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)
    message = _message()
    state = _State()
    await handlers.cmd_start(message, state=state)
    open_subscription = _callback("menu:subscription", state)
    await handlers.main_menu_cb(open_subscription, state)
    confirm = _callback("menu:subscription:confirm:on", state)

    await handlers.main_menu_cb(confirm, state)

    confirm.answer.assert_awaited_once_with(
        "Не удалось изменить подписку. Попробуй позже.",
        show_alert=True,
    )
    assert state.data["main_menu_screen"] == "subscription"
    backup.assert_not_awaited()


@pytest.mark.asyncio
async def test_backup_failure_is_contained_after_subscription_ui_reaches_home(
    monkeypatch,
):
    subscriber_state = {}

    def load():
        return dict(subscriber_state)

    async def mutate(_chat_id, name, *, subscribed):
        subscriber_state[555] = name
        return storage.SubscriptionMutation(subscribed, len(subscriber_state))

    state = _State()
    monkeypatch.setattr(handlers, "load_subscribers", load)
    monkeypatch.setattr(handlers, "mutate_subscription", AsyncMock(side_effect=mutate))

    async def failed_backup(_bot):
        assert state.data["main_menu_screen"] == "home"
        raise RuntimeError("backup unavailable")

    backup = AsyncMock(side_effect=failed_backup)
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)
    message = _message()
    await handlers.cmd_start(message, state=state)
    open_subscription = _callback("menu:subscription", state)
    await handlers.main_menu_cb(open_subscription, state)
    confirm = _callback("menu:subscription:confirm:on", state)

    await handlers.main_menu_cb(confirm, state)

    confirm.answer.assert_awaited_once_with()
    assert state.data["main_menu_screen"] == "home"
    backup.assert_awaited_once_with(confirm.message.bot)


@pytest.mark.asyncio
async def test_confirm_unsubscribe_mutates_and_schedules_backup(monkeypatch):
    subscriber_state = {555: "Neo"}

    def load():
        return dict(subscriber_state)

    async def mutate(_chat_id, _name, *, subscribed):
        assert subscribed is False
        subscriber_state.pop(555)
        return storage.SubscriptionMutation(True, 0)

    monkeypatch.setattr(handlers, "load_subscribers", load)
    monkeypatch.setattr(handlers, "mutate_subscription", AsyncMock(side_effect=mutate))
    backup = AsyncMock()
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)
    message = _message()
    state = _State()
    await handlers.cmd_stop(message, state)
    confirm = _callback("menu:subscription:confirm:off", state)

    await handlers.main_menu_cb(confirm, state)

    handlers.mutate_subscription.assert_awaited_once_with(
        555,
        "<Neo & Trinity>",
        subscribed=False,
    )
    backup.assert_awaited_once_with(confirm.message.bot)
    assert state.data["main_menu_screen"] == "home"


@pytest.mark.asyncio
async def test_group_subscription_uses_chat_title_instead_of_actor_name(
    monkeypatch,
):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    mutate = AsyncMock(return_value=storage.SubscriptionMutation(True, 1))
    monkeypatch.setattr(handlers, "mutate_subscription", mutate)
    monkeypatch.setattr(handlers, "_backup_after_subscription", AsyncMock())
    message = _message(
        user_id=555,
        chat_id=-100,
        chat_type=ChatType.SUPERGROUP,
    )
    state = _State()
    await handlers.cmd_start(message, state=state)
    open_subscription = _callback(
        "menu:subscription",
        state,
        user_id=555,
        chat_id=-100,
        chat_type=ChatType.SUPERGROUP,
    )
    await handlers.main_menu_cb(open_subscription, state)
    confirm = _callback(
        "menu:subscription:confirm:on",
        state,
        user_id=555,
        chat_id=-100,
        chat_type=ChatType.SUPERGROUP,
    )
    confirm.message.chat.title = "<Anime Club>"

    await handlers.main_menu_cb(confirm, state)

    mutate.assert_awaited_once_with(
        -100,
        "<Anime Club>",
        subscribed=True,
    )


@pytest.mark.asyncio
async def test_info_and_limit_deep_links_remain_read_only(monkeypatch):
    send_info = AsyncMock()
    mutate = AsyncMock()
    backup = AsyncMock()
    polling = MagicMock()
    monkeypatch.setattr(handlers, "_send_info", send_info)
    monkeypatch.setattr(handlers, "mutate_subscription", mutate)
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)
    monkeypatch.setattr(handlers, "start_polling_loop", polling)

    info = _message(user_id=handlers.OWNER_ID, chat_id=handlers.OWNER_ID)
    await handlers.cmd_start(
        info,
        SimpleNamespace(args="info"),
        _State(),
    )
    limit = _message()
    await handlers.cmd_start(
        limit,
        SimpleNamespace(args="inline_search_limit"),
        _State(),
    )

    send_info.assert_awaited_once_with(info)
    assert "Shikimori попросил сделать паузу" in limit.answer.await_args.args[0]
    mutate.assert_not_awaited()
    backup.assert_not_awaited()
    polling.assert_not_called()


@pytest.mark.asyncio
async def test_inline_search_subscriber_returns_without_mutation(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {555: "Neo"})
    mutate = AsyncMock()
    monkeypatch.setattr(handlers, "mutate_subscription", mutate)
    message = _message()

    await handlers.cmd_start(
        message,
        SimpleNamespace(args="inline_search"),
        _State(),
    )

    button = message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.switch_inline_query == ""
    mutate.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_search_owner_bypasses_subscription(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    message = _message(user_id=handlers.OWNER_ID, chat_id=handlers.OWNER_ID)
    state = _State()

    await handlers.cmd_start(
        message,
        SimpleNamespace(args="inline_search"),
        state,
    )

    button = message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.switch_inline_query == ""
    message.reply_photo.assert_not_awaited()
    assert state.state is None
    assert state.data == {}


@pytest.mark.asyncio
async def test_inline_search_unsubscribed_requires_confirmation(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    mutate = AsyncMock()
    monkeypatch.setattr(handlers, "mutate_subscription", mutate)
    message = _message()
    state = _State()

    await handlers.cmd_start(
        message,
        SimpleNamespace(args="inline_search"),
        state,
    )

    assert state.data["main_menu_origin"] == "inline_search"
    assert state.data["main_menu_target_subscription"] is True
    mutate.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_search_confirmation_removes_start_but_keeps_return(
    monkeypatch,
):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    monkeypatch.setattr(
        handlers,
        "mutate_subscription",
        AsyncMock(return_value=storage.SubscriptionMutation(True, 1)),
    )
    monkeypatch.setattr(handlers, "_backup_after_subscription", AsyncMock())
    message = _message()
    state = _State()
    await handlers.cmd_start(
        message,
        SimpleNamespace(args="inline_search"),
        state,
    )
    callback = _callback("menu:subscription:confirm:on", state)
    command = MagicMock()
    command.delete = AsyncMock()
    callback.message.reply_to_message = command

    await handlers.main_menu_cb(callback, state)

    assert state.state is None
    command.delete.assert_awaited_once_with()
    callback.message.delete.assert_not_awaited()
    markup = callback.message.edit_caption.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].switch_inline_query == ""


@pytest.mark.asyncio
async def test_group_deep_link_falls_back_to_home_without_mutation(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {-100: "Group"})
    mutate = AsyncMock()
    polling = MagicMock()
    monkeypatch.setattr(handlers, "mutate_subscription", mutate)
    monkeypatch.setattr(handlers, "start_polling_loop", polling)
    message = _message(
        chat_id=-100,
        chat_type=ChatType.SUPERGROUP,
    )
    message.bot.me = AsyncMock(
        return_value=SimpleNamespace(username="WorgaTestBot"),
    )
    state = _State()

    await handlers.cmd_start(
        message,
        SimpleNamespace(args="inline_search"),
        state,
    )

    assert state.data["main_menu_screen"] == "home"
    search = message.reply_photo.await_args.kwargs[
        "reply_markup"
    ].inline_keyboard[0][0]
    assert search.switch_inline_query is None
    assert search.url == "https://t.me/WorgaTestBot?start=inline_search"
    mutate.assert_not_awaited()
    polling.assert_not_called()
