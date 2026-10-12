# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Многоцикловый сбор без преждевременного admission и ротации."""

import asyncio
from copy import deepcopy
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest

import handlers
import storage
from event_journal_schema import EventJournalStateError


def _entry(value):
    return {
        "id": value, "description": "Просмотрено", "created_at": "2026-01-01T00:00:00+00:00",
        "target": {"id": value, "kind": "tv", "name": str(value)},
    }


@pytest.fixture
async def acquisition_env(backup_env, monkeypatch):
    storage.save_stats_current({"period": "2026-Q2", "events": []}, strict=True)
    await handlers._initialize_history_journal({1}, storage.restorable_restore_generation())
    monkeypatch.setattr("handlers.HISTORY_PAGE_LIMIT", 3)
    monkeypatch.setattr("handlers.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("handlers._enqueue_history_event", AsyncMock(wraps=handlers._enqueue_history_event))
    return backup_env


def _source(monkeypatch):
    # Значения ID и created_at заведомо не являются порядком обхода.
    rows = [_entry(value) for value in [*range(2, 34, 2), *range(3, 34, 2), 1]]
    calls = []

    async def fetch(_session, page=1):
        calls.append(page)
        start = (page - 1) * 3
        return deepcopy(rows[start:start + 4])

    monkeypatch.setattr("handlers.fetch_history", fetch)
    return rows, calls


async def _cycle(calls):
    calls.clear()
    result = await handlers.check_and_notify(AsyncMock(), {999}, None)
    assert len(calls) <= 5
    return result


async def _finish(calls):
    for _ in range(20):
        await _cycle(calls)
        if storage.load_event_journal()["catchup"] is None:
            return storage.load_event_journal()
    pytest.fail("сбор не завершился после стабилизации source")


@pytest.mark.asyncio
async def test_long_history_survives_restart_and_only_complete_batch_is_processed(acquisition_env, monkeypatch):
    rows, calls = _source(monkeypatch)
    seen, cur = await _cycle(calls)
    assert calls == [1, 2, 3, 4, 5]
    journal = storage.load_event_journal()
    assert journal["catchup"]["page"] == 6
    assert journal["events"] == []
    assert seen == {1}
    assert cur["event_projection"]["applied_seq"] == 0
    assert storage.load_seen_ids() != {row["id"] for row in rows}
    handlers._enqueue_history_event.assert_not_awaited()
    # Следующий вызов строит всё из диска, без процесса/cursor из прошлого цикла.
    await _cycle(calls)
    assert calls[0] == 5
    handlers._enqueue_history_event.assert_not_awaited()
    journal = await _finish(calls)
    assert [event["history_id"] for event in journal["events"]] == list(range(2, 34))
    assert journal["processed_seq"] == 32
    plan = journal["outbox"]["plans"][0]
    assert plan["events"] == [[event["seq"], event["history_id"]] for event in journal["events"]]
    assert plan["start_seq"] == 1 and plan["end_seq"] == 32
    assert [ref[1] for unit in plan["units"] for ref in unit["events"]] == list(range(2, 34))
    projected = storage.load_stats_current(strict=True)
    assert projected["events"] == []
    assert len(projected["event_time"]["periods"]["2026-Q1"]["events"]) == 32
    assert handlers._enqueue_history_event.await_count == 32
    await _cycle(calls)
    assert handlers._enqueue_history_event.await_count == 32


@pytest.mark.asyncio
async def test_coalescing_across_page_seam_waits_for_completed_acquisition(acquisition_env, monkeypatch):
    rows = [_entry(value) for value in [6, 5, 4, 3, 2, 1]]
    for row in rows:
        if row["id"] in {3, 4}:
            row["target"]["id"] = 11
            row["description"] = "Добавлено в список" if row["id"] == 3 else "Просмотрено и оценено на 8"
            row["created_at"] = "2026-04-01T00:00:00.990+00:00" if row["id"] == 3 else "2026-04-01T00:00:01.010+00:00"
    calls = []
    unavailable = [True]

    async def fetch(_session, page=1):
        calls.append(page)
        if page == 2 and unavailable[0]:
            return None
        start = (page - 1) * 3
        return deepcopy(rows[start:start + 4])

    monkeypatch.setattr("handlers.fetch_history", fetch)
    storage.save_subscribers({10: "only"})
    await handlers.check_and_notify(AsyncMock(), set(), None)
    staged = storage.load_event_journal()
    assert staged["catchup"] is not None and staged["events"] == []
    assert staged["processed_seq"] == 0 and not staged["outbox"].get("plans")
    handlers._enqueue_history_event.assert_not_awaited()
    unavailable[0] = False
    storage._journal_history_cache = None
    await handlers.check_and_notify(AsyncMock(), set(), None)
    ready = storage.load_event_journal()
    assert ready["catchup"] is None and ready["processed_seq"] == 5
    plan = ready["outbox"]["plans"][0]
    assert plan["version"] == 3
    assert {"events": [[2, 3], [3, 4]], "notification_seq": 3} in plan["entries"]
    assert plan["events"] == [[seq, seq + 1] for seq in range(1, 6)]
    assert calls == [1, 2, 1, 2, 1]
    await _cycle(calls)
    assert handlers._enqueue_history_event.await_count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["insert", "delete_prefix", "delete_anchor", "delete_head"])
