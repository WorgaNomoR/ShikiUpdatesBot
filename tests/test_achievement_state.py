# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Source, durable capture и единая строгая runtime/import схема."""

import json
from copy import deepcopy

import pytest

from achievement_state import (
    AchievementSourceError,
    AchievementStateError,
    achievement_state_json,
    capture_achievements,
    earned_levels,
    parse_achievement_member,
    parse_achievement_source,
)
from notification_outbox import (
    begin_attempt,
    complete_attempt,
)


def _capture(state, rows):
    return capture_achievements(state, rows, profile="WNR", user_id=42, owner_id=999, now=1000, base_url="https://shikimori.io")


def _json(state):
    return achievement_state_json(state, profile="WNR", owner_id=999)


def test_source_earned_levels_and_empty_success(achievement_row_factory):
    rows = [achievement_row_factory(level=0, progress=100), achievement_row_factory(level=4, progress=7), achievement_row_factory(level=1)]
    parsed = parse_achievement_source(rows, 42)
    assert earned_levels(parsed) == {"animelist": 4}
    assert rows[0]["level"] == 0
    assert parse_achievement_source([], 42) == []


@pytest.mark.parametrize("changes", [
    {"user_id": 99}, {"user_id": True}, {"neko_id": " "}, {"neko_id": None},
    {"level": True}, {"level": -1}, {"level": 0.5}, {"progress": 101},
    {"progress": True}, {"id": False}, {"created_at": {}},
])
def test_source_rejects_whole_response(achievement_row_factory, changes):
    with pytest.raises(AchievementSourceError):
        parse_achievement_source([achievement_row_factory(), {**achievement_row_factory(level=2), **changes}], 42)


@pytest.mark.parametrize("payload", [None, {}, False, [None], [{"level": 1}]])
def test_malformed_source_cannot_initialize(payload):
    with pytest.raises(AchievementSourceError):
        _capture(None, payload)


def test_duplicate_source_identity_is_not_a_complete_snapshot(achievement_row_factory):
    row = achievement_row_factory()
    with pytest.raises(AchievementSourceError):
        parse_achievement_source([row, {**row, "id": 100}], 42)


def test_baseline_and_grouped_promotions_survive_source_changes(achievement_row_factory):
    row = achievement_row_factory
    state = _capture(None, [row(level=0, progress=100), row(level=2, id=10)])
    assert state["earned"] == {"animelist": 2}
    assert len(state["pending"]) == 1
    assert state["pending"][0]["kind"] == "baseline"
    assert len(state["pending"][0]["parts"]) == 1
    baseline = deepcopy(state["pending"])
    state = _capture(state, [row(level=3), row(level=5), row("evangelion", 1, id=50)])
    assert state["pending"][:1] == baseline
    assert state["pending"][1]["awards"] == {"animelist": 5, "evangelion": 1}
    assert "2 →" in state["pending"][1]["parts"][0]["text"]
    for rows in ([row(level=5, id=999, progress=99)], [row(level=1)], [], [row(level=5)]):
        # Диск/restart сохраняет максимумы, а current snapshot следует source.
        state = parse_achievement_member(_json(state).encode(), profile="WNR", owner_id=999)
        state = _capture(state, rows)
        assert len(state["pending"]) == 2
        assert state["earned"] == {"animelist": 5, "evangelion": 1}
        assert state["snapshot"] == parse_achievement_source(rows, 42)


def test_empty_first_response_initializes_one_summary():
    state = _capture(None, [])
    assert state["earned"] == {}
    assert "<b>0</b>" in state["pending"][0]["parts"][0]["text"]
    assert _capture(state, [])["pending"] == state["pending"]


@pytest.mark.parametrize("damage", ["version", "profile", "owner", "earned", "snapshot", "html", "attempt", "parts", "terminal", "baseline"])
def test_malformed_state_fails_closed(achievement_state_factory, damage):
    state = achievement_state_factory()
    part = state["pending"][0]["parts"][0]
    if damage == "version":
        state["version"] = True
    elif damage == "profile":
        state["profile"] = "Another"
    elif damage == "owner":
        state["owner_id"] = 123
    elif damage == "earned":
        state["earned"] = {}
    elif damage == "snapshot":
        state["snapshot"][0]["user_id"] = 1
    elif damage == "html":
        part["text"] = "<script>payload</script>"
    elif damage == "attempt":
        part["delivery"]["attempts"] = [{"at": 999, "outcome": "uncertain"}]
    elif damage == "parts":
        state["pending"][0]["total_parts"] = 10**100
    elif damage == "terminal":
        part["delivery"]["status"] = "delivered"
    else:
        duplicate = deepcopy(state["pending"][0])
        duplicate["plan_id"] = "b" * 32
        state["pending"].append(duplicate)
    with pytest.raises(AchievementStateError):
        parse_achievement_member(json.dumps(state).encode(), profile="WNR", owner_id=999)


@pytest.mark.parametrize("raw", [b'{"version":1,"version":1}', b'\xff', b'{', b'[' * 1500])
def test_strict_member_rejects_broken_json(raw):
    with pytest.raises(AchievementStateError):
        parse_achievement_member(raw, profile="WNR", owner_id=999)


def test_source_numeric_identity_mismatch_cannot_capture(achievement_state_factory):
    with pytest.raises(AchievementStateError, match="profile"):
        capture_achievements(achievement_state_factory(), [], profile="WNR", user_id=43, owner_id=999, now=1000, base_url="https://shikimori.io")


@pytest.mark.parametrize("outcome", ["confirmed_rejection", "not_dispatched", "uncertain"])
def test_capacity_reserves_all_future_attempt_growth(achievement_state_factory, monkeypatch, outcome):
    import achievement_state
    from notification_outbox import recipient_progress_reserve

    state = achievement_state_factory()
    delivery = state["pending"][0]["parts"][0]["delivery"]
    budget = len(_json(state).encode()) + recipient_progress_reserve(delivery)
    monkeypatch.setattr("achievement_state.ACHIEVEMENTS_MAX_BYTES", budget - 1)
    with pytest.raises(AchievementStateError, match="capacity"):
        _json(state)
    monkeypatch.setattr("achievement_state.ACHIEVEMENTS_MAX_BYTES", budget)
    original = _json(state)
    now = 1000
    for _ in range(6):
        begin_attempt(delivery, now)
        _json(state)
        complete_attempt(delivery, outcome, now, retry_delay=0)
        if delivery["status"] != "pending":
            state["pending"] = []
        _json(state)
        if state["pending"]:
            now = delivery["next_attempt_at"]
    # Реальные bytes, включая reserve, определяют inclusive границу.
    assert len(original.encode()) < achievement_state.ACHIEVEMENTS_MAX_BYTES
