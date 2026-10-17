# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Общая authority обслуживает разные метрики и независимые baseline."""

import json
from copy import deepcopy

import pytest
from achievement_helpers import capture_reader

from achievement_plans import (
    AchievementStateError,
    encode_owner_state,
)
from bot_achievement_catalog import BOT_ACHIEVEMENTS
from bot_achievements import (
    bot_state_json,
    capture_bot_achievements,
    parse_bot_member,
)
from notification_outbox import (
    begin_attempt,
    complete_attempt,
    recipient_progress_reserve,
)


def capture(state=None, inputs=None, *, catalog=BOT_ACHIEVEMENTS):
    return capture_bot_achievements(
        state, inputs or {}, catalog=catalog, profile="WNR", owner_id=999, now=1000,
    )


def test_production_catalog_has_exact_approved_families():
    assert tuple(BOT_ACHIEVEMENTS) == (
        "reader", "chapters", "volumes", "long_works", "pre_2000", "fantasy", "romance",
        "comedy", "drama", "horror", "mystery", "sci_fi", "sports", "shounen", "shoujo", "seinen", "josei",
    )


def test_distinct_metrics_share_authority_and_new_family_baselines(bot_catalog):
    state = capture(inputs={"reader": {}, "anime_minutes": {"minutes": None}}, catalog=bot_catalog)
    assert state["families"]["reader"]["initialized"] is True
    assert state["families"]["anime_minutes"]["initialized"] is False
    state = capture(state, {"reader": {"1": True}, "anime_minutes": {"minutes": 120}}, catalog=bot_catalog)
    assert state["earned"] == {"reader": 1, "anime_minutes": 60}
    assert [plan["awards"] for plan in state["pending"]] == [{"reader": 1}]
    first = deepcopy(state)
    state = capture(state, {"reader": {"1": None}, "anime_minutes": {"minutes": 185}}, catalog=bot_catalog)
    assert state["families"]["reader"]["current_value"] is None
    assert state["families"]["reader"]["initialized"] is True
    assert state["earned"] == {"reader": 1, "anime_minutes": 180}
    assert state["pending"][:1] == first["pending"]
    assert state["pending"][-1]["awards"] == {"anime_minutes": 180}
    assert capture(state, {"anime_minutes": {"minutes": 0}}, catalog=bot_catalog)["pending"] == state["pending"]
    raw = bot_state_json(state, catalog=bot_catalog, profile="WNR", owner_id=999).encode()
    assert parse_bot_member(raw, catalog=bot_catalog, profile="WNR", owner_id=999) == state


def test_adding_definition_keeps_old_checkpoint_and_initializes_new_family_silently(bot_catalog):
    state = capture(inputs={"reader": {"1": True}})
    old = deepcopy(state["families"]["reader"])
    result = capture(state, {"anime_minutes": {"minutes": 185}}, catalog=bot_catalog)
    assert result["families"]["reader"] == old
    assert result["earned"] == {"reader": 1, "anime_minutes": 180}
    assert result["pending"] == []


def test_unavailable_family_does_not_initialize_or_change_sibling(bot_catalog):
    state = capture(inputs={"anime_minutes": {"minutes": 120}}, catalog=bot_catalog)
    before = deepcopy(state["families"]["anime_minutes"])
    result = capture(state, {"reader": {"1": None}}, catalog=bot_catalog)
    assert result["families"]["anime_minutes"] == before
    assert result["families"]["reader"]["initialized"] is False
    assert result["earned"] == state["earned"] and result["pending"] == []


@pytest.mark.parametrize("damage", ["unknown", "tier", "checkpoint", "initialized", "pending"])
def test_common_validator_rejects_inconsistent_family_and_delivery(bot_catalog, damage):
    state = capture(inputs={"anime_minutes": {"minutes": 0}}, catalog=bot_catalog)
    state = capture(state, {"anime_minutes": {"minutes": 185}}, catalog=bot_catalog)
    if damage == "unknown":
        state["families"]["unregistered"] = state["families"]["anime_minutes"]
    elif damage == "tier":
        state["earned"]["anime_minutes"] = 61
    elif damage == "checkpoint":
        state["families"]["anime_minutes"]["checkpoint"]["minutes"] = 0
    elif damage == "initialized":
        state["families"]["anime_minutes"]["initialized"] = False
    else:
        state["pending"][0]["awards"]["anime_minutes"] = 181
    with pytest.raises(ValueError):
        bot_state_json(state, catalog=bot_catalog, profile="WNR", owner_id=999)






def _completed(count):
    return {str(index): True for index in range(1, count + 1)}


