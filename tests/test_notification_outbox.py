# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Формат обязательств, честные исходы и строгие устойчивые budgets."""

import json
import math
import sys
from copy import deepcopy

import pytest

from event_journal_schema import (
    EventJournalStateError,
    journal_json,
    parse_event_journal,
    validate_event_journal,
)
from notification_outbox import (
    BACKOFF,
    LIFETIME,
    MAX_ATTEMPTS,
    OutboxStateError,
    begin_attempt,
    compact_outbox,
    complete_attempt,
    completed_seq,
    enqueue,
    finish,
    migrate_outbox,
    possible_delivery,
    progress_reserve,
    replace_recipients,
    retain_outbox,
    validate_memberships,
    validate_outbox,
)


def _box(factory):
    journal = migrate_outbox(factory(), 0)
    return enqueue(
        journal, journal["events"][0], "<b>frozen</b>", {10: "b" * 32, -100: "c" * 32}, 1000
    )


def test_recipient_replacement_owns_deltas_and_copies_only_affected_branches(journal_factory):
    journal = migrate_outbox(journal_factory(count=2), 0)
    for event in journal["events"]:
        journal = enqueue(journal, event, "frozen", {10: "b" * 32, 20: "c" * 32}, 1000)
    before = deepcopy(journal)
    assert replace_recipients(journal, {}) is journal
    delta = deepcopy(journal["outbox"]["records"][0]["recipients"]["10"])
    begin_attempt(delta, 1000)
    updated = replace_recipients(journal, {(1, "10"): delta})
    assert journal == before
    assert updated["events"] is journal["events"]
    assert updated["outbox"]["records"][1] is journal["outbox"]["records"][1]
    assert updated["outbox"]["records"][0]["payload"] is journal["outbox"]["records"][0]["payload"]
    assert updated["outbox"]["records"][0]["recipients"]["20"] is journal["outbox"]["records"][0]["recipients"]["20"]
    delta["attempts"][0]["outcome"] = "not_dispatched"
    assert updated["outbox"]["records"][0]["recipients"]["10"]["attempts"] == [{"at": 1000, "outcome": "uncertain"}]
    validate_event_journal(updated)


@pytest.mark.parametrize("seq,cid", [(0, "10"), (2, "10"), (1, "missing")])
def test_recipient_replacement_rejects_absent_obligation(journal_factory, seq, cid):
    journal = _box(journal_factory)
    before = deepcopy(journal)
    with pytest.raises(OutboxStateError, match="recipient_changed"):
        replace_recipients(journal, {(seq, cid): {}})
    assert journal == before


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


def test_capacity_control_fits_with_bounded_reserve(outbox_capacity_factory):
    journal = outbox_capacity_factory()
    actual = len(journal_json(journal).encode("utf-8"))
    assert actual == 1_408_450
    assert progress_reserve(journal) == 441 * 7000
    assert actual + progress_reserve(journal) <= 8 * 1024 * 1024
    assert parse_event_journal(journal_json(journal).encode("utf-8")) == journal


@pytest.mark.parametrize("count", range(MAX_ATTEMPTS + 1))
@pytest.mark.parametrize("inherited", [False, True])
@pytest.mark.parametrize("outcome", ["uncertain", "not_dispatched", "confirmed_rejection"])
@pytest.mark.parametrize("created", [0, 1000, 10**12 - LIFETIME])
def test_future_recipient_transitions_never_increase_reserved_budget(
    journal_factory, count, inherited, outcome, created,
):
    journal = migrate_outbox(journal_factory(), int(inherited))
    journal = enqueue(journal, journal["events"][0], "frozen", {10: "b" * 32}, created)
    recipient = journal["outbox"]["records"][0]["recipients"]["10"]
    for _ in range(count):
        begin_attempt(recipient, created)
        recipient["attempts"][-1]["outcome"] = outcome

    def budget(state):
        return len(journal_json(state).encode("utf-8")) + progress_reserve(state)

    ceiling = budget(journal)

    def check(change):
        candidate = deepcopy(journal)
        change(candidate["outbox"]["records"][0]["recipients"]["10"])
        assert budget(candidate) <= ceiling
        # Реальный parser обязан принять transition, не только size helper.
        assert parse_event_journal(journal_json(candidate).encode()) == candidate

    for now in [created, float(created), 10**12 - 0.0001, 10**12]:
        check(lambda r: finish(r, "cancelled", "ineligible", now))
        check(lambda r: finish(r, "expired", "lifetime", max(now, created + LIFETIME)))
        if count == MAX_ATTEMPTS:
            check(lambda r: finish(r, "expired", "attempt_budget", now))
        if count:
            for result in ["confirmed_success", "confirmed_rejection", "not_dispatched", "uncertain"]:
                delay = None if result in {"confirmed_success", "confirmed_rejection"} else 0
                retry_now = now if delay is None else min(now, 10**12 - BACKOFF[-1])
                check(lambda r: complete_attempt(r, result, retry_now, retry_delay=delay))
            check(lambda r: (
                complete_attempt(r, "confirmed_rejection", now),
                finish(r, "rejected", "forbidden", now),
            ))
    # Marker и ack повторяются до исходного budget, включая шестой marker.
    for _ in range(count, MAX_ATTEMPTS):
        previous = budget(journal)
        begin_attempt(recipient, created)
        assert budget(journal) <= previous <= ceiling
        complete_attempt(recipient, "uncertain", created, retry_delay=0)
        assert budget(journal) <= previous
    assert recipient["status"] == ("pending" if count == MAX_ATTEMPTS else "expired")


