# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Абсолютные seq и диагностические отпечатки не являются watermark."""

from copy import deepcopy

import pytest

from source_history import (
    event_at_seq,
    event_count,
    known_history_ids,
    semantic_hash,
    source_suffix,
)


def test_exact_ids_and_absolute_suffix_do_not_assume_source_order():
    journal = {
        "baseline_ids": [500],
        "source_base": {"through_seq": 3, "ids": [[90, "a"], [-2, "b"], [7, "c"]]},
        "events": [{"seq": 4, "history_id": 1}, {"seq": 5, "history_id": 80}],
    }
    assert event_count(journal) == 5
    assert known_history_ids(journal) == {500, 90, -2, 7, 1, 80}
    assert event_at_seq(journal, 4) == journal["events"][0]
    assert source_suffix(journal, 0, 4) == journal["events"][:1]
    assert source_suffix(journal, 4, 5) == journal["events"][1:]
    for seq in [0, 1, 3, 6, True, 4.0]:
        with pytest.raises(ValueError, match="source_seq"):
            event_at_seq(journal, seq)


def test_semantic_fingerprint_preserves_original_numeric_equality_and_immutable_input():
    event = {"seq": 1, "history_id": 7, "created_at": {"value": [1.0, -0.0, True]}, "observed_at": "first"}
    before = deepcopy(event)
    repeated = {"history_id": 7, "created_at": {"value": [1, 0, 1]}, "seq": 900, "observed_at": "later"}
    assert semantic_hash(event) == semantic_hash(repeated)
    repeated["history_id"] = 8
    assert semantic_hash(event) != semantic_hash(repeated)
    assert event == before
