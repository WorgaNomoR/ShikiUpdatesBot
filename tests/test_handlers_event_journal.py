# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Границы admission, проекции и legacy-попытки рассылки."""

import asyncio
import io
import json
import zipfile
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

import backup
import handlers
import storage
from event_journal_schema import EventJournalStateError
from notification_delivery import dispatch_notifications


def _entry(history_id=2):
    return {
        "id": history_id, "description": "Просмотрено и оценено на 8",
        "created_at": "2026-04-01T03:00:00+03:00",
        "target": {"id": history_id + 10, "kind": "tv", "name": f"Title {history_id}"},
    }


@pytest.fixture
def history_env(backup_env, monkeypatch):
    storage.save_stats_current({"period": "2026-Q2", "events": []}, strict=True)
    monkeypatch.setattr("handlers.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[]))
    monkeypatch.setattr("handlers._enqueue_history_event", AsyncMock(wraps=handlers._enqueue_history_event))
    return backup_env


async def _ready():
    await handlers._initialize_history_journal({1}, storage.restorable_restore_generation())
    return await handlers._drain_history_journal(AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [[], [1, 3]])
async def test_valid_legacy_migration_including_empty_is_silent(history_env, monkeypatch, legacy):
    storage.SEEN_IDS_FILE.write_text(json.dumps({"seen_ids": legacy}), encoding="utf-8")
    before = storage.load_stats_current(strict=True)
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=None))
    await handlers.check_and_notify(AsyncMock(), {999}, before)
    journal = storage.load_event_journal()
    assert journal["baseline_initialized"] is True
    assert journal["baseline_ids"] == legacy
    assert journal["events"] == []
    assert storage.load_stats_current(strict=True)["events"] == before["events"]
    handlers._enqueue_history_event.assert_not_awaited()
    storage.SEEN_IDS_FILE.write_bytes(b"{corrupt")
    await handlers.check_and_notify(AsyncMock(), {999}, before)
    assert storage.load_event_journal() == journal


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [None, b"{bad", b'{"seen_ids":[true]}'])
@pytest.mark.parametrize("entries", [[], [_entry()]])
async def test_bootstrap_publishes_readiness_even_when_empty(history_env, monkeypatch, legacy, entries):
    if legacy is not None:
        storage.SEEN_IDS_FILE.write_bytes(legacy)
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr("handlers.fetch_history", fetch)
    await handlers.check_and_notify(AsyncMock(), {999}, None)
    assert storage.load_event_journal() is None
    fetch.return_value = entries
    await handlers.check_and_notify(AsyncMock(), set(), None)
    journal = storage.load_event_journal()
    assert journal["baseline_initialized"] is True
    assert journal["baseline_ids"] == [entry["id"] for entry in entries]
    assert journal["events"] == []
    fetch.return_value = [_entry(3)]
    await handlers.check_and_notify(AsyncMock(), set(), None)
    assert storage.load_event_journal()["events"][0]["history_id"] == 3
    handlers._enqueue_history_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_interrupted_binding_reuses_published_identity(history_env, monkeypatch):
    original = storage.STATS_CURRENT_FILE.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr("handlers.save_stats_current", lambda *a, **k: (_ for _ in ()).throw(storage.QuarterDeliveryStateError("write")))
        with pytest.raises(storage.QuarterDeliveryStateError):
            await _ready()
    journal = storage.load_event_journal()
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    recovered, cur = await handlers._drain_history_journal(AsyncMock())
    assert recovered == journal
    assert "event_projection" not in cur
    assert "event_time" not in cur
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    handlers.fetch_history.assert_not_awaited()
    await handlers.check_and_notify(AsyncMock(), set(), None)
    assert storage.load_event_journal()["journal_id"] == journal["journal_id"]
    assert storage.load_stats_current(strict=True)["event_projection"]["journal_id"] == journal["journal_id"]
    handlers._enqueue_history_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_previously_bound_journal_stops_history(history_env):
    await _ready()
    before = storage.STATS_CURRENT_FILE.read_bytes()
    storage.EVENT_JOURNAL_FILE.unlink()
    with pytest.raises(EventJournalStateError, match="bound_journal_missing"):
        await handlers.check_and_notify(AsyncMock(), set(), None)
    assert storage.STATS_CURRENT_FILE.read_bytes() == before
    handlers.fetch_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_batch_precedes_projection_and_all_sends(history_env, monkeypatch):
    await _ready()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry(3), _entry(2), _entry(3)]))
    order = []

    real_enqueue = handlers._enqueue_history_event

    async def send(journal, event, text, generation):
        journal = storage.load_event_journal()
        projection = storage.load_stats_current(strict=True)["event_projection"]
        assert [event["history_id"] for event in journal["events"]] == [2, 3]
        assert projection["applied_seq"] == journal["processed_seq"] + 1
        order.append(journal["processed_seq"] + 1)
        return await real_enqueue(journal, event, text, generation)

    monkeypatch.setattr("handlers._enqueue_history_event", send)
    await handlers.check_and_notify(AsyncMock(), {999}, None)
    assert order == [1, 2]
    assert storage.load_event_journal()["processed_seq"] == 2