@pytest.mark.parametrize("due", [
    0, -0.0, 5e-324, 1.2345678901234568e-300, 0.00012345678901234567,
    0.0001, 0.00001, 1000, 1000.0, 10**12 - 0.0001, 10**12, float(10**12),
])
def test_reserve_covers_numeric_json_boundaries_and_latest_marker(journal_factory, due):
    journal = migrate_outbox(journal_factory(), 1)
    journal = enqueue(journal, journal["events"][0], "frozen", {10: "b" * 32}, 0)
    recipient = journal["outbox"]["records"][0]["recipients"]["10"]
    for _ in range(MAX_ATTEMPTS):
        begin_attempt(recipient, 0)
    recipient["next_attempt_at"] = due
    before = len(journal_json(journal).encode()) + progress_reserve(journal)
    # Последняя публикация ещё uncertain: attempts больше не добавятся, ack растёт.
    complete_attempt(recipient, "confirmed_rejection", float(10**12))
    assert len(journal_json(journal).encode()) <= before
    assert progress_reserve(journal) == 0


@pytest.mark.parametrize("created,attempt_at", [
    (0, -0.0), (0, 5e-324), (0, 1.2345678901234568e-300),
    (0, 0.00012345678901234567), (0, 123456.78901234567),
    (10**12 - LIFETIME, math.nextafter(float(10**12), 0)),
])
def test_reserved_slots_cover_float_attempt_times(journal_factory, created, attempt_at):
    journal = migrate_outbox(journal_factory(), 0)
    journal = enqueue(journal, journal["events"][0], "frozen", {10: "b" * 32}, created)
    recipient = journal["outbox"]["records"][0]["recipients"]["10"]
    ceiling = len(journal_json(journal).encode()) + progress_reserve(journal)
    for _ in range(MAX_ATTEMPTS):
        begin_attempt(recipient, attempt_at)
        # Проверяем длинный attempt.at вместе с предельным допустимым due.
        recipient["next_attempt_at"] = float(10**12)
        current = len(journal_json(journal).encode()) + progress_reserve(journal)
        assert current <= ceiling
        ceiling = current
    complete_attempt(recipient, "confirmed_success", float(10**12))
    assert len(journal_json(journal).encode()) <= ceiling
    assert parse_event_journal(journal_json(journal).encode()) == journal


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
    retired = retain_outbox(journal)
    assert retain_outbox(compacted) == retired
    assert retired["outbox"]["completed_seq"] == 1
    assert retired["outbox"]["records"] == []
    assert "outcomes" not in retired["outbox"]
    assert retired["events"] == before["events"]
    assert retired["baseline_ids"] == before["baseline_ids"]
    assert parse_event_journal(journal_json(retired).encode()) == retired


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
    assert retain_outbox(journal) is journal


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
    retired = retain_outbox(result)
    assert retired["outbox"]["records"] == []
    assert retired["outbox"]["completed_seq"] == 1
    assert retired["events"] == journal["events"]


def test_compaction_inherited_possible_broadcast_survives_terminal_summary(journal_factory):
    journal = migrate_outbox(journal_factory(), 1)
    journal = enqueue(journal, journal["events"][0], "legacy uncertainty", {10: "b" * 32}, 1000)
    recipient = journal["outbox"]["records"][0]["recipients"]["10"]
    begin_attempt(recipient, 1000)
    complete_attempt(recipient, "confirmed_success", 1001)
    counts = compact_outbox(journal)["outbox"]["records"][0]["outcomes"]["delivered"]
    assert counts == {"count": 1, "possible_delivery": 1, "duplicate_possible": 1}
    result = retain_outbox(journal)
    assert result["outbox"]["legacy_uncertain_seq"] == 1
    assert result["outbox"]["records"] == []
    validate_event_journal(result)


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
    assert retain_outbox(journal) is journal


