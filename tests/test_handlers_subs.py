# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты команд подписочного домена: /subs (список для владельца) и /stop
(отписка). Ассертим оркестрацию (кого зовём, что сохраняем), не рендер-текст.
Границы ввода-вывода (storage, авто-бэкап) мокаем."""

from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.enums import ParseMode

import handlers
from name_grammar import build_display_name_context

# ── /subs — только для владельца, ветвление по наличию подписчиков ──

@pytest.mark.asyncio
async def test_cmd_subs_rejects_non_owner(monkeypatch):
    load = MagicMock(return_value={1: "X"})
    monkeypatch.setattr(handlers, "load_subscribers", load)

    msg = MagicMock()
    msg.from_user = MagicMock(id=handlers.OWNER_ID + 1)   # не владелец
    msg.answer = AsyncMock()

    await handlers.cmd_subs(msg)

    msg.answer.assert_awaited_once()
    assert "владельца" in msg.answer.call_args.args[0]
    load.assert_not_called()                     # до чтения списка не доходим


@pytest.mark.asyncio
async def test_cmd_subs_empty_list(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})

    msg = MagicMock()
    msg.from_user = MagicMock(id=handlers.OWNER_ID)
    msg.answer = AsyncMock()

    await handlers.cmd_subs(msg)

    msg.answer.assert_awaited_once()
    assert "нет" in msg.answer.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_cmd_subs_lists_all_subscribers(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {111: "Alice", 222: "Bob"})

    msg = MagicMock()
    msg.from_user = MagicMock(id=handlers.OWNER_ID)
    msg.answer = AsyncMock()

    await handlers.cmd_subs(msg)

    text = msg.answer.call_args.args[0]
    assert "<b>2</b>" in text                     # счётчик подписчиков
    assert "Alice" in text and "Bob" in text      # оба в списке
    alice = '<a href="tg://user?id=111">Alice</a> (<code>111</code>)'
    bob = '<a href="tg://user?id=222">Bob</a> (<code>222</code>)'
    assert alice in text and bob in text            # профили и копируемые ID
    assert text.index(alice) < text.index(bob)      # порядок хранилища сохранён
    assert msg.answer.call_args.kwargs.get("parse_mode") == ParseMode.HTML


@pytest.mark.asyncio
async def test_cmd_subs_escapes_html_in_subscriber_names(monkeypatch):
    """Имена подписчиков из Telegram идут в HTML-сообщение -> обязаны
    экранироваться h(), иначе < > & ломают разметку."""
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {111: "<b>A&B</b>"})

    msg = MagicMock()
    msg.from_user = MagicMock(id=handlers.OWNER_ID)
    msg.answer = AsyncMock()

    await handlers.cmd_subs(msg)

    text = msg.answer.call_args.args[0]
    assert (
        '<a href="tg://user?id=111">&lt;b&gt;A&amp;B&lt;/b&gt;</a> '
        '(<code>111</code>)'
        in text
    )                                               # ссылка + экранирование
    assert "<b>A&B</b>" not in text                 # сырой вид не просочился


# ── /stop — отписка: ветвление «не подписан» / реальная отписка ──

@pytest.mark.asyncio
async def test_cmd_stop_when_not_subscribed_does_nothing(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    saved = []
    monkeypatch.setattr(handlers, "save_subscribers", lambda s: saved.append(s))
    backup = AsyncMock()
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)

    msg = MagicMock()
    msg.chat.id = 555
    msg.from_user = MagicMock(full_name="Ghost", id=555)
    msg.answer = AsyncMock()

    await handlers.cmd_stop(msg)

    msg.answer.assert_awaited_once()
    assert saved == []                            # ничего не сохраняли
    backup.assert_not_awaited()                   # и бэкап не гоняли


@pytest.mark.asyncio
async def test_cmd_stop_removes_subscriber_and_triggers_backup(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {555: "Neo", 777: "Trinity"})
    saved = []
    monkeypatch.setattr(handlers, "save_subscribers", lambda s: saved.append(dict(s)))
    backup = AsyncMock()
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)

    msg = MagicMock()
    msg.chat.id = 555
    msg.from_user = MagicMock(full_name="Neo", id=555)
    msg.bot = MagicMock()
    msg.answer = AsyncMock()

    await handlers.cmd_stop(msg)

    assert saved == [{777: "Trinity"}]            # 555 удалён, остальные целы
    # полная сигнатура: (bot, chat_id, name, subscribed=False) — ловит перестановку
    backup.assert_awaited_once_with(msg.bot, 555, "Neo", subscribed=False)
    msg.answer.assert_awaited_once()


# ── /start — подписка зрителя + авто-бэкап (зеркало /stop) ──

@pytest.mark.asyncio
async def test_cmd_start_already_subscribed_uses_genitive_display_name(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {555: "Morpheus"})
    monkeypatch.setattr(
        handlers,
        "DISPLAY_NAME_CONTEXT",
        build_display_name_context("Костя"),
    )

    msg = MagicMock()
    msg.chat.id = 555
    msg.from_user = MagicMock(full_name="Morpheus", id=555)
    msg.answer = AsyncMock()

    await handlers.cmd_start(msg)

    reply = msg.answer.call_args.args[0]
    assert "об активности Кости" in reply
    assert "активности Костя" not in reply


@pytest.mark.asyncio
async def test_cmd_start_subscribes_and_triggers_backup(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    monkeypatch.setattr(
        handlers,
        "DISPLAY_NAME_CONTEXT",
        build_display_name_context("Костя"),
    )
    saved = []
    monkeypatch.setattr(handlers, "save_subscribers", lambda s: saved.append(dict(s)))
    backup = AsyncMock()
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)

    msg = MagicMock()
    msg.chat.id = 555
    msg.from_user = MagicMock(full_name="Morpheus", id=555)
    msg.bot = MagicMock()
    msg.answer = AsyncMock()

    await handlers.cmd_start(msg)

    assert saved == [{555: "Morpheus"}]            # новый подписчик сохранён
    # полная сигнатура: (bot, chat_id, name, subscribed=True) — ловит subscribed-флип
    backup.assert_awaited_once_with(msg.bot, 555, "Morpheus", subscribed=True)
    msg.answer.assert_awaited_once()
    reply = msg.answer.call_args.args[0]
    assert "об активности Кости" in reply
    assert "активности Костя" not in reply


@pytest.mark.parametrize("already_subscribed", [True, False])
@pytest.mark.asyncio
async def test_cmd_start_escapes_html_names_in_both_branches(
    monkeypatch,
    already_subscribed,
):
    """Оба ответа /start включают HTML: экранируем имя подписчика и
    склонённое DISPLAY_NAME, а в хранилище оставляем исходное имя."""
    subscriber_name = "<Neo & Trinity>"
    initial_subs = {555: subscriber_name} if already_subscribed else {}
    monkeypatch.setattr(handlers, "load_subscribers", lambda: initial_subs.copy())
    monkeypatch.setattr(
        handlers,
        "DISPLAY_NAME_CONTEXT",
        build_display_name_context("<Костя & Co>", "none"),
    )
    saved = []
    monkeypatch.setattr(handlers, "save_subscribers", lambda s: saved.append(dict(s)))
    backup = AsyncMock()
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)

    msg = MagicMock()
    msg.chat.id = 555
    msg.from_user = MagicMock(full_name=subscriber_name, id=555)
    msg.bot = MagicMock()
    msg.answer = AsyncMock()

    await handlers.cmd_start(msg)

    msg.answer.assert_awaited_once()
    reply = msg.answer.call_args.args[0]
    assert "&lt;Neo &amp; Trinity&gt;" in reply
    assert "&lt;Костя &amp; Co&gt;" in reply
    assert subscriber_name not in reply
    assert "<Костя & Co>" not in reply
    assert msg.answer.call_args.kwargs == {"parse_mode": ParseMode.HTML}

    if already_subscribed:
        assert saved == []
        backup.assert_not_awaited()
    else:
        assert saved == [{555: subscriber_name}]
        backup.assert_awaited_once_with(msg.bot, 555, subscriber_name, subscribed=True)


@pytest.mark.parametrize("already_subscribed", [True, False])
@pytest.mark.asyncio
async def test_inline_search_deep_link_adds_only_manual_return_button(
    monkeypatch,
    already_subscribed,
):
    initial = {555: "Morpheus"} if already_subscribed else {}
    monkeypatch.setattr(handlers, "load_subscribers", lambda: initial.copy())
    monkeypatch.setattr(handlers, "save_subscribers", MagicMock())
    monkeypatch.setattr(handlers, "_backup_after_subscription", AsyncMock())
    search_service = MagicMock()
    monkeypatch.setattr(handlers, "_inline_search_service", search_service)
    msg = MagicMock()
    msg.chat.id = 555
    msg.from_user = MagicMock(full_name="Morpheus", id=555)
    msg.bot = MagicMock()
    msg.answer = AsyncMock()

    await handlers.cmd_start(msg, MagicMock(args="inline_search"))

    keyboard = msg.answer.await_args.kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "Вернуться к поиску"
    assert button.switch_inline_query == ""
    assert button.switch_inline_query_current_chat is None
    assert search_service.method_calls == []


@pytest.mark.asyncio
async def test_inline_search_parameter_in_group_keeps_ordinary_start_response(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {-100: "Group"})
    save = MagicMock()
    backup = AsyncMock()
    monkeypatch.setattr(handlers, "save_subscribers", save)
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)
    msg = MagicMock()
    msg.chat.id = -100
    msg.from_user = MagicMock(full_name="Morpheus", id=555)
    msg.answer = AsyncMock()

    await handlers.cmd_start(msg, MagicMock(args="inline_search"))

    assert msg.answer.await_args.kwargs == {"parse_mode": ParseMode.HTML}
    save.assert_not_called()
    backup.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_limit_deep_link_explains_and_changes_no_subscription_state(
    monkeypatch,
):
    load = MagicMock()
    save = MagicMock()
    backup = AsyncMock()
    polling = MagicMock()
    search_service = MagicMock()
    monkeypatch.setattr(handlers, "load_subscribers", load)
    monkeypatch.setattr(handlers, "save_subscribers", save)
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)
    monkeypatch.setattr(handlers, "start_polling_loop", polling)
    monkeypatch.setattr(handlers, "_inline_search_service", search_service)
    msg = MagicMock()
    msg.chat.id = 555
    msg.from_user = MagicMock(full_name="Morpheus", id=555)
    msg.answer = AsyncMock()

    await handlers.cmd_start(msg, MagicMock(args="inline_search_limit"))

    text = msg.answer.await_args.args[0]
    assert "Shikimori попросил сделать паузу" in text
    assert "меньше чем через минуту" in text
    keyboard = msg.answer.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].switch_inline_query == ""
    load.assert_not_called()
    save.assert_not_called()
    backup.assert_not_awaited()
    polling.assert_not_called()
    assert search_service.method_calls == []


@pytest.mark.asyncio
async def test_info_deep_link_is_private_read_only_and_does_not_subscribe(
    monkeypatch,
):
    send_info = AsyncMock()
    load = MagicMock()
    save = MagicMock()
    backup = AsyncMock()
    polling = MagicMock()
    search_service = MagicMock()
    monkeypatch.setattr(handlers, "_send_info", send_info)
    monkeypatch.setattr(handlers, "load_subscribers", load)
    monkeypatch.setattr(handlers, "save_subscribers", save)
    monkeypatch.setattr(handlers, "_backup_after_subscription", backup)
    monkeypatch.setattr(handlers, "start_polling_loop", polling)
    monkeypatch.setattr(handlers, "_inline_search_service", search_service)
    msg = MagicMock()
    msg.chat.id = 555
    msg.from_user = MagicMock(full_name="Morpheus", id=555)

    await handlers.cmd_start(msg, MagicMock(args="info"))

    send_info.assert_awaited_once_with(msg)
    load.assert_not_called()
    save.assert_not_called()
    backup.assert_not_awaited()
    polling.assert_not_called()
    assert search_service.method_calls == []
