# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Session-bound orchestration единого меню профиля."""

from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile

import handlers
import main_menu
from report_delivery import ReportDeliveryResult
from report_model import plain_report


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


def _state(*, screen="home", user_id=777, chat_id=55, message_id=200):
    return _State(
        handlers.MainMenuStates.active,
        {
            "main_menu_user_id": user_id,
            "main_menu_chat_id": chat_id,
            "main_menu_message_id": message_id,
            "main_menu_screen": screen,
            "main_menu_origin": "start",
            "main_menu_target_subscription": None,
        },
    )


def _callback(
    action,
    *,
    user_id=777,
    chat_id=55,
    message_id=200,
    chat_type=ChatType.PRIVATE,
):
    callback = MagicMock()
    callback.data = action
    callback.from_user = MagicMock(id=user_id, full_name="User")
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.message_id = message_id
    callback.message.chat.id = chat_id
    callback.message.chat.type = chat_type
    callback.message.bot = AsyncMock()
    callback.message.photo = [MagicMock(file_id="telegram-current-menu")]
    callback.message.edit_caption = AsyncMock(return_value=callback.message)
    callback.message.edit_media = AsyncMock(return_value=callback.message)
    callback.message.edit_text = AsyncMock(return_value=callback.message)
    callback.message.answer = AsyncMock()
    callback.message.delete = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    callback.message.reply_to_message = None
    return callback


def _callbacks(call):
    markup = call.kwargs["reply_markup"]
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


@pytest.fixture(autouse=True)
def _reset_main_menu_caches():
    handlers._main_menu_file_ids.clear()
    handlers._main_menu_artwork_bytes.clear()
    yield
    handlers._main_menu_file_ids.clear()
    handlers._main_menu_artwork_bytes.clear()


def test_inline_search_entitlement_is_personal_and_owner_bypasses_subscription():
    assert handlers._inline_search_allowed(handlers.OWNER_ID, {}) is True
    assert handlers._inline_search_allowed(777, {777: "User"}) is True
    assert handlers._inline_search_allowed(777, {-100: "Group"}) is False


def test_menu_list_keys_are_backed_by_existing_list_use_cases():
    assert set(main_menu.LIST_MEDIA) <= set(handlers.LIST_MEDIA_BY_KEY)
    assert {"completed", "planned", "all"} <= set(handlers.LIST_VIEW_BY_KEY)


@pytest.mark.asyncio
async def test_lists_navigation_returns_to_each_immediate_parent(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    state = _state()
    callback = _callback("menu:lists")

    await handlers.main_menu_cb(callback, state)
    assert state.data["main_menu_screen"] == "lists"
    assert "menu:home" in _callbacks(callback.message.edit_media.await_args)

    callback.data = "menu:lists:anime"
    await handlers.main_menu_cb(callback, state)
    assert state.data["main_menu_screen"] == "lists:anime"
    assert _callbacks(callback.message.edit_media.await_args)[-1] == "menu:lists"

    callback.data = "menu:lists"
    await handlers.main_menu_cb(callback, state)
    assert state.data["main_menu_screen"] == "lists"

    callback.data = "menu:home"
    await handlers.main_menu_cb(callback, state)
    assert state.data["main_menu_screen"] == "home"
    assert callback.message.edit_media.await_count == 4


@pytest.mark.asyncio
async def test_private_search_action_reuses_subscription_confirmation(
    monkeypatch,
):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    state = _state()
    callback = _callback("menu:inline_search")

    await handlers.main_menu_cb(callback, state)

    assert state.data["main_menu_screen"] == "subscription"
    assert state.data["main_menu_target_subscription"] is True
    assert state.data["main_menu_origin"] == "inline_search"
    assert "menu:subscription:confirm:on" in _callbacks(
        callback.message.edit_media.await_args
    )

    callback.data = "menu:home"
    await handlers.main_menu_cb(callback, state)

    assert state.data["main_menu_screen"] == "home"
    assert state.data["main_menu_origin"] == "start"


@pytest.mark.asyncio
async def test_forged_group_search_callback_never_subscribes_group(monkeypatch):
    mutate = AsyncMock()
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {-100: "Group"})
    monkeypatch.setattr(handlers, "mutate_subscription", mutate)
    state = _state(user_id=777, chat_id=-100)
    callback = _callback(
        "menu:inline_search",
        user_id=777,
        chat_id=-100,
        chat_type=ChatType.SUPERGROUP,
    )

    await handlers.main_menu_cb(callback, state)

    assert state.data["main_menu_screen"] == "home"
    callback.answer.assert_awaited_once_with(
        "Открой личный чат с ботом, чтобы оформить подписку.",
        show_alert=True,
    )
    mutate.assert_not_awaited()


