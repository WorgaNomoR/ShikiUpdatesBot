# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Обслуживание принятой очереди между acquisition без повторного опроса."""

import asyncio
from unittest.mock import AsyncMock

import pytest

import handlers
import storage


@pytest.fixture
def wait_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("handlers.time.monotonic", lambda: clock[0])

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr("handlers.asyncio.sleep", sleep)
    return clock


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,cost,expected", [
    (180, 6, [0, 60, 120]),
    (25, 6, [0]),
    (180, 75, [0, 76, 152]),
    (5, 10, [0]),
])
async def test_notification_wait_keeps_one_deadline(wait_clock, monkeypatch, interval, cost, expected):
    starts = []
    monkeypatch.setattr("handlers.CHECK_INTERVAL", interval)

    async def dispatch(_bot):
        starts.append(wait_clock[0])
        wait_clock[0] += cost
        return True

    monkeypatch.setattr("handlers.dispatch_notifications", dispatch)
    await handlers._wait_and_dispatch_notifications(object())
    assert starts == expected
    assert wait_clock[0] == max(interval, expected[-1] + cost)


@pytest.mark.asyncio
async def test_idle_queue_sleeps_remaining_wait_once(wait_clock, monkeypatch):
    dispatch = AsyncMock(return_value=False)
    monkeypatch.setattr("handlers.CHECK_INTERVAL", 900)
    monkeypatch.setattr("handlers.dispatch_notifications", dispatch)
    await handlers._wait_and_dispatch_notifications(object())
    dispatch.assert_awaited_once()
    assert wait_clock[0] == 900


@pytest.mark.asyncio
async def test_delivery_error_retries_without_resetting_deadline(wait_clock, monkeypatch):
    starts = []

    async def dispatch(_bot):
        starts.append(wait_clock[0])
        raise OSError("publication")

    diagnostic = AsyncMock()
    monkeypatch.setattr("handlers.CHECK_INTERVAL", 130)
    monkeypatch.setattr("handlers.dispatch_notifications", dispatch)
    monkeypatch.setattr("handlers._journal_diagnostic", diagnostic)
    await handlers._wait_and_dispatch_notifications(object())
    assert starts == [0, 60, 120]
    assert wait_clock[0] == 130
    assert diagnostic.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["dispatch", "sleep"])
async def test_notification_wait_propagates_cancellation(wait_clock, monkeypatch, phase):
    dispatch = AsyncMock(side_effect=asyncio.CancelledError if phase == "dispatch" else None, return_value=True)
    diagnostic = AsyncMock()
    monkeypatch.setattr("handlers.dispatch_notifications", dispatch)
    monkeypatch.setattr("handlers._journal_diagnostic", diagnostic)
    if phase == "sleep":
        monkeypatch.setattr("handlers.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await handlers._wait_and_dispatch_notifications(object())
    diagnostic.assert_not_awaited()


@pytest.mark.asyncio
async def test_polling_drains_three_batches_without_another_history_check(
    backup_env, journal_factory, monkeypatch,
):
    storage.save_subscribers({cid: str(cid) for cid in range(1, 101)})
    journal = journal_factory()
    storage.save_event_journal(journal)
    storage.save_stats_current({
        "period": "2026-Q2", "events": [],
        "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0},
    }, strict=True)
    await handlers._drain_history_journal(AsyncMock())
    frozen = storage.load_event_journal()["outbox"]["records"][0]
    clock = [0.0]
    checks = []
    monkeypatch.setattr("handlers.CHECK_INTERVAL", 180)
    monkeypatch.setattr("handlers.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("handlers.time.time", lambda: frozen["created_at"] + clock[0])

    async def sleep(seconds):
        clock[0] += seconds

    async def check(_bot, seen, cur):
        checks.append(clock[0])
        if len(checks) == 2:
            raise asyncio.CancelledError
        return seen, cur

    monkeypatch.setattr("handlers.asyncio.sleep", sleep)
    monkeypatch.setattr("handlers.check_and_notify", check)
    monkeypatch.setattr("handlers.fetch_favourites", AsyncMock(return_value={}))
    monkeypatch.setattr("handlers.sync_stats_all", AsyncMock(return_value=(storage._empty_stats_all(), True)))
    monkeypatch.setattr("handlers.check_and_notify_favourites", AsyncMock(return_value=(set(), False)))
    monkeypatch.setattr("handlers.rotate_quarter_if_needed", AsyncMock(side_effect=lambda _bot, cur, _stats, **kw: cur))
    monkeypatch.setattr("handlers._backup_after_subscription", AsyncMock())
    monkeypatch.setattr("handlers._weekly_backup_if_due", AsyncMock(side_effect=lambda _bot, cur: cur))
    bot = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(bot)

    assert bot.send_message.await_count == 60
    assert checks == pytest.approx([0, 180])
    record = storage.load_event_journal()["outbox"]["records"][0]
    assert record["payload"] == frozen["payload"]
    assert record["expires_at"] == frozen["expires_at"]
    assert list(record["recipients"]) == list(frozen["recipients"])
    pending = [r for r in record["recipients"].values() if r["status"] == "pending"]
    assert len(pending) == 40
    assert all(r["attempts"] == [] for r in pending)
