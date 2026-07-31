# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Доменные тесты грамматики отображаемого имени, собираемой при старте."""

import logging

import pytest
from pytrovich.enums import Gender

from name_grammar import (
    DisplayNameContext,
    GenderAlternative,
    build_display_name_context,
    format_name_template,
    is_eligible_first_name,
)


@pytest.mark.parametrize(
    "name",
    [
        "Костя",
        "Алёна",
        "Ёлка",
        "КОСТЯ",
        "костя",
        "Анна-Мария",
    ],
)
def test_eligible_russian_first_name_boundaries(name):
    assert is_eligible_first_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "Anna",
        "АннаMaria",
        "Анна Мария",
        "Анна2",
        "Анна_Мария",
        "Анна🙂",
        "Анна.Мария",
        "Анна—Мария",
        "Анна--Мария",
        "-Анна",
        "Анна-",
        "",
    ],
)
def test_ineligible_display_names(name):
    assert not is_eligible_first_name(name)


def test_auto_declines_kostya_in_all_required_cases():
    context = build_display_name_context("Костя")

    assert context == DisplayNameContext(
        nominative="Костя",
        genitive="Кости",
        dative="Косте",
        accusative="Костю",
        instrumental="Костей",
        gender="male",
        inflection_applied=True,
    )


def test_auto_declines_female_name_and_selects_female_gender():
    context = build_display_name_context("Анна")

    assert context == DisplayNameContext(
        nominative="Анна",
        genitive="Анны",
        dative="Анне",
        accusative="Анну",
        instrumental="Анной",
        gender="female",
        inflection_applied=True,
    )


def test_real_library_preserves_yo():
    context = build_display_name_context("Алёна")
    assert context.genitive == "Алёны"
    assert context.instrumental == "Алёной"


def test_real_library_declines_hyphenated_name():
    context = build_display_name_context("Анна-Мария")
    assert context.gender == "female"
    assert context.genitive == "Анны-Марии"
    assert context.accusative == "Анну-Марию"


def test_casing_variant_is_eligible_and_declined():
    context = build_display_name_context("КОСТЯ", "male")
    assert context.nominative == "КОСТЯ"
    assert context.genitive == "КОСТИ"


def test_hyphenated_components_preserve_their_own_casing():
    context = build_display_name_context("Анна-МАРИЯ", "female")
    assert context.genitive == "Анны-МАРИИ"


def test_auto_ambiguous_name_falls_back(caplog):
    with caplog.at_level(logging.INFO):
        context = build_display_name_context("Саша")

    assert context == DisplayNameContext(
        nominative="Саша",
        genitive="Саша",
        dative="Саша",
        accusative="Саша",
        instrumental="Саша",
        gender=None,
        inflection_applied=False,
    )
    assert "неоднозначен" in caplog.text


@pytest.mark.parametrize(
    ("mode", "expected_gender", "expected_word"),
    [
        ("male", "male", "сделал"),
        ("female", "female", "сделала"),
    ],
)
def test_explicit_gender_makes_ambiguous_name_deterministic(
    mode,
    expected_gender,
    expected_word,
):
    context = build_display_name_context("Саша", mode)
    text = format_name_template("{n} {g:сделал|сделала}", context)

    assert context.gender == expected_gender
    assert context.genitive == "Саши"
    assert text == f"Саша {expected_word}"


def test_none_skips_dependency_and_preserves_masculine_fallback():
    calls = []

    def counting_factory():
        calls.append(1)
        raise RuntimeError("морфология не должна запускаться")

    context = build_display_name_context(
        "Костя",
        "none",
        detector_factory=counting_factory,
        maker_factory=counting_factory,
    )

    assert calls == []
    assert context.inflection_applied is False
    assert context.gender is None
    assert context.genitive == "Костя"
    assert format_name_template("{g:доволен|довольна}", context) == "доволен"