@pytest.mark.asyncio
async def test_forged_stale_and_wrong_screen_callbacks_are_inert(monkeypatch):
    mutate = AsyncMock()
    delivery = AsyncMock()
    users = AsyncMock()
    monkeypatch.setattr(handlers, "mutate_subscription", mutate)
    monkeypatch.setattr(handlers, "deliver_report", delivery)
    monkeypatch.setattr(handlers, "deliver_user_directory", users)

    cases = [
        (_state(), _callback("menu:stats", user_id=778)),
        (_state(), _callback("menu:stats", chat_id=56)),
        (_state(), _callback("menu:stats", message_id=201)),
        (_state(screen="stats"), _callback("menu:favs")),
        (
            _state(
                screen="stats",
                user_id=handlers.OWNER_ID,
                chat_id=handlers.OWNER_ID,
            ),
            _callback(
                "menu:owner:users",
                user_id=handlers.OWNER_ID,
                chat_id=handlers.OWNER_ID,
            ),
        ),
        (_State(), _callback("menu:lists")),
    ]
    for state, callback in cases:
        before = dict(state.data)
        await handlers.main_menu_cb(callback, state)
        assert state.data == before

    mutate.assert_not_awaited()
    delivery.assert_not_awaited()
    users.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_state", "screen"),
    [
        (handlers.BackupStates.waiting_import_file, "owner:broadcast:content"),
        (handlers.BroadcastStates.waiting_content, "owner:backup:import"),
        (handlers.BroadcastStates.waiting_content, "home"),
    ],
)
async def test_operation_state_requires_its_matching_owner_screen(
    operation_state,
    screen,
):
    state = _state(screen=screen)
    state.state = operation_state
    callback = _callback("menu:home")
    before = dict(state.data)

    await handlers.main_menu_cb(callback, state)

    callback.answer.assert_awaited_once_with(
        "Меню устарело. Отправь /start ещё раз.",
        show_alert=True,
    )
    assert state.data == before
    callback.message.edit_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_actions_require_owner_and_private_chat(monkeypatch):
    users = AsyncMock()
    monkeypatch.setattr(handlers, "deliver_user_directory", users)

    ordinary = _state(screen="owner", user_id=777)
    ordinary_callback = _callback("menu:owner:users", user_id=777)
    await handlers.main_menu_cb(
        ordinary_callback,
        ordinary,
    )
    ordinary_callback.answer.assert_awaited_once_with(
        "Только для владельца в личном чате.",
        show_alert=True,
    )
    users.assert_not_awaited()

    group = _state(
        screen="owner",
        user_id=handlers.OWNER_ID,
        chat_id=-100,
    )
    group_callback = _callback(
        "menu:owner:users",
        user_id=handlers.OWNER_ID,
        chat_id=-100,
        chat_type=ChatType.SUPERGROUP,
    )
    await handlers.main_menu_cb(
        group_callback,
        group,
    )
    group_callback.answer.assert_awaited_once_with(
        "Только для владельца в личном чате.",
        show_alert=True,
    )
    users.assert_not_awaited()


