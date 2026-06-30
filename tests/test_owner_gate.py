# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты owner-reachability gate: запуск/гейт фонового цикла на старте."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import handlers


@pytest.fixture(autouse=True)
def _reset_polling_task():
    handlers._polling_task = None
    yield
    t = handlers._polling_task
    if t is not None and not t.done():
        t.cancel()
    handlers._polling_task = None


@pytest.fixture
def fake_loop(monkeypatch):
    """polling_loop -> заглушка, остаётся pending (без сети), чтобы задача была «жива»."""
    started = []

    async def _loop(bot):
        started.append(bot)
        await asyncio.sleep(3600)

    monkeypatch.setattr(handlers, "polling_loop", _loop)
    return started


@pytest.mark.asyncio
async def test_probe_starts_loop_when_owner_reachable(fake_loop):
    bot = AsyncMock()                       # send_message успешен
    await handlers.probe_owner_and_start(bot)
    bot.send_message.assert_awaited_once()
    assert handlers._polling_task is not None
    await asyncio.sleep(0)                   # даём циклу стартануть
    assert fake_loop == [bot]                # polling_loop запущен ровно с этим bot


@pytest.mark.asyncio
async def test_probe_skips_loop_when_owner_unreachable(fake_loop):
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("forbidden: bot was blocked by the user")
    await handlers.probe_owner_and_start(bot)
    assert handlers._polling_task is None    # цикл НЕ запущен
    assert fake_loop == []                    # polling_loop не вызывался


@pytest.mark.asyncio
async def test_start_polling_loop_is_idempotent(fake_loop):
    bot = MagicMock()
    assert handlers.start_polling_loop(bot) is True     # запустили
    assert handlers.start_polling_loop(bot) is False    # уже жив — повторно не стартуем
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_owner_start_rearms_loop(monkeypatch, fake_loop):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    monkeypatch.setattr(handlers, "save_subscribers", lambda s: None)
    monkeypatch.setattr(handlers, "_backup_after_subscription", AsyncMock())
    msg = AsyncMock()
    msg.from_user = MagicMock(id=handlers.OWNER_ID, full_name="Owner")
    msg.chat = MagicMock(id=handlers.OWNER_ID)
    msg.bot = MagicMock()
    await handlers.cmd_start(msg)
    assert handlers._polling_task is not None             # цикл добужен владельцем
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_non_owner_start_does_not_touch_loop(monkeypatch, fake_loop):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {})
    monkeypatch.setattr(handlers, "save_subscribers", lambda s: None)
    monkeypatch.setattr(handlers, "_backup_after_subscription", AsyncMock())
    msg = AsyncMock()
    msg.from_user = MagicMock(id=handlers.OWNER_ID + 1, full_name="Someone")
    msg.chat = MagicMock(id=handlers.OWNER_ID + 1)
    msg.bot = MagicMock()
    await handlers.cmd_start(msg)
    assert handlers._polling_task is None                 # обычный юзер цикл не трогает
