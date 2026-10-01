# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Регрессии исходного квартала, source-порядка и ревизий закрытой истории."""

from copy import deepcopy

import pytest

from event_time_stats import (
    EventTimeStateError,
    acknowledge_revisions,
    correction_periods,
    ensure_event_time,
    event_period,
    index_event_periods,
    next_period,
    period_start,
    project_event,
    report_revisions,
    rotate_event_time,
    validate_event_time,
)
from messages import normalize_history_event


def _event(history_id, when, kind="completed", score=None, target="10", media="anime"):
    event = normalize_history_event(
        {
            "id": history_id,
            "created_at": when,
            "description": "Просмотрено",
            "target": {"id": target, "kind": "tv", "name": f"Title {target}"},
        },
        "2027-01-01T00:00:00+00:00",
    )
    event.update(event_type=kind, score=score, media=media)
    return event


def _current(period="2026-Q2", applied=0, events=None):
    cur = {
        "period": period,
        "events": events or [],
        "pending_quarter_delivery": None,
        "event_projection": {"journal_id": "a" * 32, "baseline_seq": 0, "applied_seq": applied},
    }
    ensure_event_time(cur)
    return cur


def _project(events, cur=None):
    cur = _current() if cur is None else cur
    journal = {"events": []}
    for event in events:
        event = deepcopy(event)
        journal["events"].append(event)
        seq = len(journal["events"])
        project_event(cur, journal, seq)
        cur["event_projection"]["applied_seq"] = seq
        validate_event_time(cur, journal)
    return cur


@pytest.mark.parametrize(
    ("source", "period"),
    [
        ("2026-04-01T02:59:59+03:00", "2026-Q1"),
        ("2026-04-01T03:00:00+03:00", "2026-Q2"),
        ("2026-03-31T20:00:00-04:00", "2026-Q2"),
        ("2026-06-30T23:59:59Z", "2026-Q2"),
        ("2026-07-01T00:00:00Z", "2026-Q3"),
        ("2026-10-01T00:00:00Z", "2026-Q4"),
        ("2027-01-01T03:00:00+03:00", "2027-Q1"),
    ],
)
def test_utc_source_boundaries_and_offsets(source, period):
    assert event_period(_event(1, source)) == (period, None)


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (None, "missing"),
        ("", "missing"),
        ("2026-04-01T00:00:00", "naive"),
        ("bad", "invalid"),
        ({"date": "bad"}, "invalid"),
        ("2027-01-01T00:00:01Z", "future"),
    ],
)
def test_unknown_and_future_times_remain_unallocated(source, reason):
    event = _event(1, source)
    original = deepcopy(event)
    cur = _project([event])
    assert cur["events"] == []
    assert cur["event_time"]["unknown"][reason] == 1
    assert event == original


def test_source_order_and_dedup_are_independent_of_admission_ids():
    events = [
        _event(80, "2026-04-01T00:00:00Z", "score_set", 9),
        _event(2, "2026-04-02T00:00:00Z"),
        _event(7, "2026-04-03T00:00:00Z", "score_changed", 6),
        _event(3, "2026-04-04T00:00:00Z", "score_removed"),
        _event(1, "2026-04-05T00:00:00Z", "score_set", 8),
        _event(100, "2026-04-06T00:00:00Z"),
    ]
    forward = _project(events)
    backwards = _project(list(reversed(events)))
    assert forward["events"] == backwards["events"]
    assert len(forward["events"]) == 1
    assert forward["events"][0]["score"] == 8
    assert _project(events[:3])["events"][0]["score"] == 6
    assert _project(events[:4])["events"][0]["score"] is None


def test_timestamp_ties_use_exact_history_id_and_scores_stay_in_source_quarter():
    events = [
        _event(3, "2026-04-01T00:00:00Z", "score_removed"),
        _event(1, "2026-04-01T00:00:00Z", "completed", 8),
        _event(2, "2026-04-01T00:00:00Z", "score_changed", 9),
        _event(4, "2026-07-01T00:00:00Z", "score_set", 10),
    ]
    cur = _project(events)
    assert cur["events"][0]["score"] is None
    assert cur["event_time"]["periods"]["2026-Q3"]["events"] == []


def test_migration_uses_applied_checkpoint_and_preserves_legacy_and_pending():
    legacy = [{"id": "old", "media": "anime", "event": "completed", "score": 8}]
    cur = {
        "period": "2026-Q2",
        "events": legacy,
        "pending_quarter_delivery": {"opaque": "existing frozen content"},
        "event_projection": {"journal_id": "a" * 32, "baseline_seq": 0, "applied_seq": 2},
    }
    before = deepcopy(cur)
    assert ensure_event_time(cur)
    assert cur["event_time"]["baseline_seq"] == 2
    assert {key: value for key, value in cur.items() if key != "event_time"} == before
    assert not ensure_event_time(cur)


