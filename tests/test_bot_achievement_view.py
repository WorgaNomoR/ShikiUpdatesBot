# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Сохранённый уровень, условие и честный процент до следующего порога."""

from copy import deepcopy

import pytest

from achievement_view import build_achievement_report
from bot_achievement_view import PublicBotAchievement
from reading_milestones import (
    next_reading_tier,
    reading_criterion,
)
from report_delivery import freeze_report
from report_model import rendered_html


def _report(reading=None, **kwargs):
    snapshot = None
    if reading:
        tier = reading["tier"]
        target = next_reading_tier(tier)
        snapshot = (PublicBotAchievement(
            "reader", "Читатель", reading["initialized"], tier, reading["count"], target,
            reading_criterion(tier or target),
        ),)
    return build_achievement_report(
        None, bot_achievements=snapshot, base_url="https://shikimori.io", profile="WNR", **kwargs,
    )


@pytest.mark.parametrize("reading,invalid,expected", [
    (None, False, "ещё не рассчитан"), (None, True, "временно недоступны"),
    ({"initialized": False, "tier": 0, "count": None}, False, "ещё не рассчитан"),
    ({"initialized": True, "tier": 0, "count": 0}, False, "Полученных достижений бота пока нет"),
    ({"initialized": True, "tier": 1, "count": 1}, False, "20% до следующего уровня"),
    ({"initialized": True, "tier": 50, "count": 51}, False, "51% до следующего уровня"),
    ({"initialized": True, "tier": 100, "count": 2}, False, "менее 1% до следующего уровня"),
    ({"initialized": True, "tier": 100, "count": 0}, False, "0% до следующего уровня"),
    ({"initialized": True, "tier": 100, "count": None}, False, "прогресс пока неизвестен"),
    ({"initialized": True, "tier": 0, "count": None}, False, "прогресс пока неизвестен"),
    ({"initialized": True, "tier": 1500, "count": 1501}, False, "75% до следующего уровня"),
    ({"initialized": True, "tier": 1000, "count": 1499}, False, "99% до следующего уровня"),
])
def test_public_reader_has_exact_progress_and_honest_availability(reading, invalid, expected):
    before = deepcopy(reading)
    text = "\n".join(rendered_html(_report(reading, bot_invalid=invalid)))
    assert "Бота" in text and expected in text
    assert reading == before
    if reading and reading["initialized"] and reading["count"] is not None and reading["count"] < reading["tier"]:
        assert f"уровень {reading['tier']}" in text and "уровень сохранён" in text
    if reading and reading["initialized"]:
        if reading["tier"]:
            criterion = (
                "1 завершённое произведение" if reading["tier"] == 1
                else f"{reading['tier']} завершённых произведений"
            )
            assert f"<i>{criterion} манги и ранобэ</i>" in text
        elif reading["count"] == 0:
            assert "Читатель" not in text and "0%" not in text
        if reading["count"] is None:
            assert "%" not in text
    assert "Завершённых произведений сейчас" not in text and "осталось" not in text
    assert "owner_id" not in text and "pending" not in text and "unlock" not in text


def test_reader_is_complete_in_rich_and_missing_artwork_html_fallback(monkeypatch, tmp_path):
    report = _report({"initialized": True, "tier": 50, "count": 51})
    units = freeze_report(report)
    assert units[0]["transport"] == "rich"
    assert "Бота" in str(units[0]["content"])
    assert "51% до следующего уровня" in str(units[0]["content"])
    assert "50 завершённых произведений манги и ранобэ" in str(units[0]["content"])
    assert "<i>50 завершённых произведений манги и ранобэ</i>" in "\n".join(units[0]["fallback_html"])
    assert "51% до следующего уровня" in "\n".join(units[0]["fallback_html"])
    monkeypatch.setattr("report_assets.RESOURCE_ROOT", tmp_path)
    fallback = freeze_report(report)
    assert all(unit["transport"] == "html" for unit in fallback)
    text = "\n".join(unit["content"] for unit in fallback)
    assert "51% до следующего уровня" in text
    assert "<i>50 завершённых произведений манги и ранобэ</i>" in text


def test_both_origins_have_common_groups_with_test_anime_and_no_duplicate_families(bot_catalog, achievement_row_factory):
    from achievement_helpers import capture_reader

    from bot_achievement_view import bot_achievement_snapshot
    from bot_achievements import capture_bot_achievements

    state = capture_reader(None, {"1": True}, profile="WNR", owner_id=999, now=1000)
    state = capture_bot_achievements(
        state, {"anime_minutes": {"minutes": 20}}, catalog=bot_catalog, profile="WNR", owner_id=999, now=1000,
    )
    projection = bot_achievement_snapshot(state, catalog=bot_catalog)
    source_rows = [achievement_row_factory(), achievement_row_factory("started", level=0, progress=5)]
    report = build_achievement_report(
        source_rows, bot_achievements=projection, base_url="https://shikimori.io", profile="WNR",
    )
    text = "\n".join(rendered_html(report))
    shiki, own = text.split("<b>Бота</b>")
    assert "Shikimori" in shiki
    for origin in (shiki, own):
        assert "Полученные" in origin and "В процессе получения" in origin
    assert own.count("Читатель") == 1 and own.count("Аниме &lt;&amp;&gt; время") == 1
    earned, progressing = own.split("В процессе получения")
    assert "Читатель" in earned and "Аниме" in progressing and "33%" in progressing
    assert "<i>60 минут аниме</i>" in progressing
    frozen = freeze_report(report)
    assert frozen[0]["transport"] == "rich"
    blocks = frozen[0]["content"]["blocks"]
    details = [block for block in blocks if block["type"] == "details"]
    assert len(details) == 4 and all(block["is_open"] for block in details)
    assert all(any(child["type"] == "list" for child in block["blocks"]) for block in details)
    assert "Аниме <&> время" in str(blocks)
    assert "Аниме &lt;&amp;&gt; время" in "\n".join(frozen[0]["fallback_html"])
