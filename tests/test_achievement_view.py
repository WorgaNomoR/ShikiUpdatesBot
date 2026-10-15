# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Текущий снимок, семейства и полное безопасное публичное представление."""

import re
from copy import deepcopy
from html import unescape

import pytest

from achievement_metadata import (
    AchievementMetadata,
    parse_achievement_metadata,
)
from achievement_view import (
    achievement_summary,
    build_achievement_report,
)
from report_delivery import freeze_report
from report_model import rendered_html


def _render(rows, *, metadata=None, invalid=False):
    return rendered_html(build_achievement_report(
        rows, base_url="https://shikimori.io", profile="WNR",
        metadata=metadata, invalid=invalid,
    ))


def test_wnr_current_earned_tier_keeps_next_level_progress_without_duplicate(achievement_row_factory):
    row = achievement_row_factory
    rows = [row(level=level, progress=100) for level in (1, 2, 3)]
    rows.append(row(level=4, progress=28))
    rows.append(row("first_unlock", level=0, progress=40))
    text = "\n".join(_render(rows))
    earned, progress = text.split("В процессе получения", 1)
    assert "уровень 4 · 28% до следующего уровня" in earned
    assert "animelist" not in progress and "first_unlock" in progress
    assert text.count("Достижение «animelist»") == 1
    assert "уровень 1" not in text and "уровень 3" not in text
    assert "Всего:" not in text and "Получено:" not in text


def test_passive_zero_progress_is_not_presented_as_in_progress(achievement_row_factory):
    text = "\n".join(_render([
        achievement_row_factory("passive", level=0, progress=0),
        achievement_row_factory("started", level=0, progress=26),
    ]))
    assert "passive" not in text
    assert "started" in text and "26%" in text
    assert "не получено" not in text


def test_first_unlocks_are_sorted_by_descending_progress_with_stable_ties(achievement_row_factory):
    row = achievement_row_factory
    rows = [row("low", 0, progress=3), row("middle", 0, progress=40),
            row("near-b", 0, progress=79), row("near-a", 0, progress=79),
            row("ready", 0, progress=100)]
    text = "\n".join(_render(rows))
    expected = ["ready", "near-a", "near-b", "middle", "low"]
    assert [text.index(f"Достижение «{family}»") for family in expected] == sorted(
        text.index(f"Достижение «{family}»") for family in expected
    )
    assert "\n".join(_render(list(reversed(rows)))) == text


def test_earned_sort_by_source_category_then_title_with_unknowns_retained(achievement_row_factory):
    definitions = [
        ("a-author", "Автор", "author"), ("b-franchise", "Франшиза", "franchise"),
        ("c-genre", "Бета жанр", "genre"), ("d-genre", "альфа жанр", "genre"),
        ("e-common", "Общее", "common"), ("f-future", "Будущая категория", "future"),
    ]
    rows = [achievement_row_factory(family, progress=100) for family, _, _ in definitions]
    rows.append(achievement_row_factory("missing", progress=100))
    metadata = {(family, 1): AchievementMetadata(title, category) for family, title, category in definitions}
    # Декорация чужого уровня не определяет категорию текущего достижения.
    metadata[("missing", 0)] = AchievementMetadata("Чужой уровень", "common")
    text = "\n".join(_render(rows, metadata=metadata))
    expected = ["Общее", "альфа жанр", "Бета жанр", "Франшиза", "Автор", "Будущая категория", "Достижение «missing»"]
    positions = [text.index(title) for title in expected]
    assert positions == sorted(positions)
    assert "Чужой уровень" not in text
    assert "\n".join(_render(list(reversed(rows)), metadata=metadata)) == text


def test_earned_same_title_has_stable_family_tie_break(achievement_row_factory):
    rows = [achievement_row_factory(family, progress=100) for family in ("b", "a")]
    metadata = {(family, 1): AchievementMetadata("Одно имя", "genre") for family in ("b", "a")}
    text = "\n".join(_render(rows, metadata=metadata))
    assert text.index("/genre/a") < text.index("/genre/b")
    assert text.count("Одно имя") == 2