@pytest.mark.asyncio
async def test_overlap_conflicts_keep_first_semantics(history_env, monkeypatch, caplog):
    await _ready()
    source = _entry()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[source]))
    await handlers.check_and_notify(AsyncMock(), set(), None)
    original = storage.EVENT_JOURNAL_FILE.read_bytes()
    changed = deepcopy(source)
    changed["description"] = "Брошено"
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[source, changed, changed]))
    await handlers.check_and_notify(AsyncMock(), set(), None)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == original
    assert sum("конфликт семантики" in message for message in caplog.messages) == 1
    handlers._enqueue_history_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_metadata_is_admitted_once_without_blocking_batch(history_env, monkeypatch):
    await _ready()
    malformed = _entry(2)
    malformed["target"] = "unavailable"
    incomplete_title = _entry(3)
    incomplete_title["target"].update(name=123, russian=["bad"], url={"bad": "url"})
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry(4), malformed, incomplete_title]))

    await handlers.check_and_notify(AsyncMock(), set(), None)

    journal = storage.load_event_journal()
    assert [event["history_id"] for event in journal["events"]] == [2, 3, 4]
    assert journal["processed_seq"] == 3
    assert journal["events"][0]["relevant"] is False
    assert journal["events"][1]["title"] == {"name": "???", "russian": "", "url": ""}
    assert storage.load_stats_current(strict=True)["event_projection"]["applied_seq"] == 3
    assert len(storage.load_stats_current(strict=True)["events"]) == 2
    assert handlers._enqueue_history_event.await_count == 3
    assert "???" in handlers._enqueue_history_event.await_args_list[1].args[2]
    original = storage.EVENT_JOURNAL_FILE.read_bytes()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry(2), _entry(3), _entry(4)]))

    await handlers.check_and_notify(AsyncMock(), set(), None)

    assert storage.EVENT_JOURNAL_FILE.read_bytes() == original
    assert handlers._enqueue_history_event.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["admission", "projection", "checkpoint"])
async def test_failed_publication_preserves_pending_and_exact_bytes(history_env, monkeypatch, boundary):
    await _ready()
    old_journal = storage.EVENT_JOURNAL_FILE.read_bytes()
    old_cur = storage.STATS_CURRENT_FILE.read_bytes()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry(), _entry(3)]))
    real_write = storage._atomic_write

    def fail(path, payload):
        should_fail = (
            boundary == "admission" and path == storage.EVENT_JOURNAL_FILE
            or boundary == "projection" and path == storage.STATS_CURRENT_FILE
            or boundary == "checkpoint" and path == storage.EVENT_JOURNAL_FILE and json.loads(payload)["processed_seq"] == 1
        )
        if should_fail:
            raise OSError("disk failure")
        return real_write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", fail)
        with pytest.raises((EventJournalStateError, storage.QuarterDeliveryStateError)):
            await handlers.check_and_notify(AsyncMock(), set(), None)
    if boundary == "admission":
        assert storage.EVENT_JOURNAL_FILE.read_bytes() == old_journal
        assert storage.STATS_CURRENT_FILE.read_bytes() == old_cur
        handlers._enqueue_history_event.assert_not_awaited()
    else:
        assert len(storage.load_event_journal()["events"]) == 2
        assert storage.load_event_journal()["processed_seq"] == 0
        if boundary == "projection":
            assert storage.STATS_CURRENT_FILE.read_bytes() == old_cur
            handlers._enqueue_history_event.assert_not_awaited()
        else:
            assert len(storage.load_stats_current(strict=True)["events"]) == 1
            handlers._enqueue_history_event.assert_awaited_once()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[]))
    await handlers.check_and_notify(AsyncMock(), set(), None)
    if boundary != "admission":
        assert storage.load_event_journal()["processed_seq"] == 2
        assert len(storage.load_stats_current(strict=True)["events"]) == 2