@pytest.mark.asyncio
async def test_users_terminal_delegates_after_state_and_control_cleanup(
    monkeypatch,
):
    state = _state(
        screen="owner",
        user_id=handlers.OWNER_ID,
        chat_id=handlers.OWNER_ID,
    )
    callback = _callback(
        "menu:owner:users",
        user_id=handlers.OWNER_ID,
        chat_id=handlers.OWNER_ID,
    )
    cleanup = AsyncMock()

    async def deliver(bot, chat_id):
        assert state.state is None
        cleanup.assert_awaited_once_with(callback.message)
        assert (bot, chat_id) == (
            callback.message.bot,
            handlers.OWNER_ID,
        )

    monkeypatch.setattr(handlers, "_cleanup_inline_menu", cleanup)
    monkeypatch.setattr(
        handlers,
        "deliver_user_directory",
        AsyncMock(side_effect=deliver),
    )
    monkeypatch.setattr(
        handlers,
        "load_user_directory_snapshot",
        MagicMock(side_effect=AssertionError("menu storage aggregation")),
        raising=False,
    )

    await handlers.main_menu_cb(callback, state)

    handlers.deliver_user_directory.assert_awaited_once_with(
        callback.message.bot,
        handlers.OWNER_ID,
    )


@pytest.mark.asyncio
async def test_pick_terminal_delegates_after_state_and_control_cleanup(
    monkeypatch,
):
    state = _state(
        screen="owner",
        user_id=handlers.OWNER_ID,
        chat_id=handlers.OWNER_ID,
    )
    callback = _callback(
        "menu:owner:pick",
        user_id=handlers.OWNER_ID,
        chat_id=handlers.OWNER_ID,
    )
    cleanup = AsyncMock()

    async def open_pick(message, flow_state, *, send, command_message_id):
        assert state.state is None
        cleanup.assert_awaited_once_with(callback.message)
        assert message is callback.message
        assert flow_state is state
        assert send is callback.message.answer
        assert command_message_id is None

    monkeypatch.setattr(handlers, "_cleanup_inline_menu", cleanup)
    monkeypatch.setattr(
        handlers,
        "_open_pick_menu",
        AsyncMock(side_effect=open_pick),
    )

    await handlers.main_menu_cb(callback, state)

    handlers._open_pick_menu.assert_awaited_once_with(
        callback.message,
        state,
        send=callback.message.answer,
        command_message_id=None,
    )


@pytest.mark.asyncio
async def test_facts_terminal_delegates_after_state_and_control_cleanup(
    monkeypatch,
):
    state = _state(
        screen="owner",
        user_id=handlers.OWNER_ID,
        chat_id=handlers.OWNER_ID,
    )
    callback = _callback(
        "menu:owner:facts",
        user_id=handlers.OWNER_ID,
        chat_id=handlers.OWNER_ID,
    )
    cleanup = AsyncMock()

    async def open_facts(message, flow_state, *, send):
        assert state.state is None
        cleanup.assert_awaited_once_with(callback.message)
        assert message is callback.message
        assert send is callback.message.answer
        assert flow_state is state

    monkeypatch.setattr(handlers, "_cleanup_inline_menu", cleanup)
    monkeypatch.setattr(
        handlers,
        "_open_facts_menu",
        AsyncMock(side_effect=open_facts),
    )

    await handlers.main_menu_cb(callback, state)

    handlers._open_facts_menu.assert_awaited_once_with(
        callback.message,
        state,
        send=callback.message.answer,
    )


@pytest.mark.asyncio
async def test_terminal_list_clears_and_cleans_before_existing_delivery(
    monkeypatch,
):
    state = _state(screen="lists:anime")
    callback = _callback("menu:lists:anime:completed")
    cleanup = AsyncMock()
    monkeypatch.setattr(handlers, "_cleanup_inline_menu", cleanup)
    monkeypatch.setattr(
        handlers,
        "_lists_snapshot_report",
        MagicMock(return_value=plain_report("list")),
    )

    async def deliver(*args, **kwargs):
        assert state.state is None
        cleanup.assert_awaited_once_with(callback.message)
        return ReportDeliveryResult(True, 1, 1)

    monkeypatch.setattr(
        handlers,
        "deliver_report",
        AsyncMock(side_effect=deliver),
    )

    await handlers.main_menu_cb(callback, state)

    handlers._lists_snapshot_report.assert_called_once_with(
        "anime",
        "completed",
    )
    handlers.deliver_report.assert_awaited_once()


