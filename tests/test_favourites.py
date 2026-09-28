# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контракты сбора, обогащения и отчёта избранного."""

import asyncio
import copy
import json
import re
from unittest.mock import AsyncMock

import pytest

import favourites as fmod
import shiki_api
import storage
from report_model import (
    Bold,
    Italic,
    Link,
    Report,
    Text,
    rendered_html,
)

# Срез реального ответа /favourites: все 8 категорий, url=null везде,
# у TeddyLoid russian="" (должен фолбэкнуться на name).
FAV_SAMPLE = {
    "animes": [
        {"id": 226, "name": "Elfen Lied", "russian": "Эльфийская песнь", "url": None},
    ],
    "mangas": [
        {"id": 21525, "name": "Akatsuki no Yona", "russian": "Йона на заре", "url": None},
    ],
    "ranobe": [
        {"id": 74697, "name": "Re:Zero", "russian": "Re:Zero. Жизнь с нуля", "url": None},
    ],
    "characters": [],
    "people": [
        {"id": 30805, "name": "TeddyLoid", "russian": "", "url": None},
    ],
    "mangakas": [
        {"id": 32649, "name": "Tappei Nagatsuki", "russian": "Таппэй Нагацуки", "url": None},
    ],
    "seyu": [
        {"id": 34785, "name": "Rie Takahashi", "russian": "Риэ Такахаси", "url": None},
    ],
    "producers": [
        {"id": 38963, "name": "Masahiro Shinohara", "russian": "Масахиро Синохара", "url": None},
    ],
}


def _stats_with_titles():
    """stats_all с парой тайтлов для проверки джойна ссылок/оценок."""
    stats = storage._empty_stats_all()
    stats["anime"]["titles"] = {
        "226": {"title": "Эльфийская песнь", "url": "/animes/226-elfen-lied",
                "score": 9, "kind": "tv", "status": "completed"},
    }
    stats["manga"]["titles"] = {
        "21525": {"title": "Йона на заре", "url": "/mangas/21525-akatsuki-no-yona",
                  "score": 8, "kind": "manga", "status": "completed"},
    }
    return stats


@pytest.mark.asyncio
async def test_collect_favourites_join_with_titles(monkeypatch):
    stats = storage._empty_stats_all()
    stats["anime"]["titles"] = {
        "790": {"title": "Эрго Прокси", "url": "/animes/790", "score": 9},
        "5114": {"title": "ФМА", "url": "/animes/5114", "score": 0},  # без оценки
    }

    async def fake_fetch(session):
        return {
            "animes": [
                {"id": 790, "russian": "Эрго Прокси", "url": "/animes/790"},
                {"id": 5114, "russian": "ФМА", "url": "/animes/5114"},
                {"id": 9999, "russian": "Не в списке", "url": "/animes/9999"},  # нет в titles
            ],
            "mangas": [], "characters": [], "people": [],
        }
    monkeypatch.setattr("favourites.fetch_favourites", fake_fetch)

    class S:
        pass
    stats = await fmod._collect_favourites(S(), stats)
    fa = {e["id"]: e for e in stats["favourites"]["anime"]}

    assert fa["790"].get("score") == 9            # оценка из titles
    assert "score" not in fa["5114"]              # score=0 -> не показываем
    assert fa["9999"]["title"] == "Не в списке"   # не в titles -> имя из API
    assert "score" not in fa["9999"]


@pytest.mark.asyncio
async def test_collect_favourites_api_fail_keeps_previous(monkeypatch):
    stats = storage._empty_stats_all()
    stats["favourites"]["anime"] = [{"id": "1", "title": "Старое", "url": "/animes/1"}]

    async def fake_fetch(session):
        return None  # сбой API
    monkeypatch.setattr("favourites.fetch_favourites", fake_fetch)

    class S:
        pass
    stats = await fmod._collect_favourites(S(), stats)
    # Прежнее избранное не затёрто
    assert stats["favourites"]["anime"] == [{"id": "1", "title": "Старое", "url": "/animes/1"}]


