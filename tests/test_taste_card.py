# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контрпримеры и границы чистых аналитических правил карточки."""

from dataclasses import replace
from datetime import (
    datetime,
    timezone,
)
from fractions import Fraction

import pytest

from taste_card import (
    TasteInput,
    TasteTitle,
    build_taste_card,
)

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)


def _title(index, score=7, genre="Драма", **kwargs):
    return TasteTitle(
        index, f"Тайтл {index}", f"https://shikimori.io/animes/{index}",
        kwargs.pop("status", "completed"), kwargs.pop("kind", "tv"),
        score=score, genres=(genre,) if genre else (), **kwargs,
    )


def _card(titles):
    return build_taste_card(TasteInput("DEMO", "anime", NOW, None, tuple(titles)))


def test_overview_genres_histogram_and_outcomes_use_independent_selections():
    card = _card([
        replace(_title(1, 10, status="planned", kind="ova", community=Fraction(2)),
                genres=("Драма", "драма", "Приключения")),
        _title(2, 1, "Приключения", status="watching", community=Fraction(10)),
        _title(3, 10, "Драма", status="dropped"),
        _title(4, None, "", status="dropped"),
        _title(5, 6, "", kind="movie"),
    ])
    assert card.available and card.overview.statuses == (1, 1, 0, 1, 0, 2)
    assert card.overview.formats == (("Сериалы", 3), ("OVA", 1), ("Фильмы", 1))
    assert card.overview.known_genres == 3
    assert card.overview.genres == (("Драма", 2), ("Приключения", 2))
    assert card.histogram == (1, 0, 0, 0, 0, 1, 0, 0, 0, 1)
    assert card.rated == 3 and card.preferences.baseline == Fraction(17, 3)
    assert card.paired == 1 and card.below.title.id == 2 and card.above is None
    assert card.dropping.rate == Fraction(2, 3)


def test_overview_genre_rows_stop_at_five_without_changing_denominator():
    card = _card(_title(index, genre=f"Жанр {index}", status="planned") for index in range(1, 8))
    assert card.available and card.rated == 0 and card.dropping.rate is None
    assert card.overview.known_genres == 7
    assert card.overview.genres == tuple((f"Жанр {index}", 1) for index in range(1, 6))


def test_paired_comparisons_include_rated_dropped_and_have_stable_ties():
    titles = [
        _title(2, 9, community=Fraction("6.8"), status="dropped"),
        _title(1, 9, community=Fraction("6.8")),
        _title(3, 5, community=Fraction("8.1")),
        _title(4, None, community=Fraction(10), status="dropped"),
        _title(5, 8),
    ]
    card = _card(titles)
    assert card.rated == 4 and card.paired == 3
    assert card.above.title.id == 1 and card.above.delta == Fraction("2.2")
    assert card.below.title.id == 3 and card.below.delta == Fraction("-3.1")
    assert card.dropping.total == 5 and card.dropping.dropped == 2
    reordered = _card(reversed(titles))
    assert replace(reordered, source=card.source) == card


@pytest.mark.parametrize(("count", "state"), [(0, "unavailable"), (1, "insufficient"), (5, "neutral")])
def test_comparison_distinguishes_missing_small_and_neutral(count, state):
    card = _card(_title(index, community=Fraction(7)) for index in range(count))
    assert card.comparison_state == state


def test_preferences_are_relative_symmetric_and_do_not_force_a_side():
    titles = [_title(index, 9 if index < 10 else 5 if index < 20 else 7,
                     "Детектив" if index < 10 else "Романтика" if index < 20 else "Драма")
              for index in range(60)]
    card = _card(titles)
    assert card.preferences.baseline == 7
    assert card.preferences.higher.group.label == "Детектив"
    assert card.preferences.lower.group.label == "Романтика"
    assert card.preferences.higher.delta == 2 and card.preferences.lower.delta == -2
    example_ids = {title.id for title in card.examples}
    assert card.preferences.higher.example.id in example_ids
    assert card.preferences.lower.example.id in example_ids
    only_higher = _card([replace(title, score=7) if title.score == 5 else title for title in titles])
    assert only_higher.preferences.higher is not None
    assert only_higher.preferences.lower is None