@pytest.mark.parametrize("badge", [
    '<a class="b-badge level-1" title="Значок" href="/achievements/franchise/binary"></a>',
    '<div class="b-achievement is-badge level-1" data-title="Значок"><a href="/achievements/author/binary"></a></div>',
])
def test_source_binary_badge_has_no_level_but_first_cumulative_tier_keeps_it(achievement_row_factory, badge):
    metadata = parse_achievement_metadata(badge + '''<div class="b-achievement level-1" data-title="Первый шаг">
        <a href="/achievements/common/cumulative"></a></div>''')
    rows = [achievement_row_factory("binary", 1, progress=100),
            achievement_row_factory("cumulative", 1, progress=0)]
    text = "\n".join(_render(rows, metadata=metadata))
    badge_line = next(part for part in text.splitlines() if "Значок" in part)
    cumulative_line = next(part for part in text.splitlines() if "Первый шаг" in part)
    assert "уровень" not in badge_line
    assert "уровень 1" in cumulative_line


@pytest.mark.parametrize("level,progress", [(1, 40), (2, 100)])
def test_binary_hint_never_hides_a_higher_source_level_or_claims_a_next_badge_tier(
    achievement_row_factory, level, progress,
):
    metadata = {("binary", level): AchievementMetadata("Значок", "franchise", is_binary=True)}
    text = "\n".join(_render([achievement_row_factory("binary", level, progress=progress)], metadata=metadata))
    if level == 2:
        assert "уровень 2" in text
    else:
        assert "40%" in text and "следующего уровня" not in text and "уровень 1" not in text


def test_highest_current_tier_and_source_progress_have_no_monotonic_authority(achievement_row_factory):
    row = achievement_row_factory
    rows = [row(level=1), row(level=4, progress=7), row("zero", level=0, progress=100)]
    before = deepcopy(rows)
    text = "\n".join(_render(rows))
    assert text.count("Достижение «animelist»") == 1
    assert "уровень 4 · 7% до следующего уровня" in text
    assert "100%" not in text and "не получено" not in text
    assert "Полученные</b><b> · 1" in text and "zero" in text
    reduced = "\n".join(_render([row(level=1, progress=20)]))
    assert "уровень 4" not in reduced
    assert "уровень 1" in reduced
    assert "animelist" not in "\n".join(_render([]))
    assert rows == before


def test_labels_bind_exact_tier_and_unearned_zero_with_safe_unknowns(achievement_row_factory):
    rows = [achievement_row_factory("<unsafe&>", level=0, progress=100), achievement_row_factory(level=2)]
    labels = {
        ("<unsafe&>", 0): AchievementMetadata('Имя <&"', "genre", "Описание <&>"),
        ("animelist", 1): AchievementMetadata("Другой уровень", "common"),
    }
    text = "\n".join(_render(rows, metadata=labels))
    assert "Имя &lt;&amp;&quot;" in text
    assert "Другой уровень" not in text
    assert "Достижение «animelist»" in text
    assert "/achievements/genre/%3Cunsafe%26%3E" in text
    assert "Описание &lt;&amp;&gt;" in text and "не получено" not in text
    assert "<unsafe&>" not in "\n".join(_render(rows))


@pytest.mark.parametrize("rows,invalid,expected", [
    (None, False, "ещё не загружены"), (None, True, "недоступны"), ([], False, "Список достижений пуст"),
])
def test_missing_invalid_empty_are_distinct(rows, invalid, expected):
    text = "\n".join(_render(rows, invalid=invalid))
    assert expected in text
    assert "owner" not in text and "pending" not in text
    assert "Получено:" not in text and "Достижения на Shikimori" not in text


def test_large_views_keep_every_family_and_oversized_unknown_text(achievement_row_factory):
    families = [f"family-{index:04}" for index in range(1500)] + ["😀<&>" * 1500]
    chunks = _render([achievement_row_factory(family, progress=100) for family in families])
    assert len(chunks) > 1
    visible = unescape(re.sub(r"<[^>]+>", "", "".join(chunks)))
    for family in families:
        assert visible.count(family) == 1