async def test_page_shifts_reconnect_or_restart_and_bridge_head(acquisition_env, monkeypatch, mutation):
    rows, calls = _source(monkeypatch)
    await _cycle(calls)
    staged = deepcopy(storage.load_event_journal()["catchup"]["staged"])
    if mutation == "insert":
        rows[:0] = [_entry(value) for value in range(70, 82)]
    elif mutation == "delete_prefix":
        del rows[:10]
    elif mutation == "delete_head":
        head = set(storage.load_event_journal()["catchup"]["head_ids"])
        rows[:] = [row for row in rows if row["id"] not in head]
    else:
        anchor = storage.load_event_journal()["catchup"]["frontier"][-1]
        rows[:] = [row for row in rows if row["id"] != anchor]
    journal = await _finish(calls)
    expected = {row["id"] for row in rows} | {event["history_id"] for event in staged}
    assert {event["history_id"] for event in journal["events"]} == expected - {1}
    assert journal["processed_seq"] == len(expected - {1})
    by_id = {event["history_id"]: event for event in journal["events"]}
    for event in staged:
        assert by_id[event["history_id"]]["observed_at"] == event["observed_at"]


@pytest.mark.asyncio
async def test_disconnected_short_known_boundary_does_not_admit(acquisition_env, monkeypatch):
    rows, calls = _source(monkeypatch)

    async def fetch(_session, page=1):
        calls.append(page)
        return rows[:4] if page == 1 else [_entry(1)]

    monkeypatch.setattr("handlers.fetch_history", fetch)
    await _cycle(calls)
    assert storage.load_event_journal()["events"] == []
    assert storage.load_event_journal()["catchup"] is not None
    handlers._enqueue_history_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["fetch", "cancel", "write", "capacity"])
async def test_saved_acquisition_survives_failures_without_sending(acquisition_env, monkeypatch, failure):
    rows, calls = _source(monkeypatch)
    await _cycle(calls)
    original = storage.EVENT_JOURNAL_FILE.read_bytes()
    before = storage.load_event_journal()["catchup"]["staged"]
    with monkeypatch.context() as patch:
        if failure in {"fetch", "cancel"}:
            patch.setattr("handlers.fetch_history", AsyncMock(
                return_value=None, side_effect=asyncio.CancelledError if failure == "cancel" else None,
            ))
        elif failure == "write":
            patch.setattr("storage._atomic_write", lambda *_: (_ for _ in ()).throw(OSError("disk")))
        else:
            patch.setattr("storage.JOURNAL_MAX_BYTES", len(original) + storage.JOURNAL_CHECKPOINT_RESERVE)
        if failure == "fetch":
            await handlers.check_and_notify(AsyncMock(), set(), None)
            await handlers.check_and_notify(AsyncMock(), set(), None)
            assert storage.EVENT_JOURNAL_FILE.read_bytes() == original
        else:
            with pytest.raises((EventJournalStateError, asyncio.CancelledError)):
                await handlers.check_and_notify(AsyncMock(), set(), None)
            if failure != "capacity":
                assert storage.EVENT_JOURNAL_FILE.read_bytes() == original
    assert storage.load_event_journal()["catchup"]["staged"] == before
    assert storage.load_event_journal()["events"] == []
    handlers._enqueue_history_event.assert_not_awaited()
    journal = await _finish(calls)
    assert journal["processed_seq"] == len(rows) - 1