@pytest.mark.parametrize("version", [1, 2])
def test_retention_quiet_baseline_and_absolute_enqueue(journal_factory, version):
    journal = migrate_outbox(journal_factory(count=4, processed=2), 3)
    journal["outbox"]["version"] = version
    journal = enqueue(journal, journal["events"][2], "legacy uncertainty", {}, 1000)
    result = retain_outbox(journal)
    assert result["outbox"]["baseline_seq"] == 2
    assert result["outbox"]["completed_seq"] == 3
    assert result["outbox"]["legacy_uncertain_seq"] == 3
    result = enqueue(result, result["events"][3], "next", {10: "b" * 32}, 1100)
    assert result["outbox"]["records"][0]["seq"] == 4
    assert not result["outbox"]["records"][0]["recipients"]["10"]["prior_possible"]
    assert result["processed_seq"] == 4
    assert parse_event_journal(journal_json(result).encode()) == result


@pytest.mark.parametrize("summarized", [False, True])
def test_retention_is_bounded_idempotent_and_non_increasing(journal_factory, summarized):
    journal = migrate_outbox(journal_factory(count=130), 0)
    for event in journal["events"]:
        journal = enqueue(journal, event, "empty audience", {}, 1000)
    if summarized:
        journal = compact_outbox(compact_outbox(journal))
    before = deepcopy(journal)
    once = retain_outbox(journal)
    assert journal == before
    assert once["outbox"]["completed_seq"] == 128
    assert [r["seq"] for r in once["outbox"]["records"]] == [129, 130]
    assert len(journal_json(once).encode()) + progress_reserve(once) <= len(journal_json(before).encode()) + progress_reserve(before)
    twice = retain_outbox(once)
    assert twice["outbox"]["records"] == []
    assert twice["outbox"]["completed_seq"] == twice["processed_seq"] == 130
    assert retain_outbox(twice) is twice


def _retention_suffix(factory):
    journal = migrate_outbox(factory(count=4), 0)
    for event in journal["events"]:
        memberships = {10: "b" * 32, 20: "c" * 32} if event["seq"] == 2 else {}
        journal = enqueue(journal, event, "frozen", memberships, 1000)
    mixed = journal["outbox"]["records"][1]["recipients"]
    finish(mixed["20"], "cancelled", "ineligible", 1001)
    begin_attempt(mixed["10"], 1000)
    complete_attempt(mixed["10"], "uncertain", 1001, retry_delay=300)
    return journal


def test_pending_barrier_preserves_full_record_and_shared_maintenance_budget(journal_factory):
    journal = _retention_suffix(journal_factory)
    exact = json.dumps(journal["outbox"]["records"][1], sort_keys=True)
    result = retain_outbox(journal, limit=2)
    assert completed_seq(result["outbox"]) == 1
    assert json.dumps(result["outbox"]["records"][0], sort_keys=True) == exact
    assert result["outbox"]["records"][1]["summary_version"] == 1
    assert "summary_version" not in result["outbox"]["records"][2]
    assert progress_reserve(result) == progress_reserve(journal)
    twice = retain_outbox(result)
    assert twice["outbox"]["version"] == 3
    assert completed_seq(twice["outbox"]) == 1
    assert [r["seq"] for r in twice["outbox"]["records"]] == [2, 3, 4]
    assert retain_outbox(twice) is twice


