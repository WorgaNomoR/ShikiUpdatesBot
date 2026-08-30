# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контракты банка фактов для продолжения inline-выдачи."""

import pytest

from inline_facts import (
    FACT_QUERY_MATCH,
    FACT_QUERY_REJECT,
    FACT_QUERY_UNRELATED,
    GENERAL_INLINE_FACTS,
    INLINE_FACTS,
    OWNER_INLINE_FACTS,
    classify_fact_query,
    select_fact,
    select_inline_fact,
    select_next_fact,
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


@pytest.mark.parametrize(
    "query",
    [
        "fact",
        "FACT",
        "  FaCt  ",
        "\tfact\n",
        "факт",
        "ФАКТ",
        "  ФаКт  ",
    ],
)
def test_public_fact_query_accepts_only_exact_casefolded_whitespace_forms(query):
    assert classify_fact_query(query) == FACT_QUERY_MATCH


@pytest.mark.parametrize(
    "query",
    [
        "facts",
        "fact anime",
        "fact!",
        "f a c t",
        "factoid",
        "факты",
        "факт аниме",
        "факт?",
        "ф а к т",
        "фактология",
    ],
)
def test_fact_like_suffixes_malformed_spellings_and_near_misses_are_rejected(query):
    assert classify_fact_query(query) == FACT_QUERY_REJECT


@pytest.mark.parametrize(
    "query",
    [None, "", "anime fact", "аниме факт", "interesting", "фак"],
)
def test_unrelated_queries_remain_available_to_media_routing(query):
    assert classify_fact_query(query) == FACT_QUERY_UNRELATED


def test_public_selection_is_stable_and_next_rotation_never_repeats_current():
    current = select_fact("telegram-inline-query-id")

    assert select_fact("telegram-inline-query-id") == current
    assert select_next_fact(current.id) != current


def test_next_rotation_rejects_unknown_fact_id():
    with pytest.raises(ValueError, match="идентификатор"):
        select_next_fact("forged")