@pytest.mark.asyncio
async def test_collect_favourites_explicit_none_keeps_previous_without_fetch(monkeypatch):
    """Дедуп: fav=None передан ЯВНО (= «недоступно в этом цикле») → оставляем
    прежнее БЕЗ повторного фетча. Контраст с fav не переданным (тот фетчит)."""
    stats = storage._empty_stats_all()
    stats["favourites"]["anime"] = [{"id": "1", "title": "Старое", "url": "/animes/1"}]

    fetched = False

    async def fake_fetch(session):
        nonlocal fetched
        fetched = True
        return {"animes": []}

    monkeypatch.setattr("favourites.fetch_favourites", fake_fetch)

    class S:
        pass
    result = await fmod._collect_favourites(S(), stats, fav=None)

    assert fetched is False       # повторного фетча не было
    assert result["favourites"]["anime"] == [{"id": "1", "title": "Старое", "url": "/animes/1"}]


def test_smoke_build_favourites_returns_report():
    assert isinstance(fmod.build_favourites_messages({"favourites": {"anime": [{"title": "Эрго Прокси", "url": "/animes/790", "score": 9}]}}), Report)


def test_favourites_use_dynamic_summary_category_counts_and_trailing_score():
    stats = storage._empty_stats_all()
    stats["favourites"]["anime"] = [
        {"title": "Первое", "url": "", "score": 8},
        {"title": "Второе", "url": ""},
    ]
    stats["favourites"]["ranobe"] = [{"title": "Третье", "url": ""}]

    report = fmod.build_favourites_messages(stats)

    assert report.units[0].sections[0].items[1].parts == (
        Italic("3 объекта  ·  2 категории"),
    )
    assert report.units[0].sections[1].items[0].parts == (
        Text("🎬 "),
        Bold("Аниме"),
        Text(" · 2"),
    )
    assert "Первое — 8⭐" in rendered_html(report)[0]
    assert "⭐8" not in rendered_html(report)[0]


def test_favourites_keep_untrusted_values_plain_until_renderer_boundary():
    stats = storage._empty_stats_all()
    stats["favourites"]["anime"] = [{
        "title": "A <B> & C",
        "url": '/animes/1?x=1&label="quoted"',
    }]

    report = fmod.build_favourites_messages(stats)
    title_node = report.units[0].sections[1].items[1].parts[1]

    assert title_node == Link(
        "A <B> & C",
        'https://shikimori.io/animes/1?x=1&label="quoted"',
    )


def test_links_single_domain_in_favourites():
    stats = storage._empty_stats_all()
    # Полный URL из GraphQL — провокация двойного домена
    stats["favourites"]["anime"] = [
        {"id": "1", "title": "Тест", "url": "https://shikimori.io/animes/226", "score": 10}
    ]
    msg = rendered_html(fmod.build_favourites_messages(stats))[0]
    # Домен должен встречаться ровно один раз в href
    hrefs = re.findall(r'href="([^"]*)"', msg)
    assert hrefs, "должна быть ссылка"
    for href in hrefs:
        assert href.count("shikimori.io") == 1, f"двойной домен: {href}"
        assert href.startswith("https://shikimori.io/"), href


def test_collect_favourites_merges_industry_and_adds_ranobe():
    stats = storage._empty_stats_all()
    out = asyncio.run(fmod._collect_favourites(None, stats, fav=FAV_SAMPLE))
    fav = out["favourites"]

    # Ранобэ — отдельный блок
    assert len(fav["ranobe"]) == 1
    assert fav["ranobe"][0]["id"] == "74697"

    # people + mangakas + seyu + producers слиты в один блок (4 человека)
    assert len(fav["people"]) == 4
    ids = {p["id"] for p in fav["people"]}
    assert ids == {"30805", "32649", "34785", "38963"}

    # Персонажи отдельно и пусты в этом срезе
    assert fav["characters"] == []


def test_collect_favourites_empty_russian_falls_back_to_name():
    stats = storage._empty_stats_all()
    out = asyncio.run(fmod._collect_favourites(None, stats, fav=FAV_SAMPLE))
    teddy = next(p for p in out["favourites"]["people"] if p["id"] == "30805")
    # russian был "" — заголовок не должен быть пустым, берём name
    assert teddy["title"] == "TeddyLoid"


