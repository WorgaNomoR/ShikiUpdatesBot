# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Публикации, restart и независимые recipient leases без реального Telegram."""

import asyncio
import io
import json
import zipfile
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

import backup
import handlers
import notification_delivery as delivery
import storage
from event_journal_schema import EventJournalStateError
from notification_outbox import (
    LIFETIME,
    MAX_ATTEMPTS,
)


def _zip(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in members.items():
            archive.writestr(name, json.dumps(value))
    return buffer.getvalue()


def _recovery():
    return _zip(
        {
            "event_journal.json": storage.load_event_journal(),
            "stats_current.json": storage.load_stats_current(strict=True),
            "subscribers.json": json.loads(storage.SUBS_FILE.read_text(encoding="utf-8")),
        }
    )


@pytest.fixture
def outbox_env(backup_env, monkeypatch):
    clock = [1800000000.0]
    monkeypatch.setattr("notification_delivery.time.time", lambda: clock[0])
    monkeypatch.setattr("notification_delivery.asyncio.sleep", AsyncMock())
    storage.save_stats_current({"period": "2026-Q2", "events": []}, strict=True)
    storage.save_subscribers({10: "first", 20: "second", -100: "group"})
    return clock


async def _enqueue(factory, count=2):
    journal = factory(count=count)
    storage.save_event_journal(journal)
    cur = storage.load_stats_current(strict=True)
    cur["event_projection"] = {
        "journal_id": journal["journal_id"],
        "baseline_seq": 0,
        "applied_seq": 0,
    }
    storage.save_stats_current(cur, strict=True)
    await handlers._drain_history_journal(AsyncMock())


def _recipient(seq=1, cid="10"):
    return storage.load_event_journal()["outbox"]["records"][seq - 1]["recipients"][cid]


@pytest.mark.asyncio
async def test_enqueue_has_no_dispatch_and_freezes_every_chat(outbox_env, journal_factory):
    await _enqueue(journal_factory)
    journal = storage.load_event_journal()
    assert journal["processed_seq"] == 2
    assert storage.load_stats_current(strict=True)["event_projection"]["applied_seq"] == 2
    records = journal["outbox"]["records"]
    assert all(set(record["recipients"]) == {"10", "20", "-100"} for record in records)
    bot = AsyncMock()
    await delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == 6
    for call in bot.send_message.await_args_list:
        assert call.kwargs["text"] in {r["payload"]["text"] for r in records}


@pytest.mark.asyncio
async def test_partial_restart_order_and_independence(outbox_env, journal_factory):
    await _enqueue(journal_factory)
    sent = []

    async def send(**kwargs):
        sent.append(kwargs["chat_id"])
        if kwargs["chat_id"] == 10:
            raise TelegramNetworkError(SendMessage(chat_id=10, text="x"), "lost response")
        return object()

    bot = AsyncMock()
    bot.send_message.side_effect = send
    await delivery.dispatch_notifications(bot)
    assert sent.count(10) == 1 and sent.count(20) == sent.count(-100) == 2
    assert _recipient()["status"] == "pending"
    assert _recipient(2)["attempts"] == []
    assert _recipient(cid="20")["status"] == "delivered"
    # Новый consumer использует только опубликованные clocks/acknowledgements.
    await delivery.dispatch_notifications(bot)
    assert len(sent) == 5
    outbox_env[0] += 60
    bot.send_message.side_effect = None
    bot.send_message.reset_mock()
    await delivery.dispatch_notifications(bot)
    assert [call.kwargs["chat_id"] for call in bot.send_message.await_args_list] == [10, 10]
    assert _recipient()["duplicate_possible"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("later", ["rejection", "success"])
async def test_accepted_lost_response_then_late_outcome(outbox_env, journal_factory, later):
    storage.save_subscribers({10: "only"})
    await _enqueue(journal_factory, count=1)
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramNetworkError(
        SendMessage(chat_id=10, text="x"), "lost response"
    )
    await delivery.dispatch_notifications(bot)
    outbox_env[0] += 60
    bot.send_message.side_effect = (
        TelegramBadRequest(SendMessage(chat_id=10, text="x"), "invalid")
        if later == "rejection"
        else None
    )
    await delivery.dispatch_notifications(bot)
    recipient = _recipient()
    assert recipient["attempts"][0]["outcome"] == "uncertain"
    assert recipient["status"] == ("rejected" if later == "rejection" else "delivered")
    assert recipient["duplicate_possible"] == (later == "success")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["marker", "ack"])
async def test_failed_publication_never_loses_or_falsely_confirms(
    outbox_env, journal_factory, monkeypatch, phase
):
    storage.save_subscribers({10: "only"})
    await _enqueue(journal_factory, count=1)
    original = storage.EVENT_JOURNAL_FILE.read_bytes()
    write = storage._atomic_write

    def fail(path, payload):
        if path == storage.EVENT_JOURNAL_FILE:
            recipient = json.loads(payload)["outbox"]["records"][0]["recipients"]["10"]
            if (
                phase == "marker"
                and recipient["attempts"]
                or phase == "ack"
                and recipient["status"] == "delivered"
            ):
                raise OSError("write")
        return write(path, payload)

    bot = AsyncMock()
    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", fail)
        with pytest.raises(EventJournalStateError):
            await delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == (0 if phase == "marker" else 1)
    assert _recipient()["status"] == "pending"
    if phase == "marker":
        assert storage.EVENT_JOURNAL_FILE.read_bytes() == original
    else:
        assert _recipient()["attempts"] == [{"at": 1800000000.0, "outcome": "uncertain"}]
    outbox_env[0] += 60
    await delivery.dispatch_notifications(bot)
    assert _recipient()["status"] == "delivered"
    assert _recipient()["duplicate_possible"] == (phase == "ack")


@pytest.mark.asyncio
async def test_cancellation_preserves_attempt_ttl_and_duplicate_risk(outbox_env, journal_factory):
    storage.save_subscribers({10: "only"})
    await _enqueue(journal_factory, count=1)
    bot = AsyncMock()
    bot.send_message.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await delivery.dispatch_notifications(bot)
    assert len(_recipient()["attempts"]) == 1
    outbox_env[0] += 60
    bot.send_message.side_effect = None
    await delivery.dispatch_notifications(bot)
    assert _recipient()["duplicate_possible"]
    assert (
        storage.load_event_journal()["outbox"]["records"][0]["expires_at"] == 1800000000 + LIFETIME
    )


@pytest.mark.asyncio
async def test_retry_budget_and_expiry_survive_cycles(outbox_env, journal_factory):
    storage.save_subscribers({10: "only"})
    await _enqueue(journal_factory, count=1)
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(
        SendMessage(chat_id=10, text="x"), "rate", retry_after=10**9
    )
    for attempt in range(MAX_ATTEMPTS):
        await delivery.dispatch_notifications(bot)
        recipient = _recipient()
        assert len(recipient["attempts"]) == attempt + 1
        assert recipient["next_attempt_at"] - outbox_env[0] <= 21600
        outbox_env[0] = recipient["next_attempt_at"]
    assert _recipient()["status"] == "expired"
    await delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_lifetime_expires_unsent_and_new_subscription_has_no_old_queue(
    outbox_env, journal_factory
):
    await _enqueue(journal_factory, count=1)
    await storage.mutate_subscription(30, "new", subscribed=True)
    outbox_env[0] += LIFETIME
    bot = AsyncMock()
    await delivery.dispatch_notifications(bot)
    bot.send_message.assert_not_awaited()
    assert _recipient()["status"] == "expired"
    assert "30" not in storage.load_event_journal()["outbox"]["records"][0]["recipients"]


@pytest.mark.asyncio
async def test_unsubscribe_resubscribe_and_block_cancel_old_membership(outbox_env, journal_factory):
    await _enqueue(journal_factory, count=1)
    old_token = _recipient()["membership"]
    await storage.mutate_subscription(10, "first", subscribed=False)
    await storage.mutate_subscription(10, "first", subscribed=True)
    assert storage.load_subscriber_state().notification_memberships[10] != old_token
    await storage.add_blocked_user(20)
    bot = AsyncMock()
    await delivery.dispatch_notifications(bot)
    assert [call.kwargs["chat_id"] for call in bot.send_message.await_args_list] == [-100]
    assert _recipient()["status"] == _recipient(cid="20")["status"] == "cancelled"
    await storage.remove_blocked_user(20)
    await storage.mutate_subscription(20, "second", subscribed=True)
    await delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_forbidden_removes_immediately_and_other_chats_continue(outbox_env, journal_factory):
    await _enqueue(journal_factory)

    async def send(**kwargs):
        if kwargs["chat_id"] == 10:
            raise TelegramForbiddenError(SendMessage(chat_id=10, text="x"), "forbidden")
        assert 10 not in storage.load_subscribers()

    bot = AsyncMock()
    bot.send_message.side_effect = send

    # Fairness начинает с группового chat ID; проверяем удаление после refusal.
    async def checked(**kwargs):
        if kwargs["chat_id"] == 10:
            return await send(**kwargs)
        if _recipient()["status"] == "rejected":
            assert 10 not in storage.load_subscribers()

    bot.send_message.side_effect = checked
    await delivery.dispatch_notifications(bot)
    assert 10 not in storage.load_subscribers()
    assert _recipient()["status"] == "rejected" and _recipient()["reason"] == "forbidden"
    assert _recipient(2)["status"] == "cancelled"
    assert _recipient(2, "20")["status"] == "delivered"


@pytest.mark.asyncio
async def test_cycle_count_budget(outbox_env, journal_factory):
    storage.save_subscribers({i: f"chat {i}" for i in range(1, 26)})
    await _enqueue(journal_factory, count=1)
    bot = AsyncMock()
    await delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == 20
    await delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["identical", "older", "unrelated", "legacy"])