@pytest.mark.asyncio
async def test_report_builder_callable_may_return_awaitable(monkeypatch):
    state = _state(screen="stats")
    callback = _callback("menu:stats:all")
    report = plain_report("async report")
    delivery = AsyncMock(return_value=ReportDeliveryResult(
        delivered=True,
        delivered_units=1,
        total_units=1,
    ))
    monkeypatch.setattr(handlers, "_cleanup_inline_menu", AsyncMock())
    monkeypatch.setattr(handlers, "deliver_report", delivery)

    class AwaitableBuilder:
        def __call__(self):
            async def build():
                return report

            return build()

    await handlers._deliver_main_report(
        callback,
        state,
        AwaitableBuilder(),
        label="awaitable-builder",
    )

    delivery.assert_awaited_once_with(
        callback.message.bot,
        callback.message.chat.id,
        report,
        disable_preview=False,
        notify_partial=True,
    )


@pytest.mark.asyncio
async def test_backup_import_and_back_reuse_existing_fsm(monkeypatch):
    state = _state(
        screen="owner:backup",
        user_id=handlers.OWNER_ID,
        chat_id=handlers.OWNER_ID,
    )
    callback = _callback(
        "menu:owner:backup:import",
        user_id=handlers.OWNER_ID,
        chat_id=handlers.OWNER_ID,
    )

    await handlers.main_menu_cb(callback, state)

    assert state.state == handlers.BackupStates.waiting_import_file
    assert state.data["main_menu_screen"] == "owner:backup:import"
    assert _callbacks(callback.message.edit_caption.await_args) == [
        "menu:owner:backup"
    ]

    callback.data = "menu:owner:backup"
    await handlers.main_menu_cb(callback, state)

    assert handlers._main_menu_state_is_active(state.state)
    assert state.data["main_menu_screen"] == "owner:backup"


@pytest.mark.asyncio
async def test_rejected_backup_import_prompt_keeps_backup_screen():
    state = _state(
        screen="owner:backup",
        user_id=handlers.OWNER_ID,
        chat_id=handlers.OWNER_ID,
    )
    callback = _callback(
        "menu:owner:backup:import",
        user_id=handlers.OWNER_ID,
        chat_id=handlers.OWNER_ID,
    )
    callback.message.edit_caption.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="message can't be edited",
    )

    await handlers.main_menu_cb(callback, state)

    assert handlers._main_menu_state_is_active(state.state)
    assert state.data["main_menu_screen"] == "owner:backup"


@pytest.mark.asyncio
async def test_photo_navigation_uploads_once_and_caches_telegram_file_id(
    monkeypatch,
):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    state = _state()
    callback = _callback("menu:stats")
    callback.message.photo[-1].file_id = "telegram-stats-v1"

    await handlers.main_menu_cb(callback, state)

    media = callback.message.edit_media.await_args.kwargs["media"]
    assert isinstance(media.media, BufferedInputFile)
    assert media.caption is None
    assert handlers._main_menu_file_ids["stats"] == "telegram-stats-v1"

    callback.data = "menu:home"
    callback.message.photo[-1].file_id = "telegram-home-v1"
    await handlers.main_menu_cb(callback, state)
    callback.data = "menu:stats"
    await handlers.main_menu_cb(callback, state)

    media = callback.message.edit_media.await_args.kwargs["media"]
    assert media.media == "telegram-stats-v1"