def test_collect_favourites_url_join_from_titles():
    stats = _stats_with_titles()
    out = asyncio.run(fmod._collect_favourites(None, stats, fav=FAV_SAMPLE))
    anime = out["favourites"]["anime"][0]
    assert anime["url"] == "/animes/226-elfen-lied"   # ссылка подтянута из titles
    assert anime["score"] == 9                          # и оценка


def test_build_favourites_messages_has_ranobe_and_industry_blocks():
    stats = storage._empty_stats_all()
    stats["favourites"]["ranobe"] = [{"id": "1", "title": "Ранобэ-тайтл", "url": ""}]
    stats["favourites"]["people"] = [{"id": "2", "title": "Человек", "url": ""}]
    msg = rendered_html(fmod.build_favourites_messages(stats))[0]
    assert "Ранобэ" in msg
    assert "Люди индустрии" in msg


@pytest.mark.asyncio
async def test_omitted_response_without_session_preserves_snapshot(monkeypatch):
    stats = _stats_with_titles()
    snapshot = stats["favourites"]
    before = copy.deepcopy(stats)
    fetch = AsyncMock(side_effect=AssertionError("Лишний запрос"))
    monkeypatch.setattr("favourites.fetch_favourites", fetch)

    result = await fmod._collect_favourites(None, stats)

    assert result is stats
    assert result["favourites"] is snapshot
    assert result == before
    fetch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"animes": []}, None])
async def test_prefetched_empty_or_unavailable_response_never_fetches(monkeypatch, response):
    stats = _stats_with_titles()
    stats["favourites"]["anime"] = [{"id": "old", "title": "Старое", "url": ""}]
    snapshot = stats["favourites"]
    before = copy.deepcopy(stats)
    fetch = AsyncMock(side_effect=AssertionError("Повторный запрос"))
    monkeypatch.setattr("favourites.fetch_favourites", fetch)

    result = await fmod._collect_favourites(object(), stats, fav=response)

    assert result is stats
    fetch.assert_not_called()
    if response is None:
        assert result["favourites"] is snapshot
        assert result == before
    else:
        assert result["favourites"] == {
            "anime": [], "manga": [], "ranobe": [], "characters": [], "people": [],
        }


@pytest.mark.asyncio
async def test_collection_preserves_category_order_namespaces_and_raw_response(monkeypatch):
    stats = storage._empty_stats_all()
    stats["anime"]["titles"] = {
        "1": {"title": "Cached anime", "url": "https://shikimori.io/animes/1", "score": "9"},
    }
    stats["manga"]["titles"] = {
        "1": {"title": "Cached manga", "url": "/mangas/1", "score": 0},
        "2": {"title": "", "url": "", "score": "8"},
    }
    response = {
        "producers": [{"id": 9, "name": "Duplicate"}, {"id": 10, "name": "Producer"}],
        "ranobe": [{"id": 2, "russian": "API novel", "url": "https://shikimori.io/mangas/2"}],
        "animes": [{"id": 1, "name": "API anime"}, {"id": 1}, {"name": "No ID"}],
        "mangas": [{"id": 1, "name": "API manga"}],
        "characters": [{"id": 1, "name": "Character", "score": 10}],
        "people": [{"id": 7, "russian": "", "name": "First person"}],
        "mangakas": [{"id": "7", "name": "Duplicate"}, {"id": 8}],
        "seyu": [{"id": 9, "name": "Voice actor"}],
        "studios": [{"id": 99, "name": "Ignored"}],
    }
    original_response = copy.deepcopy(response)
    original_titles = copy.deepcopy((stats["anime"], stats["manga"]))
    fetch = AsyncMock(side_effect=AssertionError("Повторный запрос"))
    monkeypatch.setattr("favourites.fetch_favourites", fetch)

    result = await fmod._collect_favourites(None, stats, fav=response)

    assert result is stats
    assert result["favourites"] == {
        "anime": [
            {"id": "1", "title": "Cached anime", "url": "/animes/1", "score": 9},
            {"id": "1", "title": "Cached anime", "url": "/animes/1", "score": 9},
        ],
        "manga": [{"id": "1", "title": "Cached manga", "url": "/mangas/1"}],
        "ranobe": [{"id": "2", "title": "API novel", "url": "/mangas/2", "score": 8}],
        "characters": [{"id": "1", "title": "Character", "url": ""}],
        "people": [
            {"id": "7", "title": "First person", "url": ""},
            {"id": "8", "title": "???", "url": ""},
            {"id": "9", "title": "Voice actor", "url": ""},
            {"id": "10", "title": "Producer", "url": ""},
        ],
    }
    assert response == original_response
    assert (stats["anime"], stats["manga"]) == original_titles
    fetch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("score, expected", [("7", 7), (0, None), (-1, None), (None, None), ("bad", None)])