async def test_restore_during_dispatch_invalidates_ack(outbox_env, journal_factory, kind):
    await _enqueue(journal_factory, count=1)
    older = _recovery()
    bot = AsyncMock()

    async def send(**kwargs):
        if kind == "identical":
            raw = _recovery()
        elif kind == "older":
            raw = older
        elif kind == "legacy":
            raw = _zip({"stats_current.json": {"period": "2026-Q2", "events": []}})
        else:
            raw = _zip(
                {
                    "update_state.json": {
                        key: None
                        for key in (
                            "last_checked_at",
                            "latest_version",
                            "release_url",
                            "last_notified_version",
                        )
                    }
                }
            )
        await backup.restore_backup_zip(raw)

    bot.send_message.side_effect = send
    await delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == 1
    assert all(
        r["status"] == "pending"
        for r in storage.load_event_journal()["outbox"]["records"][0]["recipients"].values()
    )


@pytest.mark.asyncio
async def test_full_backup_and_older_restore_replay_only_restored_work(outbox_env, journal_factory):
    await _enqueue(journal_factory, count=1)
    older = _recovery()
    bot = AsyncMock()
    await delivery.dispatch_notifications(bot)
    raw, _ = await backup._build_backup_zip()
    await backup.restore_backup_zip(raw)
    await delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == 3
    await backup.restore_backup_zip(older)
    await delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == 6