@pytest.mark.asyncio
async def test_crash_before_enqueue_replays_without_reapplying_projection(history_env, monkeypatch):
    await _ready()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry(), _entry(3)]))
    real_enqueue = handlers._enqueue_history_event
    monkeypatch.setattr("handlers._enqueue_history_event", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await handlers.check_and_notify(AsyncMock(), set(), None)
    assert storage.load_event_journal()["processed_seq"] == 0
    assert storage.load_stats_current(strict=True)["event_projection"]["applied_seq"] == 1
    applied = []
    real_record = handlers.project_event

    def record(cur, journal, seq, **kwargs):
        applied.append(journal["events"][seq - 1]["history_id"])
        return real_record(cur, journal, seq, **kwargs)

    monkeypatch.setattr("handlers.project_event", record)
    monkeypatch.setattr("messages.classify_event", lambda _: pytest.fail("переклассификация локального payload"))
    monkeypatch.setattr("handlers._enqueue_history_event", AsyncMock(wraps=real_enqueue))
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[]))
    await handlers.check_and_notify(AsyncMock(), set(), None)
    assert applied == [3]
    assert storage.load_event_journal()["processed_seq"] == 2
    assert len(storage.load_stats_current(strict=True)["events"]) == 2


@pytest.mark.asyncio
async def test_export_failure_cannot_override_valid_journal(history_env, monkeypatch):
    await _ready()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry()]))
    monkeypatch.setattr("handlers.save_seen_ids", lambda _: (_ for _ in ()).throw(OSError("write")))
    seen, _ = await handlers.check_and_notify(AsyncMock(), {999}, None)
    assert seen == {1, 2}
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[]))
    await handlers.check_and_notify(AsyncMock(), {999}, None)
    handlers._enqueue_history_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_capacity_rejects_whole_new_batch_without_eviction(history_env, monkeypatch):
    await _ready()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry()]))
    await handlers.check_and_notify(AsyncMock(), set(), None)
    original = storage.EVENT_JOURNAL_FILE.read_bytes()
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", len(original) + storage.JOURNAL_CHECKPOINT_RESERVE)
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry(), _entry(3)]))
    with pytest.raises(EventJournalStateError, match="journal_capacity"):
        await handlers.check_and_notify(AsyncMock(), set(), None)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == original
    handlers._enqueue_history_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_rotation_drains_pending_and_carries_projection(history_env, monkeypatch):
    await _ready()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry()]))
    real_enqueue = handlers._enqueue_history_event
    monkeypatch.setattr("handlers._enqueue_history_event", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await handlers.check_and_notify(AsyncMock(), set(), None)
    order = []

    async def send(*args, **kwargs):
        order.append("event")
        return await real_enqueue(*args, **kwargs)

    def snapshot(_period, cur, _stats):
        order.append("snapshot")
        assert storage.load_event_journal()["processed_seq"] == 1
        assert len(cur["events"]) == 1

    monkeypatch.setattr("handlers._enqueue_history_event", send)
    monkeypatch.setattr("handlers._save_quarter_snapshot", snapshot)
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    monkeypatch.setattr("handlers._deliver_pending_quarter", AsyncMock(side_effect=lambda _bot, cur: cur))
    cur = await handlers.rotate_quarter_if_needed(AsyncMock(), {}, storage._empty_stats_all(), resync=False)
    assert order == ["event", "snapshot"]
    assert cur["period"] == "2026-Q3"
    assert cur["event_projection"]["applied_seq"] == 1
    assert cur["event_projection"]["journal_id"] == storage.load_event_journal()["journal_id"]


@pytest.mark.asyncio
async def test_crash_after_admission_recovers_before_fetch(history_env, monkeypatch):
    await _ready()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry()]))
    real_drain = handlers._drain_history_journal
    calls = 0

    async def crash_after_publication(bot, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        return await real_drain(bot, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("handlers._drain_history_journal", crash_after_publication)
        with pytest.raises(asyncio.CancelledError):
            await handlers.check_and_notify(AsyncMock(), set(), None)
    assert len(storage.load_event_journal()["events"]) == 1
    assert storage.load_stats_current(strict=True)["event_projection"]["applied_seq"] == 0
    handlers._enqueue_history_event.assert_not_awaited()

    async def disappeared(_session, page=1):
        assert storage.load_event_journal()["processed_seq"] == 1
        return []

    monkeypatch.setattr("handlers.fetch_history", disappeared)
    await handlers.check_and_notify(AsyncMock(), set(), None)
    handlers._enqueue_history_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_does_not_claim_recipient_delivery(history_env, monkeypatch):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    await _ready()
    storage.save_subscribers({10: "fails", 20: "works"})
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry()]))
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramBadRequest(method=SendMessage(chat_id=10, text="event"), message="permanent"),
        object(),
    ]
    await handlers.check_and_notify(bot, set(), None)
    await dispatch_notifications(bot)
    assert bot.send_message.await_count == 2
    assert storage.load_event_journal()["processed_seq"] == 1
    recipients = storage.load_event_journal()["outbox"]["records"][0]["recipients"]
    assert recipients["10"]["status"] == "rejected"
    assert recipients["20"]["status"] == "delivered"


