# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Admission, frozen preparation, projection/enqueue и cold resume digest."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import (
    AsyncMock,
    Mock,
)

import pytest

import handlers
import notification_delivery
import storage
from event_journal_schema import EventJournalStateError


def _install(journal):
    storage.save_stats_current({"period": "2026-Q2", "events": [], "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0}}, strict=True)
    storage.save_subscribers({10: "only"})
    storage.save_event_journal(journal)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_coalescing_counts_entries_and_keeps_independent_quarters(digest_env, coalescing_journal_factory, monkeypatch, count):
    journal = coalescing_journal_factory(count=count)
    _install(journal)
    monkeypatch.setattr("messages.random.choice", lambda bank: bank[0])
    completion = handlers.build_message(handlers.history_entry_from_event(journal["events"][1]), normalized=journal["events"][1])
    await handlers._drain_history_journal(AsyncMock())
    saved = storage.load_event_journal()
    assert saved["events"] == journal["events"]
    plan = saved["outbox"]["plans"][0]
    assert plan["version"] == 3
    assert plan["entries"][0] == {"events": [[1, 2], [2, 3]], "notification_seq": 2}
    bot = AsyncMock()
    await notification_delivery.dispatch_notifications(bot)
    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert ("Что нового у" in text) == (count == 3)
    assert completion in text
    if count == 2:
        assert text == completion
    current = storage.load_stats_current(strict=True)
    assert current["event_projection"]["applied_seq"] == count
    assert any(event["event"] == "planned" for event in current["event_time"]["periods"]["2026-Q1"]["events"])
    assert any(event["event"] == "completed" for event in current["event_time"]["periods"]["2026-Q2"]["events"])


@pytest.mark.asyncio
@pytest.mark.parametrize("media,kind,suffix", [("anime", "tv", "аниме"), ("manga", "manga", "манга"), ("manga", "novel", "ранобэ")])
@pytest.mark.parametrize("score", [None, 8])
async def test_coalescing_reuses_normalized_completion_without_description_parsing(digest_env, coalescing_journal_factory, monkeypatch, media, kind, suffix, score):
    journal = coalescing_journal_factory()
    for event in journal["events"]:
        event.update(media=media, kind=kind, description="Полностью другое исходное описание")
    journal["events"][1]["score"] = score
    journal["events"][1]["title"].update(name='<Title & "😀">', url=f"/{media}s/11")
    _install(journal)
    monkeypatch.setattr("messages.classify_event", lambda *a: pytest.fail("published semantics reparsed"))
    await handlers._drain_history_journal(AsyncMock())
    plan = storage.load_event_journal()["outbox"]["plans"][0]
    text = plan["units"][0]["payload"]["text"]
    assert f"({suffix})" in text and "&lt;Title &amp; &quot;😀&quot;&gt;" in text
    assert f'https://shikimori.io/{media}s/11' in text
    assert ("8/10" in text) == (score == 8)
    assert "Полностью другое" not in text


@pytest.mark.asyncio
async def test_coalescing_local_fallback_keeps_pairs_and_freezes_all_ordinary_parts(digest_env, coalescing_journal_factory, monkeypatch):
    _install(coalescing_journal_factory(count=3, long_title=True))
    monkeypatch.setattr("handlers.render_digest", Mock(side_effect=ValueError("local rendering")))
    await handlers._drain_history_journal(AsyncMock())
    before = storage.load_event_journal()["outbox"]["plans"][0]
    assert before["presentation"] == "ordinary" and len(before["entries"]) == 2
    parts = [unit for unit in before["units"] if unit["entries"] == [0]]
    assert len(parts) > 1 and all(unit["events"] == [[1, 2], [2, 3]] for unit in parts)
    monkeypatch.setattr("handlers.build_message", lambda *a, **k: pytest.fail("fallback rerender"))
    await handlers._drain_history_journal(AsyncMock())
    bot = AsyncMock()
    await notification_delivery.dispatch_notifications(bot)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == [unit["payload"]["text"] for unit in before["units"]]
    assert all("Что нового у" not in unit["payload"]["text"] for unit in before["units"])


@pytest.mark.asyncio
async def test_multiple_coalesced_pairs_count_as_two_summary_entries(digest_env, coalescing_journal_factory):
    journal = coalescing_journal_factory(count=4)
    first, last = journal["events"][2:]
    first.update(event_type="planned", score=None)
    last["target_id"] = first["target_id"]
    _install(journal)
    await handlers._drain_history_journal(AsyncMock())
    plan = storage.load_event_journal()["outbox"]["plans"][0]
    assert plan["presentation"] == "digest"
    assert plan["entries"] == [
        {"events": [[1, 2], [2, 3]], "notification_seq": 2},
        {"events": [[3, 4], [4, 5]], "notification_seq": 4},
    ]
    assert plan["units"][0]["entries"] == [0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_old_frozen_plan_with_qualifying_pair_is_never_regrouped(digest_env, coalescing_journal_factory, monkeypatch, version):
    from catchup_digest import render_digest
    from notification_outbox import (
        migrate_outbox,
        prepare_digest,
    )

    journal = migrate_outbox(coalescing_journal_factory(count=11), 0)
    parts = render_digest(journal["events"], ordinary=lambda event: f"Старая запись {event['seq']}", heading="Старый заголовок")
    journal = prepare_digest(journal, parts, {10: "b" * 32}, 1800000000.0)
    journal["outbox"]["plans"][0]["version"] = version
    _install(journal)
    state = storage.load_subscriber_state(strict_subscribers=True)
    state.notification_memberships = {10: "b" * 32}
    storage.save_subscriber_state(state)
    previous = deepcopy(journal["outbox"]["plans"])
    monkeypatch.setattr("handlers.notification_entries", lambda *a: pytest.fail("old plan regrouped"))
    monkeypatch.setattr("handlers.build_message", lambda *a, **k: pytest.fail("old plan rerendered"))
    monkeypatch.setattr("notification_delivery.time.time", lambda: 1800000000.0)
    await handlers._drain_history_journal(AsyncMock())
    assert storage.load_event_journal()["outbox"]["plans"] == previous
    bot = AsyncMock()
    await notification_delivery.dispatch_notifications(bot)
    assert bot.send_message.await_args.kwargs["text"] == previous[0]["units"][0]["payload"]["text"]


@pytest.mark.asyncio
async def test_pair_during_bootstrap_remains_silent(digest_env, coalescing_journal_factory, monkeypatch):
    events = coalescing_journal_factory()["events"]
    rows = [{**handlers.history_entry_from_event(event), "created_at": event["created_at"]} for event in events]
    storage.save_stats_current({"period": "2026-Q2", "events": []}, strict=True)
    storage.save_subscribers({10: "only"})
    monkeypatch.setattr("handlers.fetch_history", AsyncMock(return_value=rows))
    bot = AsyncMock()
    await handlers.check_and_notify(bot, set(), None)
    await notification_delivery.dispatch_notifications(bot)
    bot.send_message.assert_not_awaited()
    journal = storage.load_event_journal()
    assert journal["baseline_ids"] == [2, 3] and journal["events"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_coalescing_never_joins_old_enqueued_or_possibly_sent_source(digest_env, coalescing_journal_factory, monkeypatch, legacy):
    from event_time_stats import (
        ensure_event_time,
        project_event,
    )

    journal = coalescing_journal_factory()
    if not legacy:
        journal["events"] = journal["events"][:1]
    _install(journal)
    if legacy:
        cur = storage.load_stats_current(strict=True)
        ensure_event_time(cur)
        project_event(cur, journal, 1)
        cur["event_projection"]["applied_seq"] = 1
        storage.save_stats_current(cur, strict=True)
    else:
        await handlers._drain_history_journal(AsyncMock())
        journal = storage.load_event_journal()
        previous = deepcopy(journal["outbox"]["records"][0])
        journal["events"].append(coalescing_journal_factory()["events"][1])
        storage.save_event_journal(journal, admitting=True)
    await handlers._drain_history_journal(AsyncMock())
    saved = storage.load_event_journal()
    assert not saved["outbox"].get("plans")
    if legacy:
        assert saved["outbox"]["records"][0]["recipients"]["10"]["prior_possible"]
    else:
        assert saved["outbox"]["records"][0] == previous
    assert len(saved["outbox"]["records"]) == 2


@pytest.fixture
def digest_env(backup_env, monkeypatch):
    monkeypatch.setattr("notification_delivery.asyncio.sleep", AsyncMock())
    return backup_env


@pytest.mark.asyncio
@pytest.mark.parametrize("count,units", [(1, 1), (2, 1), (8, 1), (9, 1), (10, 1), (11, 1)])
async def test_every_completed_batch_reduces_sends_without_losing_event_projections(digest_env, journal_factory, count, units):
    _install(journal_factory(count=count))
    await handlers._drain_history_journal(AsyncMock())
    bot = AsyncMock()
    await notification_delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == units
    journal = storage.load_event_journal()
    assert journal["processed_seq"] == count
    assert bool(journal["outbox"].get("plans")) == (count >= 2)
    text = bot.send_message.await_args.kwargs["text"]
    assert ("Что нового у" in text) == (count >= 2)
    current = storage.load_stats_current(strict=True)
    assert current["event_projection"]["applied_seq"] == count
    assert sum(len(period["events"]) for period in current["event_time"]["periods"].values()) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", [False, True])
@pytest.mark.parametrize("error", [ValueError, OSError])
async def test_block_list_failure_only_blocks_preparation_with_recipients(digest_env, journal_factory, monkeypatch, audience, error):
    _install(journal_factory(count=2))
    if not audience:
        storage.save_subscribers({})
    blocked = Mock(side_effect=error("broken block list"))
    monkeypatch.setattr("handlers.load_blocked_users", blocked)
    if audience:
        with pytest.raises(EventJournalStateError, match="^notification_state$"):
            await handlers._drain_history_journal(AsyncMock())
        blocked.assert_called_once()
    else:
        await handlers._drain_history_journal(AsyncMock())
        blocked.assert_not_called()
    journal = storage.load_event_journal()
    expected = 0 if audience else 2
    assert journal["processed_seq"] == expected
    assert storage.load_stats_current(strict=True)["event_projection"]["applied_seq"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("silent", ["ignored", "score_removed", "irrelevant"])
@pytest.mark.parametrize("unknown", [False, True])
async def test_single_notification_among_silent_events_keeps_ordinary_payload(digest_env, journal_factory, monkeypatch, silent, unknown):
    journal = journal_factory(count=2)
    if unknown:
        journal["events"][0]["event_type"] = "unknown"
    if silent == "irrelevant":
        journal["events"][1]["relevant"] = False
    else:
        journal["events"][1]["event_type"] = silent
    _install(journal)
    monkeypatch.setattr("messages.random.choice", lambda bank: bank[0])
    expected = handlers.build_message(handlers.history_entry_from_event(journal["events"][0]), normalized=journal["events"][0])
    await handlers._drain_history_journal(AsyncMock())
    saved = storage.load_event_journal()
    assert not saved["outbox"].get("plans")
    bot = AsyncMock()
    await notification_delivery.dispatch_notifications(bot)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("silent", ["ignored", "score_removed", "irrelevant", "unknown"])
async def test_silent_events_are_excluded_and_unknown_joins_summary(digest_env, journal_factory, silent):
    journal = journal_factory(count=10)
    if silent == "irrelevant":
        journal["events"][4]["relevant"] = False
    else:
        journal["events"][4]["event_type"] = silent
    _install(journal)
    await handlers._drain_history_journal(AsyncMock())
    saved = storage.load_event_journal()
    plan = saved["outbox"]["plans"][0]
    assert plan["version"] == 2
    assert len(plan["units"][0]["events"]) == (10 if silent == "unknown" else 9)
    bot = AsyncMock()
    await notification_delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["plan", "projection", "enqueue"])
@pytest.mark.parametrize("after", [False, True])
async def test_failed_publication_and_cold_resume_freeze_plan(digest_env, notification_batch_factory, monkeypatch, boundary, after):
    _install(notification_batch_factory(count=10))
    write = storage._atomic_write
    frozen = []
    calls = []

    def publish(path, payload):
        value = json.loads(payload)
        is_plan = path == storage.notification_progress_file() and value.get("outbox", {}).get("plans") and value["processed_seq"] == 0
        if is_plan:
            frozen.append(deepcopy(value["outbox"]["plans"]))
        target = is_plan if boundary == "plan" else path == storage.STATS_CURRENT_FILE and value.get("event_projection", {}).get("applied_seq") == 1 if boundary == "projection" else path == storage.notification_progress_file() and value.get("processed_seq") == 1
        if target and not calls:
            calls.append(True)
            if after:
                write(path, payload)
            raise asyncio.CancelledError
        return write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", publish)
        with pytest.raises(asyncio.CancelledError):
            await handlers._drain_history_journal(AsyncMock())
    saved = storage.load_event_journal()
    bot = AsyncMock()
    await notification_delivery.dispatch_notifications(bot)
    bot.send_message.assert_not_awaited()
    if saved["outbox"].get("plans"):
        monkeypatch.setattr("handlers.render_digest", lambda *a, **k: pytest.fail("rerender after restart"))
    storage._journal_history_cache = None
    storage._journal_progress_cache = None
    await handlers._drain_history_journal(AsyncMock())
    ready = storage.load_event_journal()
    if boundary != "plan" or after:
        assert ready["outbox"]["plans"] == frozen[0]
    await notification_delivery.dispatch_notifications(bot)
    bot.send_message.assert_awaited_once()
    assert ready["processed_seq"] == 10


@pytest.mark.asyncio
async def test_local_render_failure_falls_back_before_publication(digest_env, journal_factory, monkeypatch):
    _install(journal_factory(count=10))
    monkeypatch.setattr("handlers.render_digest", lambda *a, **k: (_ for _ in ()).throw(ValueError("local")))
    await handlers._drain_history_journal(AsyncMock())
    assert not storage.load_event_journal()["outbox"].get("plans")
    bot = AsyncMock()
    await notification_delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == 10


@pytest.mark.asyncio
async def test_plan_write_failure_does_not_fallback_or_advance_projection(digest_env, journal_factory, monkeypatch):
    _install(journal_factory(count=10))
    write = storage._atomic_write

    def publish(path, payload):
        if path == storage.notification_progress_file() and json.loads(payload)["outbox"].get("plans"):
            raise OSError("plan failure")
        return write(path, payload)

    monkeypatch.setattr("storage._atomic_write", publish)
    with pytest.raises(EventJournalStateError, match="journal_write"):
        await handlers._drain_history_journal(AsyncMock())
    assert storage.load_event_journal()["processed_seq"] == 0
    assert storage.load_stats_current(strict=True)["event_projection"]["applied_seq"] == 0


@pytest.mark.asyncio
async def test_new_digest_preserves_old_pending_payload_audience_and_clocks(digest_env, journal_factory, monkeypatch):
    _install(journal_factory(count=1))
    with monkeypatch.context() as patch:
        patch.setattr("handlers.render_digest", lambda *a, **k: (_ for _ in ()).throw(ValueError("local")))
        await handlers._drain_history_journal(AsyncMock())
    journal = storage.load_event_journal()
    previous = deepcopy(journal["outbox"]["records"][0])
    new_events = journal_factory(count=11)["events"][1:]
    journal["events"].extend(new_events)
    storage.save_event_journal(journal, admitting=True)
    await handlers._drain_history_journal(AsyncMock())
    ready = storage.load_event_journal()
    assert ready["outbox"]["records"][0] == previous
    assert ready["outbox"]["plans"][0]["start_seq"] == 2
    assert ready["outbox"]["plans"][0]["end_seq"] == 11
    assert len(ready["outbox"]["plans"][0]["units"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["empty", "silent", "unknown"])
async def test_empty_silent_and_unknown_only_batch(digest_env, journal_factory, kind):
    journal = journal_factory(count=0 if kind == "empty" else 2)
    for event in journal["events"]:
        event["event_type"] = "unknown" if kind == "unknown" else "ignored"
    _install(journal)
    await handlers._drain_history_journal(AsyncMock())
    bot = AsyncMock()
    await notification_delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == (1 if kind == "unknown" else 0)
    saved = storage.load_event_journal()
    assert saved["processed_seq"] == len(journal["events"])
    assert bool(saved["outbox"].get("plans")) == (kind == "unknown")


@pytest.mark.asyncio
async def test_consecutive_admitted_batches_keep_separate_frozen_summaries(digest_env, journal_factory):
    _install(journal_factory(count=2))
    await handlers._drain_history_journal(AsyncMock())
    journal = storage.load_event_journal()
    previous = deepcopy(journal["outbox"]["plans"][0])
    journal["events"].extend(journal_factory(count=5)["events"][2:])
    storage.save_event_journal(journal, admitting=True)
    await handlers._drain_history_journal(AsyncMock())
    ready = storage.load_event_journal()
    assert ready["outbox"]["plans"][0] == previous
    assert [plan["start_seq"] for plan in ready["outbox"]["plans"]] == [1, 3]
    bot = AsyncMock()
    await notification_delivery.dispatch_notifications(bot)
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_restart_resumes_published_plan_without_reformatting(digest_env, legacy_digest_factory, digest_factory, monkeypatch, legacy):
    journal = legacy_digest_factory(ready=False) if legacy else digest_factory(ready=False, count=1)
    _install(journal)
    state = storage.load_subscriber_state(strict_subscribers=True)
    state.notification_memberships = {10: "b" * 32}
    storage.save_subscriber_state(state)
    previous = deepcopy(journal["outbox"]["plans"])
    monkeypatch.setattr("handlers.render_digest", lambda *a, **k: pytest.fail("published plan rerender"))
    monkeypatch.setattr("notification_delivery.time.time", lambda: 1800000000.0)
    storage._journal_history_cache = None
    storage._journal_progress_cache = None
    await handlers._drain_history_journal(AsyncMock())
    ready = storage.load_event_journal()
    assert ready["outbox"]["plans"] == previous
    bot = AsyncMock()
    await notification_delivery.dispatch_notifications(bot)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == [
        unit["payload"]["text"] for unit in previous[0]["units"]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [0, -1])
@pytest.mark.parametrize("coalesced", [False, True])
async def test_prepared_reserve_admits_every_future_event_link_at_inclusive_limit(digest_env, digest_factory, journal_factory, coalescing_factory, coalescing_journal_factory, monkeypatch, offset, coalesced):
    from types import SimpleNamespace

    from event_journal_schema import journal_json
    from notification_outbox import progress_reserve

    monkeypatch.setattr("messages.random.choice", lambda bank: bank[0])
    prepared = (coalescing_factory if coalesced else digest_factory)(ready=False)
    identity = prepared["outbox"]["plans"][0]["plan_id"]
    monkeypatch.setattr("notification_outbox.uuid4", lambda: SimpleNamespace(hex=identity))
    monkeypatch.setattr("handlers.time.time", lambda: 1800000000.0)
    _install((coalescing_journal_factory if coalesced else journal_factory)(count=10))
    limit = len(journal_json(prepared).encode()) + progress_reserve(prepared) + 4096
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", limit + offset)
    if offset:
        with pytest.raises(EventJournalStateError, match="journal_capacity"):
            await handlers._drain_history_journal(AsyncMock())
        assert storage.load_event_journal()["processed_seq"] == 0
        assert storage.load_stats_current(strict=True)["event_projection"]["applied_seq"] == 0
    else:
        await handlers._drain_history_journal(AsyncMock())
        assert storage.load_event_journal()["processed_seq"] == 10


@pytest.mark.asyncio
async def test_plan_capacity_retry_merges_new_plan_after_concurrent_terminal_retention(digest_env, journal_factory, monkeypatch):
    from event_journal_schema import journal_json
    from notification_outbox import (
        finish,
        progress_reserve,
    )

    _install(journal_factory(count=1))
    with monkeypatch.context() as patch:
        patch.setattr("handlers.render_digest", lambda *a, **k: (_ for _ in ()).throw(ValueError("local")))
        await handlers._drain_history_journal(AsyncMock())
    journal = storage.load_event_journal()
    journal["outbox"]["records"][0]["payload"]["text"] = "o" * 3500
    storage.save_event_journal(journal)
    journal["events"].extend(journal_factory(count=11)["events"][1:])
    storage.save_event_journal(journal, admitting=True)
    initial_budget = len(journal_json(journal).encode()) + progress_reserve(journal)
    render = handlers.render_digest
    rendered = []
    maintenance = handlers.compact_event_journal
    retries = []

    def freeze(*args, **kwargs):
        current = storage.load_event_journal()
        record = current["outbox"]["records"][0]
        finish(record["recipients"]["10"], "cancelled", "ineligible", record["created_at"])
        storage.save_event_journal(current)
        monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", initial_budget + 4096)
        parts = render(*args, **kwargs)
        rendered.append(parts)
        return parts

    def compact(*args, **kwargs):
        result = maintenance(*args, **kwargs)
        if kwargs.get("force_history"):
            retries.append(result)
        return result

    monkeypatch.setattr("handlers.render_digest", freeze)
    monkeypatch.setattr("handlers.compact_event_journal", compact)
    await handlers._drain_history_journal(AsyncMock())
    ready = storage.load_event_journal()
    assert len(rendered) == 1 and len(retries) == 1
    assert ready["processed_seq"] == 11 and ready["outbox"]["completed_seq"] == 1
    assert ready["outbox"]["plans"][0]["start_seq"] == 2
    assert [unit["payload"]["text"] for unit in ready["outbox"]["plans"][0]["units"]] == [part["text"] for part in rendered[0]]
    assert {record["plan_id"] for record in ready["outbox"]["records"]} == {ready["outbox"]["plans"][0]["plan_id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_already_projected_event_is_not_reapplied_or_represented_after_possible_send(digest_env, journal_factory, monkeypatch, legacy):
    from event_time_stats import (
        ensure_event_time,
        project_event,
    )
    from notification_outbox import migrate_outbox

    _install(journal_factory(count=11))
    journal = storage.load_event_journal()
    if not legacy:
        journal = migrate_outbox(journal, 0)
        storage.save_event_journal(journal)
    cur = storage.load_stats_current(strict=True)
    ensure_event_time(cur)
    project_event(cur, journal, 1)
    cur["event_projection"]["applied_seq"] = 1
    storage.save_stats_current(cur, strict=True)
    projected = []

    def project(*args, **kwargs):
        projected.append(args[2])
        return project_event(*args, **kwargs)

    monkeypatch.setattr("handlers.project_event", project)
    await handlers._drain_history_journal(AsyncMock())
    assert projected == list(range(2, 12))
    ready = storage.load_event_journal()
    assert ready["outbox"]["plans"][0]["start_seq"] == (2 if legacy else 1)
    if legacy:
        assert ready["outbox"]["records"][0]["recipients"]["10"]["prior_possible"]
        assert "plan_id" not in ready["outbox"]["records"][0]