@pytest.mark.asyncio
async def test_corrupt_recovery_rejects_before_any_publication(outbox_env, journal_factory):
    await _enqueue(journal_factory, count=1)
    journal = deepcopy(storage.load_event_journal())
    journal["outbox"]["records"][0]["recipients"]["10"]["attempts"] = [True]
    before = storage.EVENT_JOURNAL_FILE.read_bytes()
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(
            _zip(
                {
                    "event_journal.json": journal,
                    "stats_current.json": storage.load_stats_current(strict=True),
                }
            )
        )
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == before


@pytest.mark.asyncio
async def test_eligibility_change_after_marker_is_proven_non_dispatch(
    outbox_env, journal_factory, monkeypatch
):
    storage.save_subscribers({10: "only"})
    await _enqueue(journal_factory, count=1)
    original = delivery.notification_memberships
    calls = 0

    def change():
        nonlocal calls
        calls += 1
        if calls == 2:
            storage.save_subscribers({})
        return original()

    monkeypatch.setattr("notification_delivery.notification_memberships", change)
    bot = AsyncMock()
    await delivery.dispatch_notifications(bot)
    bot.send_message.assert_not_awaited()
    recipient = _recipient()
    assert recipient["status"] == "cancelled"
    assert recipient["attempts"][0]["outcome"] == "not_dispatched"


