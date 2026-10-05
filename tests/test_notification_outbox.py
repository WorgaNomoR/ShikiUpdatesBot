# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Формат обязательств, честные исходы и строгие устойчивые budgets."""

import json
from copy import deepcopy

import pytest

from event_journal_schema import (
    EventJournalStateError,
    journal_json,
    parse_event_journal,
    validate_event_journal,
)
from notification_outbox import (
    LIFETIME,
    MAX_ATTEMPTS,
    begin_attempt,
    compact_outbox,
    complete_attempt,
    enqueue,
    finish,
    migrate_outbox,
    possible_delivery,
    progress_reserve,
    validate_memberships,
)


def _box(factory):
    journal = migrate_outbox(factory(), 0)
    return enqueue(
        journal, journal["events"][0], "<b>frozen</b>", {10: "b" * 32, -100: "c" * 32}, 1000
    )


def test_enqueued_identity_payload_and_creation_clock(journal_factory):
    journal = _box(journal_factory)
    record = journal["outbox"]["records"][0]
    assert journal["processed_seq"] == journal["outbox"]["enqueued_seq"] == 1
    assert record["expires_at"] == 1000 + LIFETIME
    assert set(record["recipients"]) == {"10", "-100"}
    assert record["payload"]["text"] == "<b>frozen</b>"
    assert parse_event_journal(json.dumps(journal).encode()) == journal


def test_quiet_migration_has_no_invented_delivery_and_retains_pending(journal_factory):
    journal = migrate_outbox(journal_factory(count=2, processed=1), 2)
    assert journal["outbox"]["baseline_seq"] == 1
    assert journal["outbox"]["records"] == []
    journal = enqueue(journal, journal["events"][1], "unfinished", {10: "b" * 32}, 1000)
    recipient = journal["outbox"]["records"][0]["recipients"]["10"]
    assert recipient["prior_possible"]
    assert recipient["status"] == "pending"
    assert possible_delivery(recipient)
    validate_event_journal(journal)


@pytest.mark.parametrize("outcome", ["confirmed_rejection", "confirmed_success"])
def test_late_result_preserves_previous_uncertainty(journal_factory, outcome):
    journal = _box(journal_factory)
    recipient = journal["outbox"]["records"][0]["recipients"]["10"]
    begin_attempt(recipient, 1000)
    complete_attempt(recipient, "uncertain", 1000, retry_delay=0)
    begin_attempt(recipient, 1060)
    complete_attempt(recipient, outcome, 1061)
    assert possible_delivery(recipient)
    assert recipient["status"] == ("delivered" if outcome == "confirmed_success" else "rejected")
    assert recipient["duplicate_possible"] == (outcome == "confirmed_success")
    assert recipient["attempts"][0]["outcome"] == "uncertain"
    validate_event_journal(journal)


def test_remaining_growth_reserve_shrinks_without_resetting_budget(journal_factory):
    journal = _box(journal_factory)
    before = progress_reserve(journal)
    recipient = journal["outbox"]["records"][0]["recipients"]["10"]
    now = 1000
    for _ in range(MAX_ATTEMPTS):
        begin_attempt(recipient, now)
        complete_attempt(recipient, "uncertain", now, retry_delay=0)
        now = recipient["next_attempt_at"]
    assert recipient["status"] == "expired"
    assert recipient["reason"] == "attempt_budget"
    assert progress_reserve(journal) < before
    validate_event_journal(journal)


@pytest.mark.parametrize(
    "damage",
    [
        "version",
        "missing",
        "cursor",
        "bool",
        "ttl",
        "payload",
        "chat",
        "membership",
        "status",
        "attempts",
        "attempt_time",
        "outcome",
        "success",
        "terminal",
        "duplicate",
        "encoding",
    ],
)
def test_shared_parser_rejects_malformed_outbox(journal_factory, damage):
    journal = _box(journal_factory)
    box = journal["outbox"]
    record = box["records"][0]
    recipient = record["recipients"]["10"]
    if damage == "version":
        box["version"] = 3
    elif damage == "missing":
        box["records"] = []
    elif damage == "cursor":
        box["enqueued_seq"] = 0
    elif damage == "bool":
        record["seq"] = True
    elif damage == "ttl":
        record["expires_at"] += 1
    elif damage == "payload":
        record["payload"]["parse_mode"] = "Markdown"
    elif damage == "chat":
        record["recipients"]["010"] = record["recipients"].pop("10")
    elif damage == "membership":
        recipient["membership"] = "bad"
    elif damage == "status":
        recipient["status"] = "lost"
    elif damage == "attempts":
        recipient["attempts"] = [{"at": 1000, "outcome": "uncertain"}] * 7
    elif damage == "attempt_time":
        recipient["attempts"] = [{"at": record["expires_at"], "outcome": "uncertain"}]
    elif damage == "outcome":
        recipient["attempts"] = [{"at": 1000, "outcome": "bad"}]
    elif damage == "success":
        recipient["attempts"] = [{"at": 1000, "outcome": "confirmed_success"}]
    elif damage == "terminal":
        recipient["reason"] = "confirmed_success"
    elif damage == "duplicate":
        recipient["duplicate_possible"] = True
    elif damage == "encoding":
        record["payload"]["text"] = "\ud800"
    with pytest.raises(EventJournalStateError):
        parse_event_journal(json.dumps(journal).encode())


