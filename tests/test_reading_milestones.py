# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Пороговая шкала и наблюдения калькулятора Reader."""

import pytest

from reading_milestones import (
    calculate_reader,
    next_reading_tier,
    reading_count,
    reading_tier,
)


@pytest.mark.parametrize("tier,next_tier", [
    (0, 1), (1, 5), (5, 10), (10, 25), (25, 50), (50, 100),
    (100, 250), (250, 500), (500, 1000), (1000, 1500), (1500, 2000), (10000, 10500),
])
def test_threshold_boundaries_and_numeric_identity(tier, next_tier):
    assert reading_tier(tier) == tier
    assert reading_tier(next_tier - 1) == tier
    assert reading_tier(next_tier) == next_tier
    assert next_reading_tier(tier) == next_tier



@pytest.mark.parametrize("completed,count", [({}, 0), ({"1": True, "2": False}, 1), ({"1": None}, None)])
def test_count_preserves_unknown_eligibility(completed, count):
    assert reading_count(completed) == count


def test_reader_reconciles_previously_observed_ids_before_new_completions():
    old = {"1": True, "2": None, "3": False}
    current = {"1": True, "2": True, "3": True, "4": True}
    result = calculate_reader(old, current)
    assert result.current_value == 4 and result.quiet_value == 3
    assert result.checkpoint == current and result.checkpoint is not current
    assert old == {"1": True, "2": None, "3": False}
