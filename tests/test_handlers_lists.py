# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Оркестрация публичного session-bound меню /lists."""

from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest

import handlers
from report_delivery import ReportDeliveryResult
from report_model import rendered_html
from storage import (
    STATS_ALL_INVALID,
    STATS_ALL_MISSING,
    STATS_ALL_VALID,
    StatsAllSnapshot,
)


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


def _menu(*, chat_id=55, message_id=200):
    menu = MagicMock()
    menu.chat.id = chat_id
    menu.message_id = message_id
    menu.bot = MagicMock()
    menu.edit_text = AsyncMock()
    menu.delete = AsyncMock()
    menu.edit_reply_markup = AsyncMock()
    menu.answer = AsyncMock()
    menu.reply_to_message = None
    return menu


def _message(*, user_id=777, chat_id=55, message_id=100, menu=None):
    message = MagicMock()
    message.from_user.id = user_id
    message.chat.id = chat_id
    message.message_id = message_id
    message.bot = MagicMock()
    message.answer = AsyncMock()
    message.reply = AsyncMock(return_value=menu or _menu(chat_id=chat_id))
    return message


def _active_state(*, user_id=777, chat_id=55, menu_id=200, media=None):
    return _State(
        handlers.ListsStates.active,
        {
            "lists_user_id": user_id,
            "lists_menu_chat_id": chat_id,
            "lists_menu_message_id": menu_id,
            "lists_media": media,
        },
    )


def _callback(data, *, user_id=777, chat_id=55, message_id=200, menu=None):
    callback = MagicMock()
    callback.data = data
    callback.from_user.id = user_id
    callback.message = menu or _menu(chat_id=chat_id, message_id=message_id)
    callback.answer = AsyncMock()
    return callback


def _empty_stats():
    return {
        "anime": {"titles": {}, "aggregates": {}},
        "manga": {"titles": {}, "aggregates": {}},
    }


