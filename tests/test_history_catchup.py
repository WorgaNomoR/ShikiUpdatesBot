# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Равенство source ID и связность вместо числовых watermark."""

from history_catchup import (
    advance_acquisition,
    new_acquisition,
    resume_acquisition,
)


def test_resume_rechecks_frontier_and_bridges_head_before_completion():
    state = new_acquisition()
    assert advance_acquisition(state, [90, 2, 70], {1}, 2) == (True, False)
    state = resume_acquisition(state)
    assert state["page"] == 1
    assert advance_acquisition(state, [90, 2, 70], {1}, 2) == (True, False)
    assert advance_acquisition(state, [70, 3, 1], {1}, 2) == (True, False)
    assert state["phase"] == "head"
    assert state["page"] == 1
    assert advance_acquisition(state, [8, 90, 2], {1}, 2) == (True, True)


def test_disconnected_known_id_or_end_never_finishes():
    state = new_acquisition()
    advance_acquisition(state, [9, 8, 7], {1}, 2)
    assert advance_acquisition(state, [6, 5, 1], {1}, 2) == (False, False)
    assert advance_acquisition(state, [], {1}, 2) == (False, False)
    assert state["page"] == 1
    assert state["frontier"] == []


def test_reordered_overlap_cannot_prove_a_connected_boundary():
    state = new_acquisition()
    advance_acquisition(state, [90, 2, 70], {1}, 2)
    assert advance_acquisition(state, [70, 2, 1], {1}, 2) == (False, False)


def test_offset_seek_retains_anchor_until_exact_reconnection():
    state = new_acquisition()
    advance_acquisition(state, [9, 8, 7], {1}, 2)
    state = resume_acquisition(state)
    assert advance_acquisition(state, [11, 10, 9], {1}, 2) == (False, False)
    assert state["frontier"] == [9, 8, 7]
    assert advance_acquisition(state, [9, 8, 7], {1}, 2) == (True, False)
    assert advance_acquisition(state, [7, 1], {1}, 2) == (True, False)
    assert state["phase"] == "head"


def test_one_cycle_short_page_keeps_existing_completion_contract():
    state = new_acquisition()
    assert advance_acquisition(state, [2, 90], {1}, 2) == (True, True)


def test_known_id_before_resume_anchor_does_not_close_unscanned_tail():
    state = new_acquisition()
    advance_acquisition(state, [9, 8, 7], {1}, 2)
    assert advance_acquisition(state, [1, 7, 6], {1}, 2) == (True, False)


def test_repeated_staged_page_is_not_an_accepted_history_boundary():
    state = new_acquisition()
    advance_acquisition(state, [9, 8, 7], {1}, 2)
    state = resume_acquisition(state)
    for _ in range(5):
        assert advance_acquisition(state, [9, 8, 7], {1}, 2) == (True, False)
    assert state["phase"] == "tail"
