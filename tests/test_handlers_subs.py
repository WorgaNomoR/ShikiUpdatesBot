# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты команд подписочного домена: /subs (список для владельца) и /stop
(отписка). Ассертим оркестрацию (кого зовём, что сохраняем), не рендер-текст.
Границы ввода-вывода (storage, авто-бэкап) мокаем."""

from unittest.mock import AsyncMock, MagicMock

import pytest

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
    assert msg.answer.call_args.kwargs.get("parse_mode") is not None   # HTML-разметка


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
    backup.assert_awaited_once()                  # авто-бэкап после отписки
    assert backup.call_args.kwargs.get("subscribed") is False
    msg.answer.assert_awaited_once()