@pytest.mark.parametrize("damage", [
    "missing_checkpoint", "bool_checkpoint", "negative", "before_baseline", "after_enqueue",
    "unsupported", "legacy_extra_checkpoint", "retained_prefix", "missing_suffix", "seq",
    "history_id", "missing_summary", "pending_summary", "extra_outcomes",
])
def test_retained_runtime_import_schema_rejects_inconsistent_checkpoint(journal_factory, damage):
    journal = retain_outbox(_retention_suffix(journal_factory))
    box = journal["outbox"]
    if damage == "missing_checkpoint":
        del box["completed_seq"]
    elif damage == "bool_checkpoint":
        box["completed_seq"] = True
    elif damage == "negative":
        box["completed_seq"] = -1
    elif damage == "before_baseline":
        box["baseline_seq"] = 2
    elif damage == "after_enqueue":
        box["completed_seq"] = 5
    elif damage == "unsupported":
        box["version"] = 4
    elif damage == "legacy_extra_checkpoint":
        box["version"] = 2
    elif damage == "retained_prefix":
        box["completed_seq"] = 2
    elif damage == "missing_suffix":
        box["records"].pop()
    elif damage == "seq":
        box["records"][0]["seq"] = 1
    elif damage == "history_id":
        box["records"][0]["history_id"] = 2
    elif damage == "missing_summary":
        del box["records"][1]["summary_version"]
    elif damage == "pending_summary":
        box["records"][1]["outcomes"] = {"pending": {"count": 1}}
    else:
        box["outcomes"] = {"delivered": 1}
    with pytest.raises(EventJournalStateError):
        parse_event_journal(json.dumps(journal).encode())


def test_digest_preparation_and_enqueue_reserve_is_nonincreasing(digest_factory):
    from event_journal_schema import journal_json
    from notification_outbox import enqueue

    journal = digest_factory(ready=False, audience=20, long_title=True)
    budget = len(journal_json(journal).encode()) + progress_reserve(journal)
    frozen = deepcopy(journal["outbox"]["plans"])
    for event in journal["events"]:
        journal = enqueue(journal, event, "ignored rerender", {}, 0)
        actual = len(journal_json(journal).encode()) + progress_reserve(journal)
        assert actual <= budget
        budget = actual
    assert journal["outbox"]["plans"] == frozen


@pytest.mark.parametrize("outcome", ["confirmed_success", "confirmed_rejection", "not_dispatched", "uncertain"])
@pytest.mark.parametrize("attempts", range(1, 7))
def test_digest_part_reserve_covers_all_recipient_transitions(digest_factory, outcome, attempts):
    from event_journal_schema import journal_json
    from notification_outbox import (
        delivery_key,
        delivery_records,
        replace_recipients,
    )

    journal = digest_factory()
    record = next(delivery_records(journal))
    budget = len(journal_json(journal).encode()) + progress_reserve(journal)
    recipient = deepcopy(record["recipients"]["10"])
    for index in range(attempts):
        now = record["created_at"] + index * 22000
        begin_attempt(recipient, now)
        journal = replace_recipients(journal, {(delivery_key(record), "10"): recipient})
        actual = len(journal_json(journal).encode()) + progress_reserve(journal)
        assert actual <= budget
        budget = actual
        evidence = outcome if index == attempts - 1 else "uncertain"
        complete_attempt(recipient, evidence, now, retry_delay=None if evidence in {"confirmed_success", "confirmed_rejection"} else 0)
        journal = replace_recipients(journal, {(delivery_key(record), "10"): recipient})
        actual = len(journal_json(journal).encode()) + progress_reserve(journal)
        assert actual <= budget
        budget = actual


@pytest.mark.parametrize("damage,reason", [
    ("version", "outbox_plan"),
    ("coverage", "outbox_plan_coverage"),
    ("gap", "outbox_plan_events"),
    ("link", "outbox_plan_link"),
    ("audience", "outbox_plan_audience"),
    ("payload", "outbox_plan_payload"),
    ("attempt_before_ready", "outbox_plan_not_ready"),
    ("silent", "outbox_plan_coverage"),
    ("ordinary_v2", "outbox_plan_unit"),
])
def test_digest_shared_validator_rejects_inconsistent_plan(digest_factory, damage, reason):
    journal = digest_factory(ready=damage != "attempt_before_ready")
    plan = journal["outbox"]["plans"][0]
    unit = plan["units"][0]
    if damage == "version":
        plan["version"] = 3
    elif damage == "coverage":
        unit["events"].pop()
    elif damage == "gap":
        plan["events"].pop()
    elif damage == "link":
        journal["outbox"]["records"][0]["plan_id"] = "c" * 32
    elif damage == "audience":
        plan["units"].append(deepcopy(unit))
        plan["units"][-1].update(unit_id=plan["plan_id"] + ":1", events=[unit["events"][-1]], seq=10)
        plan["units"][-1]["recipients"] = {}
    elif damage == "payload":
        unit["payload"]["text"] = "😀" * 2049
    elif damage == "attempt_before_ready":
        begin_attempt(unit["recipients"]["10"], unit["created_at"])
    elif damage == "silent":
        journal["events"][0]["event_type"] = "score_removed"
    else:
        unit["kind"] = "ordinary"
    with pytest.raises(OutboxStateError, match=f"^{reason}$"):
        validate_outbox(journal)