def test_artwork_bytes_are_read_once_before_telegram_file_id(monkeypatch):
    asset_path = MagicMock()
    asset_path.read_bytes.return_value = b"artwork"
    asset_dir = MagicMock()
    asset_dir.__truediv__.return_value = asset_path
    monkeypatch.setattr(handlers, "MAIN_MENU_ASSET_DIR", asset_dir)

    first = handlers._load_main_menu_artwork(main_menu.stats_view())
    second = handlers._load_main_menu_artwork(main_menu.stats_view())

    assert isinstance(first, BufferedInputFile)
    assert isinstance(second, BufferedInputFile)
    assert first.data == b"artwork"
    assert second.data == b"artwork"
    asset_path.read_bytes.assert_called_once_with()


@pytest.mark.asyncio
async def test_rejected_navigation_artwork_replaces_control_with_text(
    monkeypatch,
):
    state = _state()
    callback = _callback("menu:stats")
    callback.message.edit_media.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="wrong file identifier",
    )
    replacement = MagicMock(message_id=201)
    callback.message.bot.send_message.return_value = replacement

    await handlers.main_menu_cb(callback, state)

    callback.message.bot.send_message.assert_awaited_once()
    assert "Какую статистику" in (
        callback.message.bot.send_message.await_args.args[1]
    )
    callback.message.bot.delete_message.assert_awaited_once_with(55, 200)
    assert state.data["main_menu_message_id"] == 201
    assert state.data["main_menu_screen"] == "stats"


@pytest.mark.asyncio
async def test_unchanged_navigation_keeps_photo_control_and_cache():
    handlers._main_menu_file_ids["stats"] = "telegram-stats-v1"
    state = _state()
    callback = _callback("menu:stats")
    callback.message.edit_media.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="Bad Request: message is not modified",
    )

    await handlers.main_menu_cb(callback, state)

    callback.message.bot.send_message.assert_not_awaited()
    callback.message.bot.delete_message.assert_not_awaited()
    assert handlers._main_menu_file_ids["stats"] == "telegram-stats-v1"
    assert state.data["main_menu_message_id"] == 200
    assert state.data["main_menu_screen"] == "stats"
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_unchanged_text_navigation_keeps_existing_control():
    state = _state()
    callback = _callback("menu:stats")
    callback.message.photo = []
    callback.message.edit_text.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="Bad Request: message is not modified",
    )

    result = await handlers._edit_main_menu_view(
        callback,
        state,
        main_menu.stats_view(),
    )

    assert result is callback.message


@pytest.mark.asyncio
@pytest.mark.parametrize("has_photo", [False, True])
async def test_unchanged_instruction_edit_returns_existing_control(has_photo):
    callback = _callback("menu:owner:broadcast:start")
    callback.message.photo = [MagicMock()] if has_photo else []
    method = (
        callback.message.edit_caption
        if has_photo
        else callback.message.edit_text
    )
    method.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="Bad Request: message is not modified",
    )

    result = await handlers._edit_main_menu_content(
        callback.message,
        "unchanged",
    )

    assert result is callback.message


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_block_read_only_terminal_action(
    monkeypatch,
):
    state = _state(screen="home")
    callback = _callback("menu:favs")
    callback.message.delete.side_effect = RuntimeError("inaccessible")
    callback.message.edit_reply_markup.side_effect = RuntimeError("inaccessible")
    monkeypatch.setattr(
        handlers,
        "_stats_report_favourites",
        AsyncMock(return_value=plain_report("favs")),
    )
    delivery = AsyncMock(return_value=ReportDeliveryResult(True, 1, 1))
    monkeypatch.setattr(handlers, "deliver_report", delivery)

    await handlers.main_menu_cb(callback, state)

    assert state.state is None
    delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_clears_main_menu_control_command_and_echo(monkeypatch):
    state = _state()
    state.data["main_menu_command_message_id"] = 100
    message = MagicMock()
    message.chat.id = 55
    message.message_id = 300
    message.bot = AsyncMock()
    message.answer = AsyncMock()
    cleanup = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_delete", cleanup)

    await handlers.cmd_cancel(message, state)

    assert state.state is None
    assert [call.args[2] for call in cleanup.await_args_list[:3]] == [
        200,
        100,
        300,
    ]
    message.answer.assert_awaited_once_with("❌ Отменено.")