@pytest.mark.asyncio
async def test_public_command_opens_registry_driven_root_without_reading_state(
    monkeypatch,
):
    load = MagicMock(side_effect=AssertionError("stats_all read on menu open"))
    monkeypatch.setattr(handlers, "load_stats_all_snapshot", load)
    menu = _menu()
    message = _message(user_id=123456, menu=menu)
    state = _State()

    await handlers.cmd_lists(message, state)

    message.reply.assert_awaited_once()
    load.assert_not_called()
    assert handlers._lists_state_is_active(state.state)
    assert state.data["lists_user_id"] == 123456
    assert state.data["lists_menu_message_id"] == 200
    callbacks = [
        button.callback_data
        for row in message.reply.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert callbacks == [
        "lists:media:anime",
        "lists:media:manga",
        "lists:media:ranobe",
        "lists:combined",
        "lists:close",
    ]


@pytest.mark.asyncio
async def test_repeated_command_replaces_only_previous_control(monkeypatch):
    state = _active_state()
    new_menu = _menu(message_id=201)
    message = _message(message_id=300, menu=new_menu)
    safe_delete = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_delete", safe_delete)

    await handlers.cmd_lists(message, state)

    safe_delete.assert_awaited_once_with(message.bot, 55, 200)
    assert state.data["lists_menu_message_id"] == 201
    assert handlers._lists_state_is_active(state.state)


@pytest.mark.asyncio
async def test_media_submenu_and_back_use_generic_registry_dispatch():
    state = _active_state()
    menu = _menu()
    choose = _callback("lists:media:manga", menu=menu)

    await handlers.lists_menu_cb(choose, state)

    assert state.data["lists_media"] == "manga"
    submenu = menu.edit_text.await_args.kwargs["reply_markup"]
    assert [
        button.callback_data
        for row in submenu.inline_keyboard
        for button in row
    ] == [
        "lists:view:completed",
        "lists:view:planned",
        "lists:view:all",
        "lists:back",
        "lists:close",
    ]

    back = _callback("lists:back", menu=menu)
    await handlers.lists_menu_cb(back, state)

    assert state.data["lists_media"] is None
    assert menu.edit_text.await_count == 2


@pytest.mark.asyncio
async def test_terminal_view_clears_and_cleans_before_shared_delivery(monkeypatch):
    state = _active_state(media="anime")
    callback = _callback("lists:view:completed")
    load = MagicMock(return_value=StatsAllSnapshot(_empty_stats(), STATS_ALL_VALID))
    cleanup = AsyncMock()

    async def deliver(*args, **kwargs):
        assert state.state is None
        cleanup.assert_awaited_once_with(callback.message)
        report_text = rendered_html(args[2])[0]
        assert "АНИМЕ" in report_text
        assert "0 тайтлов" in report_text
        assert "0 статусов" not in report_text
        return ReportDeliveryResult(True, 1, 1)

    monkeypatch.setattr(handlers, "load_stats_all_snapshot", load)
    monkeypatch.setattr(handlers, "_cleanup_inline_control", cleanup)
    monkeypatch.setattr(handlers, "deliver_report", AsyncMock(side_effect=deliver))

    await handlers.lists_menu_cb(callback, state)

    callback.answer.assert_awaited_once_with()
    load.assert_called_once_with()
    handlers.deliver_report.assert_awaited_once()
    delivery_call = handlers.deliver_report.await_args
    assert delivery_call.args[:2] == (callback.message.bot, 55)
    assert delivery_call.kwargs == {
        "disable_preview": True,
        "notify_partial": True,
    }


@pytest.mark.asyncio
async def test_combined_is_a_terminal_root_action(monkeypatch):
    state = _active_state()
    callback = _callback("lists:combined")
    load = MagicMock(return_value=StatsAllSnapshot(
        _empty_stats(),
        STATS_ALL_VALID,
    ))
    monkeypatch.setattr(
        handlers,
        "load_stats_all_snapshot",
        load,
    )
    delivery = AsyncMock(return_value=ReportDeliveryResult(True, 1, 1))
    monkeypatch.setattr(handlers, "deliver_report", delivery)
    monkeypatch.setattr(handlers, "_cleanup_inline_control", AsyncMock())

    await handlers.lists_menu_cb(callback, state)

    load.assert_called_once_with()
    delivery.assert_awaited_once()
    report = delivery.await_args.args[2]
    assert len(report.units) == 3
    text = "\n".join(rendered_html(report))
    assert all(label in text for label in ("АНИМЕ", "МАНГА", "РАНОБЭ"))
    assert "НЕ ОПРЕДЕЛЕНО" not in text
    assert state.state is None


@pytest.mark.parametrize(
    ("snapshot_state", "expected"),
    [
        (STATS_ALL_MISSING, "Списки ещё не готовы"),
        (STATS_ALL_INVALID, "Не получилось прочитать сохранённые списки"),
    ],
)
def test_missing_and_invalid_snapshot_have_distinct_nonempty_reports(
    monkeypatch,
    snapshot_state,
    expected,
):
    monkeypatch.setattr(
        handlers,
        "load_stats_all_snapshot",
        MagicMock(return_value=StatsAllSnapshot(_empty_stats(), snapshot_state)),
    )
    build = MagicMock(side_effect=AssertionError("domain builder called"))
    monkeypatch.setattr(handlers, "build_list_report", build)

    report = handlers._lists_snapshot_report("anime", "all")

    assert expected in rendered_html(report)[0]
    assert "пока нет тайтлов" not in rendered_html(report)[0]
    build.assert_not_called()


@pytest.mark.asyncio
async def test_opening_and_delivering_lists_has_no_forbidden_side_effects(monkeypatch):
    forbidden_names = (
        "fetch_current_rates",
        "fetch_favourites",
        "fetch_history",
        "sync_stats_all",
        "save_stats_all",
        "load_subscribers",
        "send_backup",
        "inline_access_status",
        "parse_inline_query",
    )
    forbidden = {}
    for name in forbidden_names:
        forbidden[name] = MagicMock(side_effect=AssertionError(name))
        monkeypatch.setattr(handlers, name, forbidden[name])
    load = MagicMock(return_value=StatsAllSnapshot(_empty_stats(), STATS_ALL_VALID))
    delivery = AsyncMock(return_value=ReportDeliveryResult(True, 1, 1))
    monkeypatch.setattr(handlers, "load_stats_all_snapshot", load)
    monkeypatch.setattr(handlers, "deliver_report", delivery)
    monkeypatch.setattr(handlers, "_cleanup_inline_control", AsyncMock())
    state = _State()
    menu = _menu()

    await handlers.cmd_lists(_message(user_id=999, menu=menu), state)
    await handlers.lists_menu_cb(
        _callback("lists:combined", user_id=999, menu=menu),
        state,
    )

    load.assert_called_once_with()
    delivery.assert_awaited_once()
    for mock in forbidden.values():
        mock.assert_not_called()


@pytest.mark.asyncio
async def test_stale_forged_and_malformed_callbacks_are_side_effect_free(
    monkeypatch,
):
    load = MagicMock()
    delivery = AsyncMock()
    cleanup = AsyncMock()
    monkeypatch.setattr(handlers, "load_stats_all_snapshot", load)
    monkeypatch.setattr(handlers, "deliver_report", delivery)
    monkeypatch.setattr(handlers, "_cleanup_inline_menu", cleanup)

    cases = [
        (_active_state(), _callback("lists:view:all", user_id=888)),
        (_active_state(), _callback("lists:view:all", message_id=201)),
        (_active_state(), _callback("lists:unknown:action")),
        (_active_state(), _callback("lists:view:all")),
    ]
    snapshots = [(case[0].state, dict(case[0].data)) for case in cases]
    for state, callback in cases:
        await handlers.lists_menu_cb(callback, state)

    for (state, _), (expected_state, expected_data) in zip(cases, snapshots):
        assert state.state == expected_state
        assert state.data == expected_data
    load.assert_not_called()
    delivery.assert_not_awaited()
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_reuses_exception_safe_cleanup(monkeypatch):
    state = _active_state()
    callback = _callback("lists:close")
    cleanup = AsyncMock()
    monkeypatch.setattr(handlers, "_cleanup_inline_menu", cleanup)

    await handlers.lists_menu_cb(callback, state)

    assert state.state is None
    cleanup.assert_awaited_once_with(callback.message)


@pytest.mark.asyncio
async def test_owner_cancel_uses_common_fsm_contract_for_lists(
    monkeypatch,
):
    state = _active_state()
    cancel = _message(message_id=300)
    safe_delete = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_delete", safe_delete)

    await handlers.cmd_cancel(cancel, state)

    assert state.state is None
    assert state.data == {}
    safe_delete.assert_not_awaited()
    cancel.answer.assert_awaited_once_with("❌ Отменено.")


@pytest.mark.asyncio
async def test_terminal_cleanup_failures_do_not_block_delivery(monkeypatch):
    state = _active_state(media="anime")
    command = MagicMock()
    command.delete = AsyncMock(side_effect=RuntimeError("command inaccessible"))
    menu = _menu()
    menu.reply_to_message = command
    menu.delete = AsyncMock(side_effect=RuntimeError("menu inaccessible"))
    menu.edit_reply_markup = AsyncMock(side_effect=RuntimeError("markup inaccessible"))
    callback = _callback("lists:view:all", menu=menu)
    monkeypatch.setattr(
        handlers,
        "load_stats_all_snapshot",
        MagicMock(return_value=StatsAllSnapshot(_empty_stats(), STATS_ALL_VALID)),
    )
    delivery = AsyncMock(return_value=ReportDeliveryResult(True, 1, 1))
    monkeypatch.setattr(handlers, "deliver_report", delivery)

    await handlers.lists_menu_cb(callback, state)

    delivery.assert_awaited_once()
    menu.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    command.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_does_not_replace_an_unrelated_fsm_operation():
    state = _State("FactsStates:waiting_upload_file", {"prompt_msg_id": 10})
    message = _message()

    await handlers.cmd_lists(message, state)

    message.answer.assert_awaited_once_with(
        "⚠️ Сначала заверши текущую операцию или отправь /cancel."
    )
    message.reply.assert_not_awaited()
    assert state.state == "FactsStates:waiting_upload_file"
    assert state.data == {"prompt_msg_id": 10}