@pytest.mark.asyncio
async def test_late_forbidden_cannot_remove_new_subscription(outbox_env, journal_factory):
    storage.save_subscribers({10: "only"})
    await _enqueue(journal_factory, count=1)

    async def send(**kwargs):
        assert not storage._restorable_state_lock().locked()
        await storage.mutate_subscription(10, "only", subscribed=False)
        await storage.mutate_subscription(10, "new", subscribed=True)
        raise TelegramForbiddenError(SendMessage(chat_id=10, text="x"), "forbidden")

    bot = AsyncMock()
    bot.send_message.side_effect = send
    await delivery.dispatch_notifications(bot)
    assert storage.load_subscribers() == {10: "new"}
    assert _recipient()["status"] == "rejected"
    assert (
        storage.load_subscriber_state().notification_memberships[10] != _recipient()["membership"]
    )


@pytest.mark.asyncio
async def test_request_timeout_is_uncertain_and_work_time_is_bounded(
    outbox_env, journal_factory, monkeypatch
):
    storage.save_subscribers({10: "only"})
    await _enqueue(journal_factory, count=1)
    monkeypatch.setattr("notification_delivery.REQUEST_SECONDS", 0.01)
    started = asyncio.Event()

    async def send(**kwargs):
        assert not storage._restorable_state_lock().locked()
        started.set()
        await asyncio.Event().wait()

    bot = AsyncMock()
    bot.send_message.side_effect = send
    await delivery.dispatch_notifications(bot)
    assert started.is_set()
    assert _recipient()["status"] == "pending"
    assert _recipient()["attempts"][0]["outcome"] == "uncertain"
    outbox_env[0] += 60
    monkeypatch.setattr("notification_delivery.DISPATCH_SECONDS", 0)
    await delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_two_consumers_do_not_repeat_confirmed_recipient(outbox_env, journal_factory):
    storage.save_subscribers({10: "only"})
    await _enqueue(journal_factory, count=1)
    started, resume = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        assert not storage._restorable_state_lock().locked()
        started.set()
        await resume.wait()

    bot = AsyncMock()
    bot.send_message.side_effect = send
    first = asyncio.create_task(delivery.dispatch_notifications(bot))
    await started.wait()
    # Повтор уже разрешён, пока первая отправка ещё ждёт подтверждения.
    outbox_env[0] += 60
    second = asyncio.create_task(delivery.dispatch_notifications(bot))
    resume.set()
    await asyncio.gather(first, second)
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_crash_on_last_marker_never_resets_attempt_budget(outbox_env, journal_factory):
    storage.save_subscribers({10: "only"})
    await _enqueue(journal_factory, count=1)
    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError()
    for _ in range(MAX_ATTEMPTS - 1):
        await delivery.dispatch_notifications(bot)
        outbox_env[0] = _recipient()["next_attempt_at"]
    bot.send_message.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await delivery.dispatch_notifications(bot)
    assert len(_recipient()["attempts"]) == MAX_ATTEMPTS
    bot.send_message.side_effect = None
    await delivery.dispatch_notifications(bot)
    assert _recipient()["status"] == "expired"
    assert bot.send_message.await_count == MAX_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["journal", "privacy"])
async def test_polling_dispatches_accepted_work_despite_acquisition_failure(outbox_env, journal_factory, monkeypatch, failure):
    from shiki_api import ProfilePrivacyError

    storage.save_subscribers({10: "only"})
    await _enqueue(journal_factory, count=1)
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: {"animes_1"})
    monkeypatch.setattr("handlers.fetch_favourites", AsyncMock(return_value={}))
    monkeypatch.setattr("handlers.sync_stats_all", AsyncMock(return_value=(storage._empty_stats_all(), False)))
    monkeypatch.setattr("handlers.check_and_notify", AsyncMock(side_effect=EventJournalStateError("acquisition") if failure == "journal" else ProfilePrivacyError("history")))
    monkeypatch.setattr("handlers.check_and_notify_favourites", AsyncMock(return_value=({"animes_1"}, False)))
    monkeypatch.setattr("handlers._backup_after_subscription", AsyncMock())
    monkeypatch.setattr("handlers._weekly_backup_if_due", AsyncMock(side_effect=lambda bot, cur: cur))
    async def sleep(delay):
        if delay == handlers.CHECK_INTERVAL:
            raise asyncio.CancelledError()
    monkeypatch.setattr("handlers.asyncio.sleep", sleep)
    bot = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(bot)
    assert _recipient()["status"] == "delivered"
    assert storage.load_event_journal()["processed_seq"] == 1