def test_digest_recipient_batch_copies_each_changed_container_once(digest_factory, monkeypatch):
    journal = digest_factory(audience=20, long_title=True)
    units = journal["outbox"]["plans"][0]["units"]
    assert len(units) >= 3
    before = deepcopy(journal)
    updates = {}
    observed_lists, observed_maps = [], {unit["unit_id"]: [] for unit in units[:2]}
    for cid in units[0]["recipients"]:
        for unit in units[:2]:
            recipient = deepcopy(unit["recipients"][cid])
            finish(recipient, "expired", "lifetime", unit["expires_at"])
            updates[unit["unit_id"], cid] = recipient
    source_units = {id(value): key[0] for key, value in updates.items()}

    def observe(value):
        # Структурный бюджет копий: наблюдаем private-ветви перед каждой дельтой.
        # Сохраняем ссылки, чтобы повторное использование id не скрывало копии.
        state = sys._getframe(1).f_locals
        current = state["plans"][0]["units"]
        observed_lists.append(current)
        key = source_units[id(value)]
        unit = next(unit for unit in current if unit["unit_id"] == key)
        observed_maps[key].append(unit["recipients"])
        return deepcopy(value)

    monkeypatch.setattr("notification_outbox.deepcopy", observe)
    result = replace_recipients(journal, updates)
    assert len({id(value) for value in observed_lists}) == 1
    assert all(len({id(value) for value in values}) == 1 for values in observed_maps.values())
    assert journal == before
    changed = result["outbox"]["plans"][0]["units"]
    assert changed[2] is units[2]
    assert changed[0]["payload"] is units[0]["payload"]
    assert all(recipient["status"] == "expired" for unit in changed[:2] for recipient in unit["recipients"].values())
    updates[units[0]["unit_id"], "10"]["attempts"].append({"at": 0, "outcome": "uncertain"})
    assert changed[0]["recipients"]["10"]["attempts"] == []
    validate_outbox(result)


@pytest.mark.parametrize("missing", ["unit", "recipient"])
def test_digest_recipient_batch_rejects_missing_lease_without_mutating_source(digest_factory, missing):
    journal = digest_factory()
    before = deepcopy(journal)
    unit = journal["outbox"]["plans"][0]["units"][0]
    recipient = deepcopy(unit["recipients"]["10"])
    begin_attempt(recipient, unit["created_at"])
    bad_key = (unit["unit_id"] + "missing", "10") if missing == "unit" else (unit["unit_id"], "missing")
    with pytest.raises(OutboxStateError, match="^recipient_changed$"):
        replace_recipients(journal, {(unit["unit_id"], "10"): recipient, bad_key: recipient})
    assert journal == before


def test_digest_retention_keeps_whole_plan_until_every_part_terminal(digest_factory):
    from notification_outbox import (
        delivery_records,
        retain_outbox,
    )

    journal = digest_factory(long_title=True)
    before = deepcopy(journal)
    units = list(delivery_records(journal))
    for unit in units[:-1]:
        begin_attempt(unit["recipients"]["10"], unit["created_at"])
        complete_attempt(unit["recipients"]["10"], "confirmed_success", unit["created_at"])
    assert retain_outbox(journal) is journal
    assert journal["events"] == before["events"]
    begin_attempt(units[-1]["recipients"]["10"], units[-1]["created_at"])
    complete_attempt(units[-1]["recipients"]["10"], "confirmed_rejection", units[-1]["created_at"])
    partial = retain_outbox(journal, limit=3)
    assert partial["outbox"]["completed_seq"] == 3
    assert partial["outbox"]["plans"] == journal["outbox"]["plans"]
    validate_outbox(partial)
    complete = retain_outbox(partial)
    assert complete["outbox"]["completed_seq"] == 10
    assert complete["outbox"]["plans"] == []
    validate_outbox(complete)


def test_legacy_plan_keeps_original_threshold_and_unknown_parts(legacy_digest_factory):
    journal = legacy_digest_factory()
    validate_outbox(journal)
    changed = deepcopy(journal)
    changed["events"][0]["event_type"] = "unknown"
    with pytest.raises(ValueError, match="outbox_plan_threshold"):
        validate_outbox(changed)
    changed = deepcopy(journal)
    changed["outbox"]["plans"][0]["units"][1]["kind"] = "digest"
    with pytest.raises(ValueError, match="outbox_plan_coverage"):
        validate_outbox(changed)