@pytest.mark.parametrize(
    "event_type,relevant", [("ignored", True), ("score_removed", True), ("completed", False)]
)
def test_silent_decisions_are_retained(journal_factory, event_type, relevant):
    journal = migrate_outbox(journal_factory(), 0)
    event = journal["events"][0]
    event.update(event_type=event_type, relevant=relevant)
    journal = enqueue(journal, event, None, {10: "b" * 32}, 1000)
    record = journal["outbox"]["records"][0]
    assert record["payload"] is None and record["recipients"] == {}
    validate_event_journal(journal)


def test_empty_audience_keeps_exact_notification(journal_factory):
    journal = migrate_outbox(journal_factory(), 0)
    journal = enqueue(journal, journal["events"][0], "no audience", {}, 1000)
    assert journal["outbox"]["records"][0]["recipients"] == {}
    assert journal["outbox"]["records"][0]["payload"]["text"] == "no audience"
    validate_event_journal(journal)


def test_membership_schema_is_strict_for_positive_and_group_chats():
    metadata = {"version": 1, "tokens": {"10": "b" * 32, "-100": "c" * 32}}
    assert validate_memberships(metadata, {10: "personal", -100: "group"}) == {
        10: "b" * 32,
        -100: "c" * 32,
    }
    for change in [
        {"version": True},
        {"tokens": {"10": "b" * 32}},
        {"tokens": {"10": "bad", "-100": "c" * 32}},
    ]:
        with pytest.raises(ValueError):
            validate_memberships({**deepcopy(metadata), **change}, {10: "personal", -100: "group"})


def test_recovery_capacity_includes_future_progress(journal_factory, monkeypatch):
    journal = _box(journal_factory)
    raw = journal_json(journal).encode()
    monkeypatch.setattr(
        "event_journal_schema.JOURNAL_MAX_BYTES", len(raw) + progress_reserve(journal) - 1
    )
    with pytest.raises(EventJournalStateError):
        parse_event_journal(raw)


def test_huge_time_integer_is_validation_failure(journal_factory):
    journal = _box(journal_factory)
    journal["outbox"]["records"][0]["created_at"] = 10**400
    with pytest.raises(EventJournalStateError):
        parse_event_journal(json.dumps(journal).encode())


@pytest.mark.parametrize("status", ["delivered", "cancelled", "expired", "rejected"])
@pytest.mark.parametrize("possibilities", [0, 1, 2])
def test_compaction_retains_honest_outcome_and_duplicate_counts(journal_factory, status, possibilities):
    journal = _box(journal_factory)
    record = journal["outbox"]["records"][0]
    record["recipients"] = {"10": record["recipients"]["10"]}
    recipient = record["recipients"]["10"]
    for number in range(possibilities):
        begin_attempt(recipient, 1000 + number * 60)
    if status == "delivered":
        begin_attempt(recipient, 1200)
        complete_attempt(recipient, "confirmed_success", 1201)
    elif status == "rejected":
        begin_attempt(recipient, 1200)
        complete_attempt(recipient, "confirmed_rejection", 1201)
    else:
        finish(recipient, status, "ineligible" if status == "cancelled" else "lifetime", 1000 + LIFETIME)
    before = deepcopy(journal)
    compacted = compact_outbox(journal)
    assert journal == before
    expected = {
        "count": 1,
        "possible_delivery": int(status == "delivered" or possibilities > 0),
        "duplicate_possible": int(possibilities + int(status == "delivered") > 1),
    }
    assert compacted["outbox"]["records"] == [{
        "summary_version": 1, "seq": 1, "history_id": 2, "outcomes": {status: expected},
    }]
    assert compacted["events"] == before["events"]
    assert compacted["baseline_ids"] == before["baseline_ids"]
    assert len(journal_json(compacted).encode()) + progress_reserve(compacted) <= len(journal_json(before).encode()) + progress_reserve(before)
    assert compact_outbox(compacted) is compacted
    assert parse_event_journal(journal_json(compacted).encode()) == compacted