def test_ineligible_name_skips_dependency():
    calls = []

    def counting_factory():
        calls.append(1)
        raise RuntimeError("морфология не должна запускаться")

    context = build_display_name_context(
        "WorgaNomoR",
        detector_factory=counting_factory,
        maker_factory=counting_factory,
    )

    assert calls == []
    assert context.genitive == "WorgaNomoR"
    assert context.inflection_applied is False


def test_invalid_gender_mode_falls_back_and_logs_once(caplog):
    invalid_mode = "private-marker<&>"
    with caplog.at_level(logging.WARNING):
        context = build_display_name_context("Костя", invalid_mode)

    assert context.genitive == "Костя"
    assert context.gender is None
    records = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(records) == 1
    assert invalid_mode not in caplog.text


def test_detector_failure_falls_back_and_logs_once(caplog):
    def broken_detector():
        raise RuntimeError("сломанные правила определения пола")

    with caplog.at_level(logging.WARNING):
        context = build_display_name_context(
            "Костя",
            detector_factory=broken_detector,
        )

    assert context.genitive == "Костя"
    assert context.gender is None
    records = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(records) == 1


def test_maker_failure_falls_back(caplog):
    def broken_maker():
        raise RuntimeError("сломанные правила склонения")

    with caplog.at_level(logging.WARNING):
        context = build_display_name_context(
            "Костя",
            maker_factory=broken_maker,
        )

    assert context.genitive == "Костя"
    assert context.inflection_applied is False
    assert "исходное имя" in caplog.text


def test_non_string_case_result_falls_back():
    class Detector:
        def detect(self, **_kwargs):
            return Gender.MALE

    class Maker:
        def make(self, *_args, **_kwargs):
            return None

    context = build_display_name_context(
        "Костя",
        detector_factory=Detector,
        maker_factory=Maker,
    )
    assert context.genitive == "Костя"
    assert context.inflection_applied is False


def test_indeclinable_name_keeps_unchanged_forms_and_female_grammar():
    context = build_display_name_context("Николь")

    assert context.gender == "female"
    assert context.inflection_applied is True
    assert {
        context.genitive,
        context.dative,
        context.accusative,
        context.instrumental,
    } == {"Николь"}
    assert format_name_template("{n} {g:доволен|довольна}", context) == (
        "Николь довольна"
    )


def test_html_escaping_happens_after_safe_fallback():
    context = build_display_name_context("<b>Костя&", "auto")
    text = format_name_template(
        "{n}|{n_gen}|{n_dat}|{n_acc}|{n_ins}",
        context,
    )

    assert text == "|".join(["&lt;b&gt;Костя&amp;"] * 5)
    assert "<b>" not in text


@pytest.mark.parametrize(
    ("gender", "expected"),
    [
        ("male", "посмотрел"),
        ("female", "посмотрела"),
        (None, "посмотрел"),
    ],
)
def test_gender_alternative_selection(gender, expected):
    assert format(GenderAlternative(gender), "посмотрел|посмотрела") == expected


@pytest.mark.parametrize(
    "format_spec",
    ["", "male", "|female", "male|", "male|female|other"],
)
def test_gender_alternative_rejects_malformed_specs(format_spec):
    with pytest.raises(ValueError, match="два непустых"):
        format(GenderAlternative("female"), format_spec)


def test_rendering_does_not_repeat_detection_or_inflection():
    calls = {"detector": 0, "maker": 0, "make": 0}

    class Detector:
        def __init__(self):
            calls["detector"] += 1

        def detect(self, **_kwargs):
            return Gender.MALE

    class Maker:
        def __init__(self):
            calls["maker"] += 1

        def make(self, _part, _gender, case, name, **_kwargs):
            calls["make"] += 1
            return f"{name}-{case.name}"

    context = build_display_name_context(
        "Костя",
        detector_factory=Detector,
        maker_factory=Maker,
    )
    for _ in range(5):
        format_name_template("{n_gen} {g:готов|готова}", context)

    assert calls == {"detector": 1, "maker": 1, "make": 4}