def test_summary_counts_current_families_and_distinguishes_unavailable(achievement_row_factory):
    rows = [achievement_row_factory(level=1), achievement_row_factory(level=2), achievement_row_factory("zero", 0, progress=100)]
    assert "<b>1</b>" in achievement_summary(rows)
    assert "<b>0</b>" in achievement_summary([])
    assert "пока не загружены" in achievement_summary(None)
    assert "недоступны" in achievement_summary(None, invalid=True)


def test_achievement_report_uses_native_rich_lists_with_complete_html_fallback(achievement_row_factory):
    rows = [achievement_row_factory(level=3, progress=7), achievement_row_factory("unearned", 0, progress=100)]
    report = build_achievement_report(rows, base_url="https://shikimori.io", profile="WNR")
    frozen = freeze_report(report, disable_preview=True)
    assert len(frozen) == 1 and frozen[0]["transport"] == "rich"
    blocks = frozen[0]["content"]["blocks"]
    assert any(block["type"] == "heading" and block["size"] == 1 for block in blocks)
    details = [block for block in blocks if block["type"] == "details"]
    assert len(details) == 2 and all(block["is_open"] for block in details)
    assert all(any(child["type"] == "list" for child in block["blocks"]) for block in details)
    assert blocks[0]["type"] == "anchor" and blocks[1]["type"] == "photo"
    assert "↑ К началу" in str(blocks[-1])
    fallback = "\n".join(frozen[0]["fallback_html"])
    assert fallback.count("Достижение «animelist»") == 1
    assert fallback.count("Достижение «unearned»") == 1
    assert "7% до следующего уровня" in fallback and "100%" not in fallback
    assert frozen[0]["fallback_disable_preview"] is True


def test_large_rich_or_html_fallback_preserves_every_family(achievement_row_factory):
    families = [f"family-{index:04}" for index in range(1500)]
    report = build_achievement_report(
        [achievement_row_factory(family, progress=100) for family in families],
        base_url="https://shikimori.io", profile="WNR",
    )
    frozen = freeze_report(report)
    fallback = "\n".join(
        part
        for transport in frozen
        for part in (transport["fallback_html"] if transport["transport"] == "rich" else [transport["content"]])
    )
    assert all(fallback.count(family) == 1 for family in families)


def test_missing_artwork_keeps_complete_achievement_html_report(
    achievement_row_factory, monkeypatch, tmp_path,
):
    monkeypatch.setattr("report_assets.RESOURCE_ROOT", tmp_path)
    rows = [achievement_row_factory("earned", 2, progress=28),
            achievement_row_factory("started", 0, progress=42)]
    report = build_achievement_report(rows, base_url="https://shikimori.io", profile="WNR")

    frozen = freeze_report(report)

    assert frozen and all(unit["transport"] == "html" for unit in frozen)
    text = "\n".join(unit["content"] for unit in frozen)
    assert "earned" in text and "28% до следующего уровня" in text
    assert "started" in text and "42%" in text


def test_exact_tier_criterion_explains_earned_level_without_repeating_family(achievement_row_factory):
    metadata = {("animelist", 4): AchievementMetadata("Беззаботный зритель", "common", "150 просмотренных аниме")}
    text = "\n".join(_render([achievement_row_factory(level=4, progress=28)], metadata=metadata))
    assert text.count("150 просмотренных аниме") == 1
    assert text.count("Беззаботный зритель") == 1
    assert "В процессе получения" not in text


@pytest.mark.parametrize("progress", [0, 28, 100])
def test_earned_percentage_only_shows_nonzero_next_level_progress(achievement_row_factory, progress):
    text = "\n".join(_render([achievement_row_factory(level=4, progress=progress)]))
    assert ("% до следующего уровня" in text) == (progress == 28)
    assert "В процессе получения" not in text
