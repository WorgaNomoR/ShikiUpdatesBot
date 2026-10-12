# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Абсолютные seq и диагностические отпечатки не являются watermark."""

from copy import deepcopy

import pytest

from source_history import (
    event_at_seq,
    event_count,
    known_history_ids,
    rebase_source_candidate,
    retain_source_fingerprints,
    same_history_authority,
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


@pytest.mark.parametrize("prefix", [4095, 4096, 4097, 4224])
def test_fingerprint_window_keeps_exact_seq_ids_and_read_only_source_facts(source_index_factory, prefix):
    old, cur = source_index_factory(prefix=prefix)
    before = deepcopy(old)
    candidate = retain_source_fingerprints(old)
    assert old == before
    base = candidate["source_base"]
    cutoff = max(0, prefix - 4096)
    assert base["version"] == 2
    assert base["through_seq"] == prefix
    assert base["ids"] == [[row[0], None if i < cutoff else row[1]] for i, row in enumerate(before["source_base"]["ids"])]
    for key in ["binding", "periods", "unknown"]:
        assert base[key] == before["source_base"][key]
    assert candidate["events"] == old["events"]
    assert candidate["outbox"] == old["outbox"]
    assert known_history_ids(candidate) == known_history_ids(old)
    assert retain_source_fingerprints(candidate) is candidate


def test_fingerprint_only_maintenance_preserves_lease_and_rebases_same_boundary(source_index_factory):
    old, _ = source_index_factory()
    candidate = retain_source_fingerprints(old)
    assert same_history_authority(candidate, old)
    assert not same_history_authority(old, candidate)
    admission = deepcopy(old)
    admission["events"].append({"seq": event_count(old) + 1, "history_id": 87654321})
    rebased = rebase_source_candidate(admission, candidate)
    assert rebased["source_base"] == candidate["source_base"]
    assert rebased["events"] == admission["events"]
    tampered = deepcopy(candidate)
    tampered["source_base"]["binding"]["legacy_hash"] = "f" * 64
    assert not same_history_authority(tampered, old)


def test_window_moves_with_absolute_compacted_seq_and_keeps_old_source_facts(source_index_factory):
    from event_time_stats import (
        compact_source_history,
        validate_event_time,
        validate_source_base,
    )

    full, cur = source_index_factory()
    old = retain_source_fingerprints(full)
    candidate = old
    for through in [4101, 4102]:
        candidate = compact_source_history(candidate, cur, through)
        validate_source_base(candidate)
        validate_event_time(cur, candidate)
        cutoff = through - 4096
        assert all(row[1] is None for row in candidate["source_base"]["ids"][:cutoff])
        assert all(isinstance(row[1], str) for row in candidate["source_base"]["ids"][cutoff:])
        assert same_history_authority(candidate, old)
        assert known_history_ids(candidate) == known_history_ids(full)
    assert candidate["events"] == []
    assert candidate["source_base"]["unknown"] == old["source_base"]["unknown"]
    assert candidate["source_base"]["binding"] == old["source_base"]["binding"]
    tampered = deepcopy(candidate)
    tampered["source_base"]["ids"][-1][1] = "f" * 64
    assert not same_history_authority(tampered, old)