@pytest.mark.asyncio
async def test_capacity_warning_is_static_and_debounced(history_env, monkeypatch):
    await _ready()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry()]))
    monkeypatch.setattr("handlers.JOURNAL_WARN_BYTES", 1)
    monkeypatch.setattr("handlers._last_journal_capacity_notice_at", None)
    bot = AsyncMock()
    await handlers.check_and_notify(bot, set(), None)
    await handlers.check_and_notify(bot, set(), None)
    bot.send_message.assert_awaited_once_with(chat_id=handlers.OWNER_ID, text=handlers._JOURNAL_CAPACITY_NOTICE)


@pytest.mark.asyncio
async def test_capacity_warning_counts_pending_recipient_reserve(history_env, monkeypatch):
    from event_journal_schema import journal_json
    from notification_outbox import progress_reserve

    await _ready()
    storage.save_subscribers({10: "pending"})
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry()]))
    bot = AsyncMock()
    await handlers.check_and_notify(bot, set(), None)
    journal = storage.load_event_journal()
    used = len(journal_json(journal).encode())
    reserve = progress_reserve(journal)
    assert reserve > 0
    monkeypatch.setattr("handlers.JOURNAL_WARN_BYTES", used + reserve)
    monkeypatch.setattr("handlers._last_journal_capacity_notice_at", None)
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[]))
    await handlers.check_and_notify(bot, set(), None)
    bot.send_message.assert_awaited_once_with(
        chat_id=handlers.OWNER_ID, text=handlers._JOURNAL_CAPACITY_NOTICE
    )


@pytest.mark.asyncio
async def test_baseline_bad_metadata_does_not_block_new_events(history_env, monkeypatch):
    await _ready()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[{"id": 1, "target": "bad"}, _entry()]))
    await handlers.check_and_notify(AsyncMock(), set(), None)
    assert storage.load_event_journal()["processed_seq"] == 1
    handlers._enqueue_history_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_consumers_cannot_overtake_unfinished_enqueue(history_env, journal_factory, monkeypatch):
    journal = journal_factory(count=2)
    storage.save_event_journal(journal)
    storage.save_stats_current({
        "period": "2026-Q2", "events": [],
        "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0},
    }, strict=True)
    started = asyncio.Event()
    resume = asyncio.Event()
    sends = []

    real_enqueue = handlers._enqueue_history_event

    async def send(*args, **kwargs):
        seq = storage.load_stats_current(strict=True)["event_projection"]["applied_seq"]
        sends.append(seq)
        if seq == 1:
            started.set()
            await resume.wait()
        return await real_enqueue(*args, **kwargs)

    monkeypatch.setattr("handlers._enqueue_history_event", send)
    first = asyncio.create_task(handlers._drain_history_journal(AsyncMock()))
    await started.wait()
    second = asyncio.create_task(handlers._drain_history_journal(AsyncMock()))
    # Дать второй задаче дойти до собственного lock без подменённого sleep.
    loop = asyncio.get_running_loop()
    turn = loop.create_future()
    loop.call_soon(turn.set_result, None)
    await turn
    assert sends == [1]
    resume.set()
    await asyncio.gather(first, second)
    assert sends == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [None, [], [1]])
