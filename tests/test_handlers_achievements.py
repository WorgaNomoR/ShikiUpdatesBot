# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Достижения участвуют в runtime без подписки и отдельного scheduler."""

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

import handlers
import storage
from achievement_state import AchievementStateError
from shiki_api import ProfilePrivacyError


@pytest.fixture
def achievement_polling_env(backup_env, monkeypatch):
    """Один запуск и цикл с изолированными HTTP, Telegram, бэкапами и часами."""
    storage.save_subscribers({111: "subscriber"})
    history = AsyncMock(return_value=(set(), storage._empty_stats_current("2026-Q4")))
    subscription = AsyncMock()
    weekly = AsyncMock(side_effect=lambda bot, cur: cur)
    monkeypatch.setattr("handlers.fetch_favourites", AsyncMock(return_value={}))
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[]))
    monkeypatch.setattr("handlers.sync_stats_all", AsyncMock(return_value=({}, True)))
    monkeypatch.setattr("handlers.check_and_notify", history)
    monkeypatch.setattr("handlers.check_and_notify_favourites", AsyncMock(return_value=(set(), False)))
    monkeypatch.setattr("handlers.rotate_quarter_if_needed", AsyncMock(side_effect=lambda bot, cur, stats, **kw: cur))
    monkeypatch.setattr("handlers._backup_after_subscription", subscription)
    monkeypatch.setattr("handlers._weekly_backup_if_due", weekly)
    monkeypatch.setattr("handlers.CHECK_INTERVAL", 900)
    monkeypatch.setattr("handlers.dispatch_notifications", AsyncMock(return_value=False))
    real_sleep = handlers.asyncio.sleep

    async def stop(seconds):
        if seconds > 1:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr("handlers.asyncio.sleep", stop)
    return history, subscription, weekly


@pytest.mark.asyncio
async def test_polling_imports_at_boot_and_each_cycle_and_delivers_to_owner(
    achievement_polling_env, monkeypatch, achievement_row_factory,
):
    source = AsyncMock(return_value={"user_id": 42, "rows": [achievement_row_factory()]})
    monkeypatch.setattr("achievement_delivery.fetch_achievements", source)
    bot = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(bot)
    assert source.await_count == 2
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 999
    assert storage.load_achievement_state()["pending"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["boot", "cycle"])
async def test_unexpected_achievement_error_keeps_history_backups_and_pending_delivery(
    achievement_polling_env, monkeypatch, achievement_row_factory, caplog, phase,
):
    caplog.set_level(logging.WARNING, logger="shikiupdatesbot")
    marker = "PRIVATE_ACHIEVEMENT_ERROR_DETAILS"
    valid = {"user_id": 42, "rows": [achievement_row_factory()]}
    failure = RuntimeError(marker)
    source = AsyncMock(side_effect=[failure, valid] if phase == "boot" else [valid, failure])
    monkeypatch.setattr("achievement_delivery.fetch_achievements", source)
    bot = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(bot)
    history, subscription, weekly = achievement_polling_env
    assert source.await_count == 2
    history.assert_awaited_once()
    assert subscription.await_count == 2
    assert weekly.await_count == 2
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 999
    assert marker not in caplog.text
    assert "RuntimeError" in caplog.text
    state = storage.load_achievement_state()
    assert state["earned"] == {"animelist": 1}
    assert state["pending"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), ProfilePrivacyError("fetch_achievements")])
async def test_achievement_refresh_propagates_cancellation_and_profile_privacy(monkeypatch, error, caplog):
    source = AsyncMock(side_effect=error)
    monkeypatch.setattr("handlers.synchronize_achievements", source)
    bot = AsyncMock()
    with pytest.raises(type(error)):
        await handlers._refresh_achievements(bot)
    source.assert_awaited_once_with(session=None)
    bot.send_message.assert_not_awaited()
    assert not caplog.records


@pytest.mark.asyncio
async def test_achievement_state_error_retains_safe_owner_diagnostic(monkeypatch):
    error = AchievementStateError("achievement_version")
    source = AsyncMock(side_effect=error)
    diagnostic = AsyncMock()
    monkeypatch.setattr("handlers.synchronize_achievements", source)
    monkeypatch.setattr("handlers.achievement_diagnostic", diagnostic)
    bot, session = object(), object()
    await handlers._refresh_achievements(bot, session=session)
    source.assert_awaited_once_with(session=session)
    diagnostic.assert_awaited_once_with(bot, error)


@pytest.mark.asyncio
async def test_failed_owner_diagnostic_keeps_polling_and_corrupt_state(
    achievement_polling_env, monkeypatch, caplog,
):
    caplog.set_level(logging.WARNING, logger="shikiupdatesbot")
    damaged = b'{"PRIVATE_ACHIEVEMENT_STATE":'
    storage.ACHIEVEMENTS_FILE.write_bytes(damaged)
    source = AsyncMock()
    monkeypatch.setattr("achievement_delivery.fetch_achievements", source)
    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError("PRIVATE_TELEGRAM_ERROR_DETAILS")

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(bot)

    history, subscription, weekly = achievement_polling_env
    history.assert_awaited_once()
    assert subscription.await_count == 2
    assert weekly.await_count == 2
    source.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 999
    assert "Достижения Shikimori временно не обрабатываются" in bot.send_message.await_args.args[1]
    assert "PRIVATE_" not in caplog.text
    assert "PRIVATE_" not in bot.send_message.await_args.args[1]
    assert storage.ACHIEVEMENTS_FILE.read_bytes() == damaged


@pytest.mark.asyncio
async def test_history_delivery_failure_does_not_block_achievement_consumer(monkeypatch):
    monkeypatch.setattr("handlers.dispatch_notifications", AsyncMock(side_effect=ValueError()))
    monkeypatch.setattr("handlers._journal_diagnostic", AsyncMock())
    achievements = AsyncMock(return_value=False)
    monkeypatch.setattr("handlers.dispatch_achievements", achievements)
    monkeypatch.setattr("handlers.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await handlers._wait_and_dispatch_notifications(object())
    achievements.assert_awaited_once()
