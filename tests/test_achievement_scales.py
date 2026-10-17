# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Точная матрица пяти шкал принадлежит общей шкале, а не её потребителям."""

import pytest

from achievement_scales import (
    CHAPTER_SCALE,
    RARE_SCALE,
    READER_SCALE,
    TAXONOMY_SCALE,
    VOLUME_SCALE,
)


@pytest.mark.parametrize("scale,initial,step,twentieth,twenty_first", [
    (READER_SCALE, (1, 5, 10, 25, 50), 25, 425, 450),
    (CHAPTER_SCALE, (25, 100, 250, 500, 1000), 1000, 16000, 17000),
    (VOLUME_SCALE, (1, 5, 10, 50, 100), 150, 2350, 2500),
    (RARE_SCALE, (1, 3, 5, 10, 15), 5, 90, 95),
    (TAXONOMY_SCALE, (5, 10, 25, 50, 75), 25, 450, 475),
])
def test_exact_initial_boundaries_and_uncapped_constant_tail(scale, initial, step, twentieth, twenty_first):
    assert scale.threshold(0) == 0 and scale.level_for(0) == 0
    expected = (*initial, *(initial[-1] + step * index for index in range(1, 21)))
    for level, threshold in enumerate(expected, 1):
        assert scale.threshold(level) == threshold
        assert scale.level_for(threshold - 1) == level - 1
        assert scale.level_for(threshold) == level
        assert scale.next_threshold(level - 1) == threshold
    assert scale.threshold(20) == twentieth
    assert scale.threshold(21) == twenty_first
    assert scale.threshold(10**6 + 1) - scale.threshold(10**6) == step
    assert scale.level_for(scale.threshold(10**6)) == 10**6