def test_correction_acknowledges_only_the_revision_frozen_with_report():
    cur = _project([_event(1, "2026-01-01T00:00:00Z", score=8)])
    assert correction_periods(cur) == ["2026-Q1"]
    fresh = {
        "period": "2026-Q3",
        "events": [],
        "event_projection": deepcopy(cur["event_projection"]),
    }
    plan = {
        "plan_id": "b" * 32,
        "old_period": "2026-Q2",
        "event_time_revisions": report_revisions(cur),
    }
    fresh["pending_quarter_delivery"] = plan
    rotate_event_time(cur, fresh, plan)
    journal = {
        "events": [
            _event(1, "2026-01-01T00:00:00Z", score=8),
            _event(2, "2026-01-02T00:00:00Z", "score_removed"),
        ]
    }
    project_event(fresh, journal, 2)
    fresh["event_projection"]["applied_seq"] = 2
    acknowledge_revisions(fresh)
    validate_event_time(fresh, journal)
    bucket = fresh["event_time"]["periods"]["2026-Q1"]
    assert (bucket["announced_revision"], bucket["revision"]) == (1, 2)
    assert correction_periods(fresh) == ["2026-Q1"]
    assert cur["event_time"]["periods"]["2026-Q1"]["events"][0]["score"] == 8


def test_next_period_and_explicit_start_cross_year():
    assert next_period("2026-Q4") == "2027-Q1"
    assert period_start("2027-Q1") == "2027-01-01T00:00:00+00:00"
    with pytest.raises(EventTimeStateError):
        next_period("9999-Q4")


def test_recovery_rejects_plausible_but_non_journal_content_and_unknown_counts():
    event = _event(1, "2026-04-01T00:00:00Z", score=8)
    cur = _project([event])
    corrupted = deepcopy(cur)
    corrupted["event_time"]["periods"]["2026-Q2"]["events"][0]["score"] = 9
    corrupted["events"] = deepcopy(corrupted["event_time"]["periods"]["2026-Q2"]["events"])
    validate_event_time(corrupted)
    with pytest.raises(EventTimeStateError, match="recovery_payload"):
        validate_event_time(corrupted, {"events": [event]})
    corrupted = deepcopy(cur)
    corrupted["event_time"]["unknown"]["missing"] = 1
    with pytest.raises(EventTimeStateError, match="recovery_counts"):
        validate_event_time(corrupted, {"events": [event]})


@pytest.mark.parametrize(
    "damage",
    [
        "version",
        "baseline",
        "unknown",
        "revision",
        "ack",
        "current",
        "timestamp",
        "score",
        "extra_record",
    ],
)
def test_strict_state_rejects_invalid_revisions_current_and_record_fields(damage):
    cur = _project([_event(1, "2026-04-01T00:00:00Z")])
    state = cur["event_time"]
    if damage == "version":
        state["version"] = True
    elif damage == "baseline":
        state["baseline_seq"] = True
    elif damage == "unknown":
        state["unknown"]["missing"] = True
    elif damage == "revision":
        state["periods"]["2026-Q2"]["announced_revision"] = 2
    elif damage == "ack":
        state["report_ack"] = {"plan_id": "wrong", "revisions": {}}
    elif damage == "current":
        cur["events"] = []
    else:
        ev = state["periods"]["2026-Q2"]["events"][0]
        if damage == "timestamp":
            ev["recorded_at"] = "2026-01-01T00:00:00+00:00"
        elif damage == "score":
            ev["score"] = True
        else:
            ev["extra"] = "bad"
        cur["events"] = deepcopy(state["periods"]["2026-Q2"]["events"])
    with pytest.raises(EventTimeStateError):
        validate_event_time(cur)


@pytest.mark.parametrize("field", ["baseline_seq", "applied_seq"])
@pytest.mark.parametrize("value", ["missing", None, True, False, 0.5, "0", [], {}])
def test_projection_cursor_damage_raises_typed_state_error(field, value):
    cur = _current(applied=2 if field == "baseline_seq" else 0)
    if value == "missing":
        cur["event_projection"].pop(field)
    else:
        cur["event_projection"][field] = value
    with pytest.raises(EventTimeStateError, match="event_time_structure"):
        validate_event_time(cur)


@pytest.mark.parametrize("value", ["missing", None, {}, False, ""])
def test_current_events_damage_raises_typed_state_error(value):
    cur = _current()
    if value == "missing":
        cur.pop("events")
    else:
        cur["events"] = value
    with pytest.raises(EventTimeStateError):
        validate_event_time(cur)


@pytest.mark.parametrize("reverse", [False, True])
def test_indexed_projection_resumes_prefix_and_matches_source_order(reverse):
    events = [
        _event(1, "2026-04-01T00:00:00Z", score=3),
        _event(9, "2026-04-04T00:00:00Z", "score_removed"),
        _event(3, "2026-04-02T00:00:00Z", score=8),
        _event(7, "2026-04-03T00:00:00Z", "score_changed", 6),
        _event(8, "2026-07-01T00:00:00Z", score=9),
        _event(4, None),
        _event(5, "2027-01-01T00:00:01Z"),
    ]
    if reverse:
        events[1:] = reversed(events[1:])
    cur = _current(applied=1)
    journal = {"events": events}
    project_event(cur, journal, 2)
    cur["event_projection"]["applied_seq"] = 2
    reference = deepcopy(cur)
    before = deepcopy(journal)
    periods = index_event_periods(cur, journal)
    for seq in range(3, len(events) + 1):
        project_event(cur, journal, seq, period_events=periods)
        project_event(reference, journal, seq)
        cur["event_projection"]["applied_seq"] = seq
        reference["event_projection"]["applied_seq"] = seq
        validate_event_time(cur, journal)
        assert cur == reference
    assert journal == before
    assert cur["events"][0]["score"] is None
    assert cur["event_time"]["unknown"]["missing"] == 1
    assert cur["event_time"]["unknown"]["future"] == 1
