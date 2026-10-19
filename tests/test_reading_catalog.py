# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Метрики отметок, длины, года и Manga-таксономии без сетевых подмен."""

from copy import deepcopy

import pytest

from achievement_plans import AchievementStateError
from reading_catalog import (
    READING_ACHIEVEMENTS,
    TAXONOMY_FAMILIES,
    calculate_marks,
    manga_metadata_evidence,
    marked_count,
    reading_evidence,
    reading_family_inputs,
)


def _inputs(*, status="completed", eligible=True, chapters=100, volumes=2, evidence=None):
    row = {"status": status, "chapters": chapters, "volumes": volumes}
    return reading_family_inputs({"1": row}, {"1": eligible}, {"1": {"reading_evidence": evidence or {}}})


@pytest.mark.parametrize("definition", READING_ACHIEVEMENTS, ids=lambda definition: definition.family)
@pytest.mark.parametrize("surface", ["criterion", "congratulations"])
def test_reading_text_identifies_activity_and_media_without_report_context(definition, surface):
    text = definition.criterion(5) if surface == "criterion" else definition.award_text(5, 84)
    assert "Прочитано" in text
    assert "манги и ранобэ" in text
    assert "произведений" not in text


def test_pre_2000_text_identifies_publication_start_rather_than_reading_start():
    definition = next(item for item in READING_ACHIEVEMENTS if item.family == "pre_2000")
    for text in (definition.criterion(5), definition.award_text(5, 16)):
        assert "начавших выходить до 2000 года" in text


@pytest.mark.parametrize("status", ["completed", "watching", "rewatching", "on_hold", "dropped", "planned"])
def test_marks_include_every_current_status_without_completion_inference(status):
    inputs = _inputs(status=status, chapters=33, volumes=2, evidence={"chapters": 900})
    assert marked_count(inputs["chapters"]) == 33
    assert marked_count(inputs["volumes"]) == 2
    assert inputs["long_works"] == ({"1": True} if status == "completed" else {})


@pytest.mark.parametrize("bad", [None, -1, True, 1.5, "5", [], {}])
def test_malformed_marks_preserve_independent_volume_metric(bad):
    inputs = _inputs(chapters=bad, volumes=5)
    assert marked_count(inputs["chapters"]) is None
    assert marked_count(inputs["volumes"]) == 5


@pytest.mark.parametrize("eligible,expected", [(True, 0), (False, 0), (None, None)])
def test_explicit_zero_and_unknown_kind_are_distinct(eligible, expected):
    assert marked_count(_inputs(eligible=eligible, chapters=0)["chapters"]) == expected


def test_excluded_records_do_not_require_progress_but_keep_observed_marks():
    checkpoint = _inputs(eligible=False, chapters=None)["chapters"]
    assert checkpoint == {"1": {"eligible": False, "marks": None}}
    assert marked_count(checkpoint) == 0


@pytest.mark.parametrize("old_eligibility", [False, None])
def test_eligibility_repair_uses_old_marks_before_same_update_increase(old_eligibility):
    previous = {"1": {"eligible": old_eligibility, "marks": 99}}
    current = {"1": {"eligible": True, "marks": 101}, "2": {"eligible": True, "marks": 20}}
    before = deepcopy((previous, current))
    evaluation = calculate_marks(previous, current)
    assert evaluation.current_value == 121 and evaluation.quiet_value == 99
    assert (previous, current) == before
    assert evaluation.checkpoint == current and evaluation.checkpoint["1"] is not current["1"]


def test_unknown_mark_repair_is_quiet_and_decreases_do_not_create_progress():
    assert calculate_marks({"1": {"eligible": True, "marks": None}}, _inputs()["chapters"]).quiet_value == 100
    assert calculate_marks({"1": {"eligible": True, "marks": 1000}}, _inputs()["chapters"]).quiet_value == 100
    assert calculate_marks(None, {}).current_value == 0


@pytest.mark.parametrize("checkpoint", [
    [], {"01": {"eligible": True, "marks": 5}}, {"1": {}},
    {"1": {"eligible": 1, "marks": 5}}, {"1": {"eligible": True, "marks": -1}},
    {"1": {"eligible": True, "marks": True}}, {"1": {"eligible": True, "marks": 0, "extra": 1}},
])
def test_malformed_checkpoint_fails_closed(checkpoint):
    with pytest.raises(AchievementStateError):
        marked_count(checkpoint)