async def test_cached_score_conversion_and_api_name_url_fallback(score, expected):
    stats = storage._empty_stats_all()
    stats["manga"]["titles"]["2"] = {"title": None, "url": None, "score": score}
    response = {"ranobe": [{"id": 2, "russian": "", "name": "Novel", "url": "/mangas/2"}]}

    result = await fmod._collect_favourites(None, stats, fav=response)

    entry = {"id": "2", "title": "Novel", "url": "/mangas/2"}
    if expected is not None:
        entry["score"] = expected
    assert result["favourites"]["ranobe"] == [entry]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("unavailable"), shiki_api.ProfilePrivacyError("fetch_favourites")])
async def test_fetch_exception_propagates_without_replacing_snapshot(monkeypatch, error):
    stats = _stats_with_titles()
    snapshot = stats["favourites"]
    before = copy.deepcopy(stats)
    session = object()
    fetch = AsyncMock(side_effect=error)
    monkeypatch.setattr("favourites.fetch_favourites", fetch)

    with pytest.raises(type(error)) as raised:
        await fmod._collect_favourites(session, stats)

    assert raised.value is error
    fetch.assert_awaited_once_with(session)
    assert stats["favourites"] is snapshot
    assert stats == before


@pytest.mark.asyncio
async def test_failed_enrichment_does_not_publish_partial_favourites():
    stats = _stats_with_titles()
    snapshot = stats["favourites"]
    before = copy.deepcopy(stats)
    response = {"animes": [{"id": 226}], "people": [None]}

    with pytest.raises(AttributeError):
        await fmod._collect_favourites(None, stats, fav=response)

    assert stats["favourites"] is snapshot
    assert stats == before


@pytest.mark.asyncio
async def test_comments_are_excluded_from_favourites_and_report():
    marker = "COMMENT_MARKER_137 </script> & <b>hostile</b>"
    stats = _stats_with_titles()
    stats["anime"]["titles"]["226"]["comment"] = marker

    result = await fmod._collect_favourites(None, stats, fav={"animes": [{"id": 226}]})
    report = fmod.build_favourites_messages(result)

    assert marker not in json.dumps(result["favourites"], ensure_ascii=False)
    assert "COMMENT_MARKER_137" not in "".join(rendered_html(report))
    assert stats["anime"]["titles"]["226"]["comment"] == marker


@pytest.mark.parametrize("stats", [{}, {"favourites": None}, storage._empty_stats_all()])
def test_empty_favourites_return_typed_empty_state(stats):
    report = fmod.build_favourites_messages(stats)

    assert isinstance(report, Report)
    assert len(report.units) == 1
    assert report.units[0].sections[1].items[0].parts == (Italic("Список избранного пока пуст."),)


def test_report_preserves_all_category_and_item_order_without_mutation(monkeypatch):
    stats = {"favourites": {
        "people": [{"title": "Person", "url": ""}],
        "characters": [{"title": "Character", "url": ""}],
        "ranobe": [{"title": "Novel", "url": ""}],
        "manga": [{"title": "Manga", "url": ""}],
        "anime": [{"title": "Second", "url": ""}, {"title": "First", "url": ""}],
    }}
    before = copy.deepcopy(stats)
    fetch = AsyncMock(side_effect=AssertionError("I/O отчёта"))
    monkeypatch.setattr("favourites.fetch_favourites", fetch)

    report = fmod.build_favourites_messages(stats)

    sections = report.units[0].sections
    assert sections[0].items[1].parts == (Italic("6 объектов  ·  5 категорий"),)
    assert [block.items[0].parts[1].value for block in sections[1:]] == [
        "Аниме", "Манга", "Ранобэ", "Персонажи", "Люди индустрии",
    ]
    assert [row.parts[1].value for row in sections[1].items[1:]] == ["Second", "First"]
    assert stats == before
    fetch.assert_not_called()
