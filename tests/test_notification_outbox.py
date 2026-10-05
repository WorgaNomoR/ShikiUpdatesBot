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
    complete_attempt,
    enqueue,
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
        box["version"] = 2
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