@pytest.mark.parametrize("evidence,long,old", [
    ({}, None, None),
    ({"chapters": None, "year": None}, False, False),
    ({"chapters": 0, "year": 2000}, False, False),
    ({"chapters": 99, "year": 1999}, False, True),
    ({"chapters": 100, "year": 2001}, True, False),
    ({"chapters": 101, "year": 1}, True, True),
    ({"chapters": "100", "year": 0}, None, None),
    ({"chapters": True, "year": -1}, None, None),
    ({"chapters": 100}, True, None),
])
def test_known_unknown_and_failed_length_and_year_are_distinct(evidence, long, old):
    inputs = _inputs(evidence=evidence)
    assert inputs["long_works"] == {"1": long}
    assert inputs["pre_2000"] == {"1": old}


@pytest.mark.parametrize("eligible", [False, None])
def test_kind_takes_precedence_over_length_and_taxonomy(eligible):
    inputs = _inputs(eligible=eligible, evidence={"chapters": 200, "year": 1990, "taxonomy": [[57, "genre"]]})
    for family in ("long_works", "pre_2000", "fantasy"):
        assert inputs[family] == {"1": eligible}


@pytest.mark.parametrize("family,title,kind,tag_id,label", TAXONOMY_FAMILIES)
def test_each_approved_manga_taxon_requires_id_and_graphql_kind(family, title, kind, tag_id, label):
    assert _inputs(evidence={"taxonomy": [[tag_id, kind], [tag_id, kind]]})[family] == {"1": True}
    assert _inputs(evidence={"taxonomy": [[tag_id, "theme"]]})[family] == {"1": False}
    wrong_kind = "genre" if kind == "demographic" else "demographic"
    assert _inputs(evidence={"taxonomy": [[tag_id, wrong_kind]]})[family] == {"1": False}
    assert _inputs(evidence={"taxonomy": []})[family] == {"1": False}
    assert _inputs()[family] == {"1": None}
    definition = next(item for item in READING_ACHIEVEMENTS if item.family == family)
    assert definition.title == title
    assert definition.level_for(5) == 1 and definition.next_level(5) == 100
    assert definition.criterion(5).endswith("75")
    qualifier = "в жанре" if kind == "genre" else "категории"
    for text in (definition.criterion(5), definition.award_text(5, 84)):
        assert f"{qualifier} «{label}»" in text


def test_exact_taxonomy_ids_and_overlapping_memberships_do_not_use_names_or_rest_category():
    assert [(kind, tag_id) for _, _, kind, tag_id, _ in TAXONOMY_FAMILIES] == [
        ("genre", 57), ("genre", 62), ("genre", 49), ("genre", 50),
        ("genre", 80), ("genre", 46), ("genre", 53), ("genre", 76),
        ("demographic", 47), ("demographic", 63), ("demographic", 52), ("demographic", 87),
    ]
    evidence = manga_metadata_evidence({"genres": [
        {"id": "57", "kind": "genre", "name": "Fantasy"},
        {"id": "62", "kind": "genre", "name": "Romance"},
        {"id": "47", "kind": "demographic", "name": "Shounen"},
        {"id": "78", "kind": "genre", "name": "Shounen Ai"},
    ]})
    inputs = _inputs(evidence=evidence)
    assert inputs["fantasy"] == inputs["romance"] == inputs["shounen"] == {"1": True}
    assert inputs["shoujo"] == {"1": False}
    rest = manga_metadata_evidence({"genres": [{"id": 47, "kind": "genre", "name": "Shounen"}]})
    assert _inputs(evidence=rest)["shounen"] == {"1": False}


@pytest.mark.parametrize("raw", [None, {}, [None], [{"id": 57}], [{"id": True, "kind": "genre"}],
                                      [{"id": "", "kind": "genre"}], [{"id": 57, "kind": []}]])
def test_bad_taxonomy_does_not_become_empty_or_erase_known_other_evidence(raw):
    assert manga_metadata_evidence({"chapters": 100, "airedOn": {"year": 1999}, "genres": raw}) == {
        "chapters": 100, "year": 1999,
    }


def test_source_unknown_is_explicit_and_cached_evidence_is_revalidated():
    assert manga_metadata_evidence({"chapters": None, "airedOn": None, "genres": []}) == {
        "chapters": None, "year": None, "taxonomy": [],
    }
    assert manga_metadata_evidence({}) == {}
    assert reading_evidence({"reading_evidence": {"chapters": 100, "taxonomy": [[True, "genre"]]}}) == {"chapters": 100}
