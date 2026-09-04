# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты owner-reachability gate: запуск/гейт фонового цикла на старте."""

import asyncio
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest

import handlers
import runtime_status
import storage
from utils import _utcnow


@pytest.fixture(autouse=True)
def _reset_polling_task():
    handlers._polling_task = None
    runtime_status.set_polling_active(False)
    yield
    t = handlers._polling_task
    if t is not None and not t.done():
        t.cancel()
    handlers._polling_task = None
    runtime_status.set_polling_active(False)


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
async def test_polling_runtime_status_becomes_inactive_when_task_stops(fake_loop):
    assert handlers.start_polling_loop(MagicMock()) is True
    await asyncio.sleep(0)
    assert runtime_status.get_runtime_snapshot().polling_active is True

    task = handlers._polling_task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert runtime_status.get_runtime_snapshot().polling_active is False


def test_stale_polling_done_callback_does_not_hide_new_active_task(monkeypatch):
    old_task = MagicMock()
    old_task.cancelled.return_value = True
    handlers._polling_task = MagicMock()
    runtime_status.set_polling_active(True)

    handlers._on_polling_done(old_task)

    assert runtime_status.get_runtime_snapshot().polling_active is True


@pytest.mark.asyncio
async def test_owner_start_rearms_loop(monkeypatch, fake_loop):
    monkeypatch.setattr(
        handlers,
        "mutate_subscription",
        AsyncMock(
            return_value=storage.SubscriptionMutation(
                changed=True,
                subscriber_count=1,
            ),
        ),
    )
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
    monkeypatch.setattr(
        handlers,
        "mutate_subscription",
        AsyncMock(
            return_value=storage.SubscriptionMutation(
                changed=True,
                subscriber_count=1,
            ),
        ),
    )
    monkeypatch.setattr(handlers, "_backup_after_subscription", AsyncMock())
    msg = AsyncMock()
    msg.from_user = MagicMock(id=handlers.OWNER_ID + 1, full_name="Someone")
    msg.chat = MagicMock(id=handlers.OWNER_ID + 1)
    msg.bot = MagicMock()
    await handlers.cmd_start(msg)
    assert handlers._polling_task is None                 # обычный юзер цикл не трогает


# ── стартовый health-снапшот в пинге owner-gate ──
def test_build_startup_text_renders_snapshot(monkeypatch):
    monkeypatch.setattr(handlers, "load_subscribers", lambda: {1: "a", 2: "b", 3: "c"})
    monkeypatch.setattr(handlers, "load_seen_ids", lambda: set(range(1240)))
    monkeypatch.setattr(handlers, "load_seen_favourites",
                        lambda: {f"anime_{i}" for i in range(37)})
    monkeypatch.setattr(handlers, "load_stats_all",
                        lambda: {"updated_at": _utcnow().isoformat()})
    monkeypatch.setattr(
        handlers,
        "load_subscription_backup_state",
        lambda: {"last_backup_at": None},
    )
    txt = handlers._build_startup_text()
    assert txt.startswith("🟢 Бот запущен")
    assert "Подписчиков: 3" in txt
    assert "история 1240" in txt


def test_build_startup_text_falls_back_on_error(monkeypatch):
    def boom():
        raise RuntimeError("disk gone")
    monkeypatch.setattr(handlers, "load_stats_all", boom)
    # сборка снапшота упала -> голый пинг, проба доставки не должна ломаться
    assert handlers._build_startup_text() == "🟢 Бот запущен"


@pytest.mark.asyncio
async def test_probe_sends_startup_snapshot(monkeypatch, fake_loop):
    monkeypatch.setattr(handlers, "_build_startup_text", lambda: "🟢 SNAP")
    bot = AsyncMock()
    await handlers.probe_owner_and_start(bot)
    bot.send_message.assert_awaited_once_with(handlers.OWNER_ID, "🟢 SNAP", parse_mode=None)