@pytest.mark.asyncio
async def test_incomplete_acquisition_blocks_rotation_until_completion(acquisition_env, monkeypatch):
    _, calls = _source(monkeypatch)
    await _cycle(calls)
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    snapshot = MagicMock()
    sync = AsyncMock()
    monkeypatch.setattr("handlers._save_quarter_snapshot", snapshot)
    monkeypatch.setattr("handlers.sync_stats_all", sync)
    cur = await handlers.rotate_quarter_if_needed(AsyncMock(), {}, storage._empty_stats_all())
    assert cur["period"] == "2026-Q2"
    snapshot.assert_not_called()
    sync.assert_not_awaited()
    await _finish(calls)
    monkeypatch.setattr("handlers._deliver_pending_quarter", AsyncMock(side_effect=lambda _bot, cur: cur))
    cur = await handlers.rotate_quarter_if_needed(AsyncMock(), {}, storage._empty_stats_all(), resync=False)
    assert cur["period"] == "2026-Q3"
    snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_polling_startup_waits_and_other_duties_continue(acquisition_env, monkeypatch):
    _, calls = _source(monkeypatch)
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: {"animes_10"})
    monkeypatch.setattr("handlers.fetch_favourites", AsyncMock(return_value={}))
    sync = AsyncMock(return_value=(storage._empty_stats_all(), False))
    monkeypatch.setattr("handlers.sync_stats_all", sync)
    fav = AsyncMock(return_value=({"animes_10"}, False))
    subscription = AsyncMock()
    weekly = AsyncMock(side_effect=lambda _bot, cur: cur)
    monkeypatch.setattr("handlers.check_and_notify_favourites", fav)
    monkeypatch.setattr("handlers._backup_after_subscription", subscription)
    monkeypatch.setattr("handlers._weekly_backup_if_due", weekly)

    async def sleep(delay):
        if delay >= handlers._NOTIFICATION_INTERVAL:
            raise asyncio.CancelledError

    monkeypatch.setattr("handlers.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(AsyncMock())
    assert calls == [1, 2, 3, 4, 5]
    assert storage.load_stats_current(strict=True)["period"] == "2026-Q2"
    assert storage.load_event_journal()["catchup"] is not None
    fav.assert_awaited_once()
    assert sync.await_count == 2
    assert subscription.await_count == 2
    assert weekly.await_count == 2
    handlers._enqueue_history_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_admission_failure_keeps_staged_payload_and_sends_nothing(acquisition_env, monkeypatch):
    rows, calls = _source(monkeypatch)
    await _cycle(calls)
    await _cycle(calls)
    real_write = storage._atomic_write
    snapshots = []

    def fail_final(path, payload):
        import json

        if path == storage.EVENT_JOURNAL_FILE and json.loads(payload).get("catchup") is None:
            snapshots.append(storage.EVENT_JOURNAL_FILE.read_bytes())
            raise OSError("admission")
        return real_write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", fail_final)
        with pytest.raises(EventJournalStateError, match="journal_write"):
            await _cycle(calls)
    assert snapshots == [storage.EVENT_JOURNAL_FILE.read_bytes()]
    assert storage.load_event_journal()["catchup"]["phase"] == "head"
    assert storage.load_event_journal()["events"] == []
    assert storage.load_stats_current(strict=True)["event_projection"]["applied_seq"] == 0
    handlers._enqueue_history_event.assert_not_awaited()
    journal = await _finish(calls)
    assert journal["processed_seq"] == len(rows) - 1


@pytest.mark.asyncio
async def test_v1_is_read_without_replay_and_upgrades_on_acquisition(acquisition_env, journal_factory, monkeypatch):
    journal = journal_factory(count=1, processed=1)
    storage.save_event_journal(journal)
    cur = storage.load_stats_current(strict=True)
    cur["event_projection"].update(journal_id=journal["journal_id"], applied_seq=1)
    # Старый бинарник ещё не мог записать source-time authority.
    cur.pop("event_time")
    storage.save_stats_current(cur, strict=True)
    _source(monkeypatch)
    await handlers.check_and_notify(AsyncMock(), set(), None)
    # ID 2 на первой странице — точная граница; остальные ID принимаются.
    upgraded = storage.load_event_journal()
    assert upgraded["version"] == 3
    assert upgraded["events"][0] == journal["events"][0]
    assert upgraded["baseline_ids"] == [1]
    assert [event["history_id"] for event in upgraded["events"]] == [2, 4, 6, 8]
    assert handlers._enqueue_history_event.await_count == 3


@pytest.mark.asyncio
async def test_static_gap_beyond_budget_eventually_delivers(acquisition_env, monkeypatch):
    rows, calls = _source(monkeypatch)
    for _ in range(3):
        await _cycle(calls)
    assert storage.load_seen_ids() == {row["id"] for row in rows}
    assert handlers._enqueue_history_event.await_count == len(rows) - 1


@pytest.mark.asyncio
async def test_acquisition_started_during_rendering_defers_rotation(
    acquisition_env, acquisition_factory, monkeypatch,
):
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    real_freeze = handlers.freeze_report

    def acquire(report):
        journal = acquisition_factory()
        journal["journal_id"] = storage.load_event_journal()["journal_id"]
        storage.save_event_journal(journal, admitting=True)
        return real_freeze(report)

    monkeypatch.setattr("handlers.freeze_report", acquire)
    snapshot = MagicMock()
    monkeypatch.setattr("handlers._save_quarter_snapshot", snapshot)
    cur = await handlers.rotate_quarter_if_needed(AsyncMock(), {}, storage._empty_stats_all(), resync=False)
    assert cur["period"] == "2026-Q2"
    assert storage.load_event_journal()["catchup"] is not None
    snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_existing_frozen_plan_remains_deliverable_during_acquisition(acquisition_env, monkeypatch):
    _, calls = _source(monkeypatch)
    await _cycle(calls)
    cur = storage.load_stats_current(strict=True)
    cur["pending_quarter_delivery"] = storage.new_quarter_delivery_plan("2026-Q1", "2026-Q2", [])
    storage.save_stats_current(cur, strict=True)
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    delivered = AsyncMock(side_effect=lambda _bot, state: state)
    monkeypatch.setattr("handlers._deliver_pending_quarter", delivered)
    returned = await handlers.rotate_quarter_if_needed(AsyncMock(), {}, storage._empty_stats_all(), resync=False)
    assert returned == cur
    delivered.assert_awaited_once()
    assert storage.load_event_journal()["catchup"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_failed_first_page_defers_rotation_until_history_is_verified(acquisition_env, monkeypatch, version):
    journal = storage.load_event_journal()
    if version == 1:
        journal.pop("catchup")
        journal["version"] = 1
        storage.save_event_journal(journal)
    original_cur = storage.load_stats_current(strict=True)
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr("handlers.fetch_history", fetch)
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    monkeypatch.setattr("handlers._deliver_pending_quarter", AsyncMock(side_effect=lambda _bot, state: state))
    snapshot = MagicMock()
    monkeypatch.setattr("handlers._save_quarter_snapshot", snapshot)

    seen, returned = await handlers.check_and_notify(AsyncMock(), set(), None)
    assert seen == {1}
    assert returned == original_cur
    cur = await handlers.rotate_quarter_if_needed(AsyncMock(), returned, storage._empty_stats_all(), resync=False)
    assert cur["period"] == "2026-Q2"
    snapshot.assert_not_called()
    handlers._enqueue_history_event.assert_not_awaited()
    pending = storage.load_event_journal()
    assert {key: value for key, value in pending.items() if key not in {"version", "catchup", "outbox"}} == {
        key: value for key, value in journal.items() if key not in {"version", "catchup", "outbox"}
    }
    assert pending["catchup"]["staged"] == []
    assert fetch.await_count == 1

    fetch.return_value = []
    await handlers.check_and_notify(AsyncMock(), seen, returned)
    cur = await handlers.rotate_quarter_if_needed(AsyncMock(), returned, storage._empty_stats_all(), resync=False)
    assert cur["period"] == "2026-Q3"
    snapshot.assert_called_once()
    assert storage.load_event_journal()["catchup"] is None
    handlers._enqueue_history_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_compacted_old_id_reconnects_resumed_overlap_without_readmission(
    acquisition_env, source_history_factory, monkeypatch, caplog,
):
    from messages import history_entry_from_event
    from source_history import (
        event_count,
        prefix_seq,
    )
    full, cur = source_history_factory(count=8, padding=0)
    old = history_entry_from_event(full["events"][0])
    old["created_at"] = full["events"][0]["created_at"]
    storage.save_event_journal(full)
    storage.save_stats_current(cur, strict=True)
    compact = storage.compact_completed_history(
        full, storage.load_stats_current(strict=True),
        expected_generation=storage.restorable_restore_generation(), force=True,
    )
    assert prefix_seq(compact) == 8
    rows = [_entry(value) for value in range(60, 40, -1)] + [old]
    calls = []

    async def fetch(_session, page=1):
        calls.append(page)
        start = (page - 1) * 3
        return deepcopy(rows[start:start + 4])

    monkeypatch.setattr("handlers.fetch_history", fetch)
    seen, _ = await handlers.check_and_notify(AsyncMock(), set(), None)
    first = storage.load_event_journal()
    assert first["catchup"] is not None
    assert first["processed_seq"] == 8
    assert seen == set(range(1, 10))
    handlers._enqueue_history_event.assert_not_awaited()
    storage._journal_history_cache = storage._journal_progress_cache = None
    for _ in range(6):
        seen, _ = await handlers.check_and_notify(AsyncMock(), set(), None)
        if storage.load_event_journal()["catchup"] is None:
            break
    recovered = storage.load_event_journal()
    assert recovered["catchup"] is None
    assert prefix_seq(recovered) == 8
    assert event_count(recovered) == recovered["processed_seq"] == 28
    assert seen == set(range(1, 10)) | set(range(41, 61))
    assert [ev["seq"] for ev in recovered["events"]] == list(range(9, 29))
    assert not any("конфликт семантики" in text for text in caplog.messages)
    changed = deepcopy(old)
    changed["description"] = "Брошено"
    before = storage.EVENT_JOURNAL_FILE.read_bytes()
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=[old, changed, changed]))
    await handlers.check_and_notify(AsyncMock(), set(), None)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == before
    assert sum("конфликт семантики" in text for text in caplog.messages) == 1
