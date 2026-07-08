# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты команд подписочного домена: /subs (список для владельца) и /stop
(отписка). Ассертим оркестрацию (кого зовём, что сохраняем), не рендер-текст.
Границы ввода-вывода (storage, авто-бэкап) мокаем."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ParseMode

import handlers

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
    assert "111" in text and "222" in text        # с chat_id
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
    assert "&lt;b&gt;A&amp;B&lt;/b&gt;" in text     # экранировано
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
async def test_cmd_start_subscribes_and_triggers_backup(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
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