async def test_startup_privacy_failure_does_not_publish_journal_baseline(history_env, monkeypatch, legacy):
    from shiki_api import ProfilePrivacyError

    if legacy is not None:
        storage.save_seen_ids(set(legacy))
    original = storage.STATS_CURRENT_FILE.read_bytes()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry()]))
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: {"animes_10"})
    monkeypatch.setattr("handlers.fetch_favourites", AsyncMock(return_value={}))
    monkeypatch.setattr("handlers.sync_stats_all", AsyncMock(side_effect=ProfilePrivacyError("list_export")))
    monkeypatch.setattr("handlers.check_and_notify", AsyncMock(side_effect=asyncio.CancelledError))
    monkeypatch.setattr("handlers._backup_after_subscription", AsyncMock())
    monkeypatch.setattr("handlers._weekly_backup_if_due", AsyncMock(side_effect=lambda _bot, cur: cur))
    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(AsyncMock())
    assert storage.load_event_journal() is None
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    assert storage.load_legacy_seen_ids() == (set(legacy) if legacy is not None else None)
    handlers._enqueue_history_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotation_defers_batch_admitted_while_report_is_prepared(history_env, journal_factory, monkeypatch):
    journal = journal_factory(count=0)
    storage.save_event_journal(journal)
    storage.save_stats_current({
        "period": "2026-Q2", "events": [],
        "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0},
    }, strict=True)
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    real_freeze = handlers.freeze_report

    def admit(report):
        storage.save_event_journal(journal_factory(), admitting=True)
        return real_freeze(report)

    monkeypatch.setattr("handlers.freeze_report", admit)
    snapshot = AsyncMock()
    monkeypatch.setattr("handlers._save_quarter_snapshot", snapshot)
    current = await handlers.rotate_quarter_if_needed(AsyncMock(), {}, storage._empty_stats_all(), resync=False)
    assert current["period"] == "2026-Q2"
    assert current["pending_quarter_delivery"] is None
    assert storage.load_event_journal()["processed_seq"] == 0
    snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_corrupt_journal_suspends_history_keeps_recovery_and_resumes(history_env, journal_factory, monkeypatch):
    original = b"{broken journal"
    storage.EVENT_JOURNAL_FILE.write_bytes(original)
    original_cur = storage.STATS_CURRENT_FILE.read_bytes()
    monkeypatch.setattr("handlers._last_journal_notice_at", None)
    monkeypatch.setattr("handlers.fetch_favourites", AsyncMock(return_value={}))
    monkeypatch.setattr("handlers.sync_stats_all", AsyncMock(return_value=(storage._empty_stats_all(), False)))
    subscription = AsyncMock()
    weekly = AsyncMock(side_effect=lambda _bot, cur: cur)
    favourites = AsyncMock(return_value=({"animes_10"}, False))
    monkeypatch.setattr("handlers._backup_after_subscription", subscription)
    monkeypatch.setattr("handlers._weekly_backup_if_due", weekly)
    monkeypatch.setattr("handlers.check_and_notify_favourites", favourites)
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: {"animes_10"})

    async def sleep(delay):
        if delay == handlers.CHECK_INTERVAL:
            raise asyncio.CancelledError

    monkeypatch.setattr("handlers.asyncio.sleep", sleep)
    bot = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(bot)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == original
    assert storage.STATS_CURRENT_FILE.read_bytes() == original_cur
    handlers.fetch_history.assert_not_awaited()
    handlers._enqueue_history_event.assert_not_awaited()
    favourites.assert_awaited_once_with(bot, {"animes_10"}, favourites={})
    assert subscription.await_count == 2
    weekly.assert_awaited_once_with(bot, storage.load_stats_current(strict=True))
    bot.send_message.assert_awaited_once_with(chat_id=handlers.OWNER_ID, text=handlers._JOURNAL_STATE_NOTICE)
    journal = journal_factory(count=0)
    cur = {"period": "2026-Q2", "events": [], "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("event_journal.json", json.dumps(journal))
        archive.writestr("stats_current.json", json.dumps(cur))
    await backup.restore_backup_zip(buf.getvalue())
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[_entry()]))
    await handlers.check_and_notify(bot, {999}, None)
    assert storage.load_event_journal()["processed_seq"] == 1
    handlers._enqueue_history_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_journal_reads_do_not_scale_with_recipients_or_retries(history_env, journal_factory, monkeypatch):
    from aiogram.exceptions import TelegramServerError
    from aiogram.methods import SendMessage

    journal = journal_factory(count=2)
    storage.save_event_journal(journal)
    storage.save_stats_current({
        "period": "2026-Q2", "events": [],
        "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0},
    }, strict=True)
    storage.save_subscribers({10: "first", 20: "second", 30: "third"})
    real_load = storage.load_event_journal
    reads = []

    def load():
        result = real_load()
        reads.append(result["processed_seq"])
        return result

    monkeypatch.setattr("handlers.load_event_journal", load)
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramServerError(method=SendMessage(chat_id=10, text="event"), message="temporary"),
        *[object() for _ in range(6)],
    ]
    await handlers._drain_history_journal(bot)
    bot.send_message.assert_not_awaited()
    assert all(len(record["recipients"]) == 3 for record in storage.load_event_journal()["outbox"]["records"])
    assert reads == [0, 0, 1]
    assert storage.load_event_journal()["processed_seq"] == 2
    projected = storage.load_stats_current(strict=True)
    assert projected["events"] == []
    assert len(projected["event_time"]["periods"]["2026-Q1"]["events"]) == 2


