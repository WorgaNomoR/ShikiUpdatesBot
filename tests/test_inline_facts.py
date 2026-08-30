# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контракты банка фактов для продолжения inline-выдачи."""

import pytest

from inline_facts import (
    GENERAL_INLINE_FACTS,
    INLINE_FACTS,
    OWNER_INLINE_FACTS,
    select_inline_fact,
)


def test_fact_bank_has_expected_size_unique_ids_and_owner_bonus():
    assert len(GENERAL_INLINE_FACTS) == 50
    assert len(OWNER_INLINE_FACTS) == 6
    assert len(INLINE_FACTS) == 56
    assert len({fact.id for fact in INLINE_FACTS}) == 56
    assert len({fact.text for fact in INLINE_FACTS}) == 56
    assert all(not fact.owner_pick for fact in GENERAL_INLINE_FACTS)
    assert all(fact.owner_pick for fact in OWNER_INLINE_FACTS)
    assert sum("Евангелион" in fact.text for fact in OWNER_INLINE_FACTS) == 2
    assert sum("Фрирен" in fact.text for fact in OWNER_INLINE_FACTS) == 2
    assert sum("цикады" in fact.text for fact in OWNER_INLINE_FACTS) == 2


def test_selection_is_stable_and_avoids_repeats_for_a_full_bank_cycle():
    seed = "777\0anime\0fate"

    first_pass = [
        select_inline_fact(seed, page=page)
        for page in range(2, 2 + len(INLINE_FACTS))
    ]
    second_pass = [
        select_inline_fact(seed, page=page)
        for page in range(2, 2 + len(INLINE_FACTS))
    ]

    assert first_pass == second_pass
    assert len({fact.id for fact in first_pass}) == len(INLINE_FACTS)


def test_selection_rejects_first_page():
    with pytest.raises(ValueError, match="продолжения"):
        select_inline_fact("seed", page=1)