def _capture(state=None, completed=None, *, count=0):
    return capture_reader(
        state, _completed(count) if completed is None else completed,
        profile="WNR", owner_id=999, now=1000,
    )


def _json(state):
    return bot_state_json(state, catalog=BOT_ACHIEVEMENTS, profile="WNR", owner_id=999)


@pytest.mark.parametrize("count,tier", [(0, 0), (1, 1), (51, 5), (500, 23), (1002, 43)])
def test_first_complete_snapshot_is_silent_including_valid_zero(count, tier):
    state = _capture(count=count)
    assert state["families"]["reader"]["initialized"] is True
    assert state["families"]["reader"]["current_value"] == count
    assert state["earned"].get("reader", 0) == tier
    assert state["pending"] == []
    assert _capture(state, count=count) == state
    assert parse_bot_member(_json(state).encode(), catalog=BOT_ACHIEVEMENTS, profile="WNR", owner_id=999) == state


def test_crossings_coalesce_highest_and_survive_decrease_and_recrossing():
    state = _capture(count=99)
    state = _capture(state, count=100)
    first = deepcopy(state["pending"])
    assert state["earned"] == {"reader": 7}
    assert len(first) == 1 and first[0]["awards"] == {"reader": 7}
    state = _capture(state, count=1501)
    assert len(state["pending"]) == 2
    assert state["pending"][-1]["awards"] == {"reader": 63}
    assert state["pending"][:1] == first
    frozen = deepcopy(state["pending"])
    state = _capture(state, count=1)
    assert state["families"]["reader"]["current_value"] == 1 and state["earned"] == {"reader": 63}
    state = _capture(state, count=1501)
    assert state["pending"] == frozen
    _json(state)


def test_incomplete_metadata_never_initializes_or_claims_zero():
    state = _capture(completed={"1": None})
    assert state["families"]["reader"]["current_value"] is None and state["families"]["reader"]["initialized"] is False
    assert state["earned"] == {} and state["pending"] == []
    _json(state)
    repaired = _capture(state, completed={"1": True})
    assert repaired["families"]["reader"]["initialized"] is True and repaired["earned"] == {"reader": 1}
    assert repaired["pending"] == []


@pytest.mark.parametrize("previous", [False, None])
def test_metadata_reclassification_is_silent_but_later_source_progress_is_not(previous):
    state = _capture(count=4)
    state = _capture(state, completed={**_completed(4), "5": previous})
    assert state["earned"] == {"reader": 1}
    state = _capture(state, count=5)
    assert state["earned"] == {"reader": 2} and state["pending"] == []
    state = _capture(state, count=10)
    assert state["pending"][0]["awards"] == {"reader": 3}


def test_repair_and_new_completion_in_same_snapshot_reconcile_in_that_order():
    state = _capture(count=4)
    state = _capture(state, completed={**_completed(4), "5": None})
    state = _capture(state, count=10)
    assert state["earned"] == {"reader": 3}
    assert [plan["awards"] for plan in state["pending"]] == [{"reader": 3}]


@pytest.mark.parametrize("damage", [
    "version", "identity", "profile", "owner", "initialized", "count", "bool_count",
    "earned", "bool_earned", "dedup", "missing", "negative_id", "bool_eligibility",
    "baseline", "startup", "tier", "parts", "delivered", "extra",
])
def test_invalid_authority_fails_closed(damage):
    state = _capture(_capture(count=0), count=1)
    if damage == "version":
        state["version"] = 99
    elif damage == "identity":
        state["state_id"] = "broken"
    elif damage == "profile":
        state["profile"] = "other"
    elif damage == "owner":
        state["owner_id"] = True
    elif damage == "initialized":
        state["families"]["reader"]["initialized"] = 1
    elif damage == "count":
        state["families"]["reader"]["current_value"] = 0
    elif damage == "bool_count":
        state["families"]["reader"]["current_value"] = True
    elif damage == "earned":
        state["earned"]["reader"] = -1
    elif damage == "bool_earned":
        state["earned"]["reader"] = True
    elif damage == "dedup":
        state["earned"] = {}
    elif damage == "missing":
        del state["families"]["reader"]["checkpoint"]
    elif damage == "negative_id":
        state["families"]["reader"]["checkpoint"] = {"-1": True}
    elif damage == "bool_eligibility":
        state["families"]["reader"]["checkpoint"] = {"1": 1}
    elif damage == "baseline":
        state["families"]["reader"]["initialized"] = False
    elif damage == "startup":
        state["pending"][0]["kind"] = "baseline"
    elif damage == "tier":
        state["pending"][0]["awards"] = {"reader": 2}
    elif damage == "parts":
        state["pending"][0]["total_parts"] = 2
    elif damage == "delivered":
        state["pending"][0]["parts"][0]["delivery"]["status"] = "delivered"
    else:
        state["extra"] = True
    with pytest.raises(AchievementStateError):
        parse_bot_member(json.dumps(state).encode(), catalog=BOT_ACHIEVEMENTS, profile="WNR", owner_id=999)