@pytest.mark.asyncio
async def test_exhausted_uncertain_recipient_still_means_only_durable_enqueue(
    history_env, monkeypatch, journal_factory,
):
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    journal = journal_factory()
    storage.save_event_journal(journal, admitting=True)
    storage.save_stats_current({
        "period": "2026-Q2", "events": [],
        "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0},
    }, strict=True)
    storage.save_subscribers({7: "lost", 8: "blocked", 9: "healthy"})
    monkeypatch.setattr("telegram_delivery._sleep", AsyncMock())
    calls = []

    async def send(**kwargs):
        calls.append(kwargs["chat_id"])
        if kwargs["chat_id"] == 7:
            raise TimeoutError()
        if kwargs["chat_id"] == 8:
            raise TelegramForbiddenError(method=SendMessage(chat_id=8, text="test"), message="forbidden")

    bot = AsyncMock()
    bot.send_message.side_effect = send
    await handlers._drain_history_journal(bot)
    await dispatch_notifications(bot)
    assert calls == [7, 8, 9]
    assert storage.load_event_journal()["outbox"]["records"][0]["recipients"]["7"]["status"] == "pending"
    assert storage.load_event_journal()["processed_seq"] == 1
    assert set(storage.load_subscribers()) == {7, 9}
    assert storage.load_stats_current(strict=True)["event_projection"]["applied_seq"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["migration", "membership", "enqueue_capacity"])
async def test_outbox_publication_failure_keeps_recoverable_authority(history_env, journal_factory, monkeypatch, phase):
    journal = journal_factory()
    storage.save_event_journal(journal)
    storage.save_stats_current({
        "period": "2026-Q2", "events": [],
        "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0},
    }, strict=True)
    storage.SUBS_FILE.write_text('{"subscribers":{"10":"legacy"}}', encoding="utf-8")
    before_journal = storage.EVENT_JOURNAL_FILE.read_bytes()
    before_members = storage.SUBS_FILE.read_bytes()
    write = storage._atomic_write
    def fail(path, payload):
        if phase == "membership" and path == storage.SUBS_FILE:
            raise OSError("memberships")
        if path == storage.EVENT_JOURNAL_FILE:
            candidate = json.loads(payload)
            if phase == "migration" and candidate["version"] == 3:
                raise OSError("migration")
            if phase == "enqueue_capacity" and candidate["processed_seq"] == 1:
                raise EventJournalStateError("journal_capacity")
        return write(path, payload)
    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", fail)
        with pytest.raises(EventJournalStateError):
            await handlers._drain_history_journal(AsyncMock())
    assert storage.load_event_journal()["processed_seq"] == 0
    if phase == "migration":
        assert storage.EVENT_JOURNAL_FILE.read_bytes() == before_journal
    if phase in {"migration", "membership"}:
        assert storage.SUBS_FILE.read_bytes() == before_members
    await handlers._drain_history_journal(AsyncMock())
    assert storage.load_event_journal()["processed_seq"] == 1
    assert len(storage.load_event_journal()["outbox"]["records"]) == 1