def test_high_absolute_score_below_usual_level_is_never_positive_preference():
    titles = [_title(index, 8 if index < 5 else 9, "Группа" if index < 10 else "Другие")
              for index in range(30)]
    prefs = _card(titles).preferences
    assert prefs.baseline > Fraction("8.5")
    assert prefs.higher is None


@pytest.mark.parametrize("sign", [1, -1])
def test_one_extreme_rating_cannot_create_group_level_preference(sign):
    usual, extreme = (1, 10) if sign == 1 else (10, 1)
    titles = [_title(index, extreme if index == 0 else usual,
                     "Группа" if index < 8 else "Остальные")
              for index in range(30)]
    prefs = _card(titles).preferences
    assert prefs.higher is None and prefs.lower is None
    assert prefs.state == "neutral"


def test_preference_threshold_is_inclusive_before_rounding():
    titles = [_title(index, 8 if index < 6 else 7, "Группа" if index < 8 else "Остальные")
              for index in range(40)]
    # Сумма 280, группа 62/8: ровно +0,75.
    titles = [replace(title, score=6) if 8 <= title.id < 14 else title for title in titles]
    prefs = _card(titles).preferences
    assert prefs.higher.delta == Fraction(3, 4)


def test_preferences_require_axis_coverage_and_a_comparison_group():
    titles = [_title(index, 9 if index < 8 else 7, "Группа" if index < 8 else "")
              for index in range(30)]
    prefs = _card(titles).preferences
    assert prefs.state == "insufficient" and prefs.higher is None
    assert prefs.coverage == (8, 0, 30)
    assert _card(_title(index) for index in range(100)).preferences.state == "insufficient"


def test_dropping_compares_rates_and_ignores_current_statuses_and_ratings():
    titles = [_title(index, None, "Популярный" if index < 60 else "Остальные",
                     status="dropped" if index % 5 == 0 else "completed")
              for index in range(100)]
    base = _card(titles)
    assert base.dropping.rate == Fraction(1, 5)
    assert base.dropping.state == "neutral" and base.dropping.pattern is None
    extended = _card(titles + [_title(index, None, status="watching") for index in range(100, 600)])
    assert extended.dropping == base.dropping
    assert extended.rated == 0 and not extended.diversity.formats


@pytest.mark.parametrize(("group_size", "group_dropped", "expected"), [(10, 4, True), (2, 2, False)])
def test_dropping_sample_and_effect_boundaries(group_size, group_dropped, expected):
    titles = [_title(index, None, "Группа" if index < group_size else "Другие",
                     status="dropped" if index < group_dropped or group_size <= index < group_size + 10 - group_dropped else "completed")
              for index in range(50)]
    dropping = _card(titles).dropping
    assert dropping.rate == Fraction(1, 5)
    assert (dropping.pattern is not None) == expected
    if expected:
        assert dropping.pattern.delta == Fraction(1, 5)


def test_diversity_describes_rated_titles_and_keeps_single_title_facts():
    card = _card([_title(1, year=1998), _title(2, None, year=2025, status="dropped")])
    assert card.diversity.years == (1998,)
    assert card.diversity.decades == (1990,)
    assert card.diversity.formats == (("Сериалы", 1),)
    assert card.diversity.genre is None and card.diversity.rare is None
    assert card.rated == 1 and card.dropping.total == 2


def test_diversity_rare_format_and_deterministic_example():
    titles = [_title(index, kind="ova" if index in {29, 30} else "tv", year=2000 + index % 20)
              for index in range(1, 31)]
    diversity = _card(reversed(titles)).diversity
    assert diversity.rare.id == 29 and diversity.rare_label == "OVA"
    assert diversity.genre == ("Драма", 30)


def test_sparse_tags_do_not_make_a_rare_known_genre_characteristic():
    titles = [_title(index, genre="Драма" if index < 3 else "", year=2000) for index in range(30)]
    diversity = _card(titles).diversity
    assert diversity.genre is None
    assert diversity.formats == (("Сериалы", 30),)
    assert len(diversity.years) == 30


def test_large_lists_keep_full_counts_but_bounded_examples():
    card = _card(_title(index, community=Fraction(5)) for index in range(3000))
    assert card.rated == 3000 and card.paired == 3000 and card.dropping.total == 3000
    example_ids = [title.id for title in card.examples]
    assert card.above.title.id in example_ids
    assert len(example_ids) == len(set(example_ids)) <= 6