@pytest.mark.parametrize("raw", [b'{', b'\xff', b'{"version":1,"version":1}', b'[' * 1500, b' ' * (8 * 1024 * 1024 + 1)], ids=["json", "encoding", "duplicate", "nesting", "oversize"])
def test_bounded_strict_json_rejects_corruption(raw):
    with pytest.raises(AchievementStateError):
        parse_bot_member(raw, catalog=BOT_ACHIEVEMENTS, profile="WNR", owner_id=999)


def test_reader_capacity_pays_future_attempts_inclusive_boundary():
    state = _capture(_capture(count=0), count=1)
    delivery = state["pending"][0]["parts"][0]["delivery"]
    budget = len(_json(state).encode()) + recipient_progress_reserve(delivery)
    with pytest.raises(AchievementStateError, match="capacity"):
        encode_owner_state(state, max_bytes=budget - 1)
    for _ in range(5):
        begin_attempt(delivery, delivery["next_attempt_at"])
        complete_attempt(delivery, "uncertain", delivery["attempts"][-1]["at"], retry_delay=0)
        encode_owner_state(state, max_bytes=budget)
        _json(state)


def test_catalog_baselines_independently_and_groups_highest_genuine_crossings():
    from achievement_helpers import catalog_inputs

    state = capture(inputs={"reader": {"1": True}})
    state = capture(state, catalog_inputs(1))
    assert state["pending"] == [] and len(state["families"]) == 17
    state = capture(state, catalog_inputs(5))
    assert len(state["pending"]) == 1
    awards = state["pending"][0]["awards"]
    assert set(awards) == set(BOT_ACHIEVEMENTS)
    assert awards["reader"] == 2 and awards["chapters"] == 4 and awards["volumes"] == 3
    assert awards["long_works"] == awards["pre_2000"] == 3
    assert awards["fantasy"] == awards["josei"] == 1
    assert state["pending"][0]["total_parts"] == 1
    text = state["pending"][0]["parts"][0]["text"]
    assert "уровень 4" in text and text.count("Новые достижения бота!") == 1
    assert capture(state, catalog_inputs(5)) == state
    frozen = deepcopy(state["pending"])
    state = capture(state, catalog_inputs(0))
    state = capture(state, catalog_inputs(5))
    assert state["pending"] == frozen and state["earned"] == awards
    assert parse_bot_member(_json(state).encode(), catalog=BOT_ACHIEVEMENTS, profile="WNR", owner_id=999) == state


def test_new_independent_family_does_not_congratulate_historical_progress():
    state = capture(inputs={"chapters": {"1": {"eligible": True, "marks": None}}, "volumes": {}})
    state = capture(state, {"chapters": {"1": {"eligible": True, "marks": 3000}},
                            "volumes": {"1": {"eligible": True, "marks": 100}}})
    assert state["earned"] == {"chapters": 7, "volumes": 5}
    assert [plan["awards"] for plan in state["pending"]] == [{"volumes": 5}]


def test_mixed_mark_eligibility_correction_and_real_growth_awards_only_after_quiet_tier():
    state = capture(inputs={"chapters": {"1": {"eligible": False, "marks": 99}}})
    state = capture(state, {"chapters": {"1": {"eligible": True, "marks": 100}}})
    assert state["pending"][0]["awards"] == {"chapters": 2}


def test_grouped_congratulations_split_only_at_telegram_limit_and_keep_frozen_old_plan():
    from dataclasses import replace

    from bot_achievements import achievement_catalog

    reader = replace(BOT_ACHIEVEMENTS["reader"], award_text=lambda level, value: "<b>" + "😀" * 3000 + "</b>")
    catalog = achievement_catalog(reader, BOT_ACHIEVEMENTS["volumes"])
    state = capture(inputs={"reader": {}, "volumes": {}}, catalog=catalog)
    state = capture(state, {"reader": {"1": True}, "volumes": {"1": {"eligible": True, "marks": 100}}}, catalog=catalog)
    plan = state["pending"][0]
    assert plan["awards"] == {"reader": 1, "volumes": 5}
    assert len(plan["parts"]) == 2
    for part in plan["parts"]:
        from catchup_digest import html_length
        assert html_length(part["text"]) <= 4096
    old = deepcopy(plan)
    state = capture(state, {"reader": _completed(5)}, catalog=catalog)
    assert state["pending"][0] == old