def test_compaction_preserves_mixed_record_and_all_pending_budgets(journal_factory):
    journal = _box(journal_factory)
    record = journal["outbox"]["records"][0]
    complete = record["recipients"]["10"]
    begin_attempt(complete, 1000)
    complete_attempt(complete, "confirmed_success", 1001)
    pending = record["recipients"]["-100"]
    begin_attempt(pending, 1000)
    complete_attempt(pending, "uncertain", 1001, retry_delay=300)
    before = journal_json(journal)
    reserve = progress_reserve(journal)
    assert compact_outbox(journal) is journal
    assert journal_json(journal) == before
    assert progress_reserve(journal) == reserve


@pytest.mark.parametrize("silent", [True, False])
def test_compaction_silent_and_empty_audiences_remain_completed(journal_factory, silent):
    journal = migrate_outbox(journal_factory(), 0)
    if silent:
        journal["events"][0]["event_type"] = "score_removed"
    journal = enqueue(journal, journal["events"][0], None if silent else "empty audience", {}, 1000)
    result = compact_outbox(journal)
    assert result["processed_seq"] == result["outbox"]["enqueued_seq"] == 1
    assert result["outbox"]["records"][0]["outcomes"] == {}
    validate_event_journal(result)


def test_compaction_inherited_possible_broadcast_survives_terminal_summary(journal_factory):
    journal = migrate_outbox(journal_factory(), 1)
    journal = enqueue(journal, journal["events"][0], "legacy uncertainty", {10: "b" * 32}, 1000)
    recipient = journal["outbox"]["records"][0]["recipients"]["10"]
    begin_attempt(recipient, 1000)
    complete_attempt(recipient, "confirmed_success", 1001)
    counts = compact_outbox(journal)["outbox"]["records"][0]["outcomes"]["delivered"]
    assert counts == {"count": 1, "possible_delivery": 1, "duplicate_possible": 1}


def test_compaction_is_bounded_and_keeps_record_order(journal_factory):
    journal = migrate_outbox(journal_factory(count=130), 0)
    for event in journal["events"]:
        journal = enqueue(journal, event, "empty audience", {}, 1000)
    once = compact_outbox(journal)
    assert sum("summary_version" in r for r in once["outbox"]["records"]) == 128
    twice = compact_outbox(once)
    assert all("summary_version" in r for r in twice["outbox"]["records"])
    assert [r["seq"] for r in twice["outbox"]["records"]] == list(range(1, 131))
    assert compact_outbox(twice) is twice


@pytest.mark.parametrize("damage", [
    "box_version", "summary_version", "bool_version", "identity", "order", "extra_payload",
    "pending", "count_bool", "zero", "negative", "possible_overflow", "duplicate_overflow",
    "delivered_without_acceptance", "silent_with_outcomes", "unknown_fields", "missing_fields",
])
def test_compacted_runtime_import_schema_rejects_inconsistent_summaries(journal_factory, damage):
    journal = _box(journal_factory)
    for recipient in journal["outbox"]["records"][0]["recipients"].values():
        begin_attempt(recipient, 1000)
        complete_attempt(recipient, "confirmed_success", 1001)
    journal = compact_outbox(journal)
    box = journal["outbox"]
    record = box["records"][0]
    counts = record["outcomes"]["delivered"]
    if damage == "box_version":
        box["version"] = 1
    elif damage == "summary_version":
        record["summary_version"] = 2
    elif damage == "bool_version":
        record["summary_version"] = True
    elif damage == "identity":
        record["history_id"] += 1
    elif damage == "order":
        record["seq"] = 2
    elif damage == "extra_payload":
        record["payload"] = {"text": "must never dispatch"}
    elif damage == "pending":
        record["outcomes"]["pending"] = record["outcomes"].pop("delivered")
    elif damage == "count_bool":
        counts["count"] = True
    elif damage == "zero":
        counts.update(count=0, possible_delivery=0)
    elif damage == "negative":
        counts["duplicate_possible"] = -1
    elif damage == "possible_overflow":
        counts["possible_delivery"] = 3
    elif damage == "duplicate_overflow":
        counts["duplicate_possible"] = 3
    elif damage == "delivered_without_acceptance":
        counts["possible_delivery"] = 0
    elif damage == "silent_with_outcomes":
        journal["events"][0]["event_type"] = "ignored"
    elif damage == "unknown_fields":
        counts["attempts"] = 1
    else:
        del counts["possible_delivery"]
    with pytest.raises(EventJournalStateError):
        parse_event_journal(json.dumps(journal).encode())


def test_compaction_keeps_silent_legacy_baselines_and_unfinished_acquisition(acquisition_factory):
    journal = migrate_outbox(acquisition_factory(), 0)
    before = deepcopy(journal)
    assert compact_outbox(journal) == before
