# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Пороговая шкала и наблюдения калькулятора Reader."""

import pytest

from reading_milestones import (
    calculate_reader,
    next_reading_tier,
    reading_award_text,
    reading_count,
    reading_criterion,
    reading_tier,
)


def test_reader_uses_ordinal_levels_and_approved_thresholds():
    assert reading_tier(51) == 5
    assert next_reading_tier(5) == 75


@pytest.mark.parametrize("level,threshold,count", [(1, 1, 1), (5, 50, 51)])
def test_reader_text_identifies_reading_manga_and_ranobe(level, threshold, count):
    assert reading_criterion(level) == f"Прочитано манги и ранобэ: {threshold}"
    assert f"Прочитано манги и ранобэ: <b>{count}</b>" in reading_award_text(level, count)



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
