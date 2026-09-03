# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Тесты модуля stats: агрегация (recompute_aggregates), сбор избранного
(_collect_favourites), фильтр мусора по kind, metadata-retry в sync_stats_all,
и smoke-тесты билдеров отчётов.

Форматтеры отчётов (messages._fmt_mono_rows / _top_block / _score_dist_block
и т.п.) тестируются здесь же — низкоуровневые хелперы рендера отчётов stats,
живут рядом со своими потребителями.

Дисциплина: падает на непропатченном, проходит на пропатченном.
"""

import copy
import json
import logging
import re
from datetime import datetime, timedelta
from unittest.mock import (
    AsyncMock,
    call,
)

import pytest

import handlers
import messages
import shiki_api
import stats as smod
import storage
import utils


def _manga_record(title, kind, status="completed", chapters_read=1):
    return {
        "title": title, "title_en": title, "score": 0, "status": status,
        "rewatches": 0, "url": "", "kind": kind, "year": None,
        "shiki_score": None, "genres": [], "themes": [], "demographic": [],
        "chapters_read": chapters_read, "volumes_read": 0,
        "chapters_total": None, "volumes_total": None, "publishers": [],
    }


def _export_manga_row(tid, status="completed", chapters=1):
    return {"target_id": tid, "target_type": "Manga", "target_title": "x",
            "target_title_ru": "x", "score": 0, "status": status,
            "rewatches": 0, "chapters": chapters, "volumes": 0}


# ════════════════════════════════════════════════════════════════
#  Форматтеры
# ════════════════════════════════════════════════════════════════

def test_section_header():
    assert messages._section_header("🎬", "АНИМЕ") == "<b>━━━━━ 🎬 АНИМЕ ━━━━━</b>"

def test_fmt_mono_rows_empty():
    assert messages._fmt_mono_rows([]) == ""

def test_fmt_mono_rows_basic_alignment():
    out = messages._fmt_mono_rows([("Экшен", 66), ("Триллер", 45)])
    assert "<code>" in out and "</code>" in out
    assert "66" in out and "45" in out

def test_fmt_mono_rows_percent():
    out = messages._fmt_mono_rows([("Экшен", 50)], show_percent=True, total=100)
    assert "50%" in out

def test_fmt_mono_rows_percent_skipped_without_total():
    out = messages._fmt_mono_rows([("Экшен", 50)], show_percent=True, total=0)
    assert "%" not in out

def test_fmt_mono_rows_html_escape():
    out = messages._fmt_mono_rows([("A & B", 5)])
    assert "&amp;" in out

def test_top_block_empty_counter():
    assert messages._top_block("🎭", "Жанры", {}, 8) == []

def test_top_block_structure():
    block = messages._top_block("🎨", "Студии", {"MAPPA": 12, "Bones": 9}, 6)
    assert len(block) == 2
    assert "Студии" in block[0]
    assert "<code>" in block[1]

def test_score_dist_block_empty():
    assert messages._score_dist_block({}) == []

def test_score_dist_block_star_marker_and_order():
    # score_dist: оценка -> сколько раз; нули (без оценки) игнорируются
    block = messages._score_dist_block({"10": 5, "9": 8, "0": 3})
    body = block[1]
    assert "★10" in body and "★9" in body
    assert "★0" not in body  # нулевая оценка не показывается
    # порядок по убыванию: ★10 раньше ★9
    assert body.index("★10") < body.index("★9")

def test_fmt_kinds_order_and_skip_zero():
    out = messages._fmt_kinds({"tv": 61, "movie": 36, "ova": 0}, smod._KIND_RU_ANIME)
    assert "Сериалы 61" in out and "Фильмы 36" in out
    assert "OVA" not in out  # ноль пропускается

def test_avg_score_from_dist():
    # (10*1 + 8*2) / 3 = 8.67
    assert messages._avg_score_from_dist({"10": 1, "8": 2}) == pytest.approx(8.67, abs=0.01)

def test_avg_score_from_dist_ignores_zero():
    # нули (без оценки) не участвуют
    assert messages._avg_score_from_dist({"8": 1, "0": 100}) == 8.0

def test_avg_score_from_dist_empty():
    assert messages._avg_score_from_dist({}) is None


# ════════════════════════════════════════════════════════════════
#  recompute_aggregates
# ════════════════════════════════════════════════════════════════

def _anime_rec(**kw):
    base = dict(status="completed", score=8, shiki_score=7.5, kind="tv", year=2013,
                genres=["Экшен"], themes=["Школа"], demographic=["Сёнэн"],
                studios=["MAPPA"], origin="Манга", rating="R-17",
                episodes_watched=24, duration=24)
    base.update(kw)
    return base

def test_aggregates_status_counters():
    titles = {
        "1": _anime_rec(status="completed"),
        "2": _anime_rec(status="dropped"),
        "3": _anime_rec(status="planned"),
        "4": _anime_rec(status="watching"),
    }
    agg = smod.recompute_aggregates("anime", titles)
    assert agg["total_completed"] == 1
    assert agg["total_dropped"] == 1
    assert agg["total_planned"] == 1
    assert agg["total_watching"] == 1

def test_aggregates_only_completed_counted_in_genres():
    # жанры считаются ТОЛЬКО по completed
    titles = {
        "1": _anime_rec(status="completed", genres=["Экшен"]),
        "2": _anime_rec(status="dropped", genres=["Драма"]),
    }
    agg = smod.recompute_aggregates("anime", titles)
    assert agg["genres"].get("Экшен") == 1
    assert "Драма" not in agg["genres"]  # дроп не учитывается в жанрах

def test_aggregates_score_dist():
    titles = {
        "1": _anime_rec(score=9),
        "2": _anime_rec(score=9),
        "3": _anime_rec(score=7),
    }
    agg = smod.recompute_aggregates("anime", titles)
    # Ключи score_dist — строки (для JSON-совместимости)
    assert agg["score_dist"].get("9") == 2
    assert agg["score_dist"].get("7") == 1

def test_aggregates_avg_shiki_only_with_personal_score():
    # shiki берётся только если есть личная оценка
    titles = {
        "1": _anime_rec(score=8, shiki_score=7.0),
        "2": _anime_rec(score=0, shiki_score=9.0),  # без личной оценки -> shiki не учитывается
    }
    agg = smod.recompute_aggregates("anime", titles)
    assert agg["avg_shiki_completed"] == 7.0

def test_aggregates_episodes_and_hours():
    titles = {"1": _anime_rec(episodes_watched=25, duration=24)}  # 600 мин = 10 ч
    agg = smod.recompute_aggregates("anime", titles)
    assert agg["total_episodes_watched"] == 25
    assert agg["total_hours_watched"] == 10.0

def test_aggregates_manga_chapters():
    titles = {"1": dict(status="completed", score=8, kind="manga",
                        genres=[], themes=[], demographic=[], publishers=["Young Ace"],
                        chapters_read=100, volumes_read=10)}
    agg = smod.recompute_aggregates("manga", titles)
    assert agg["total_chapters_read"] == 100
    assert agg["total_volumes_read"] == 10
    assert agg["publishers"].get("Young Ace") == 1


# ════════════════════════════════════════════════════════════════
#  /pick: classifier, planned-каталог и чистые selector-контракты
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("manga", smod.PICK_CATEGORY_MANGA),
        ("manhwa", smod.PICK_CATEGORY_MANGA),
        ("manhua", smod.PICK_CATEGORY_MANGA),
        ("light_novel", smod.PICK_CATEGORY_RANOBE),
        ("novel", smod.PICK_CATEGORY_RANOBE),
        ("ranobe", smod.PICK_CATEGORY_RANOBE),
        (None, smod.PICK_CATEGORY_UNKNOWN),
        (123, smod.PICK_CATEGORY_UNKNOWN),
        ("", smod.PICK_CATEGORY_UNKNOWN),
        ("future_book_kind", smod.PICK_CATEGORY_UNKNOWN),
    ],
)
def test_manga_presentation_classifier_is_tri_state_without_guessing(kind, expected):
    assert smod.classify_manga_presentation_kind(kind) == expected


def _pick_record(
    title,
    *,
    status="planned",
    release_status=None,
    kind="tv",
    year=2011,
    genres=None,
):
    return {
        "title": title,
        "status": status,
        "release_status": release_status,
        "kind": kind,
        "url": f"https://shikimori.one/animes/{title}",
        "year": year,
        "genres": ["Экшен"] if genres is None else genres,
    }


def _pick_candidate(candidate_id, *, year=2011, genres=("Экшен",)):
    return smod.PickCandidate(
        id=candidate_id,
        category=smod.PICK_CATEGORY_ANIME,
        title=candidate_id,
        url=f"/animes/{candidate_id}",
        year=year,
        genres=genres,
    )


def test_pick_catalog_contains_only_planned_and_discloses_unknown_manga_records():
    stats = storage._empty_stats_all()
    stats["updated_at"] = "2026-09-01T10:00:00+00:00"
    stats["anime"]["titles"] = {
        "1": _pick_record("anime", kind="future_anime_kind"),
        "2": _pick_record("completed", status="completed"),
        "3": _pick_record("uppercase", status="PLANNED"),
    }
    stats["manga"]["titles"] = {
        "4": _pick_record("manga", kind="manga"),
        "5": _pick_record("ranobe", kind="light_novel"),
        "6": _pick_record("unknown", kind="future_book_kind"),
        "7": _pick_record("missing-kind", kind=None),
    }

    catalog = smod.build_pick_catalog(stats)

    assert catalog is not None
    assert [candidate.id for candidate in catalog.anime] == ["1"]
    assert [candidate.id for candidate in catalog.manga] == ["4"]
    assert [candidate.id for candidate in catalog.ranobe] == ["5"]
    assert catalog.unresolved_count == 2
    assert catalog.updated_at == "2026-09-01T10:00:00+00:00"
    all_ids = {
        candidate.id
        for pool in (catalog.anime, catalog.manga, catalog.ranobe)
        for candidate in pool
    }
    assert all_ids == {"1", "4", "5"}


def test_pick_catalog_excludes_only_explicitly_announced_titles():
    stats = storage._empty_stats_all()
    stats["anime"]["titles"] = {
        "1": _pick_record("released", release_status="released"),
        "2": _pick_record("ongoing", release_status="ongoing"),
        "3": _pick_record("announced", release_status="anons"),
        "4": _pick_record("legacy"),
        "5": _pick_record("future-status", release_status="future_status"),
    }
    stats["manga"]["titles"] = {
        "6": _pick_record("manga-announced", release_status=" ANONS ", kind="manga"),
        "7": _pick_record(
            "unknown-announced",
            release_status="anons",
            kind="future_book_kind",
        ),
    }

    catalog = smod.build_pick_catalog(stats)

    assert catalog is not None
    assert [candidate.id for candidate in catalog.anime] == ["1", "2", "4", "5"]
    assert catalog.manga == ()
    assert catalog.ranobe == ()
    assert catalog.unresolved_count == 0


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"anime": {}, "manga": {"titles": {}}},
        {"anime": {"titles": []}, "manga": {"titles": {}}},
    ],
)
def test_pick_catalog_rejects_structurally_unusable_snapshot(payload):
    assert smod.build_pick_catalog(payload) is None


def test_pick_catalog_normalizes_missing_optional_fields_without_crashing():
    stats = storage._empty_stats_all()
    stats["anime"]["titles"] = {
        "1": {"status": "planned", "genres": ["Экшен", None, "экшен", ""]},
    }

    catalog = smod.build_pick_catalog(stats)

    assert catalog is not None
    assert catalog.anime == (
        smod.PickCandidate(
            id="1",
            category=smod.PICK_CATEGORY_ANIME,
            title="Без названия",
            url="",
            year=None,
            genres=("Экшен",),
        ),
    )


def test_ordinary_pick_is_uniform_over_unseen_pool_and_resets_after_exhaustion(
    monkeypatch,
):
    candidates = tuple(_pick_candidate(str(index)) for index in range(1, 4))
    offered: list[tuple[str, ...]] = []

    def choose(items):
        offered.append(tuple(candidate.id for candidate in items))
        return items[0]

    monkeypatch.setattr(smod.random, "choice", choose)
    shown = frozenset()
    selected = []
    resets = []
    for _ in range(4):
        result = smod.select_pick_candidate(candidates, shown)
        selected.append(result.candidate.id)
        resets.append(result.pool_reset)
        shown = result.shown_ids

    assert selected == ["1", "2", "3", "1"]
    assert offered == [("1", "2", "3"), ("2", "3"), ("3",), ("1", "2", "3")]
    assert resets == [False, False, False, True]
    assert shown == frozenset({"1"})


def test_contrast_pick_prefers_other_decade_then_minimum_genre_overlap(
    monkeypatch,
):
    anchor = _pick_candidate("anchor", year=2011, genres=("Экшен", "Драма"))
    candidates = (
        anchor,
        _pick_candidate("same-zero", year=2015, genres=("Комедия",)),
        _pick_candidate("other-two", year=1998, genres=("Экшен", "Драма")),
        _pick_candidate("other-zero", year=1987, genres=("Комедия",)),
        _pick_candidate("other-zero-2", year=1977, genres=("Музыка",)),
        _pick_candidate("other-missing-genres", year=1967, genres=()),
        _pick_candidate("missing-year", year=None, genres=("Комедия",)),
    )
    offered = []

    def choose(items):
        offered.append(tuple(candidate.id for candidate in items))
        return items[0]

    monkeypatch.setattr(smod.random, "choice", choose)

    result = smod.select_contrast_pick_candidate(candidates, anchor, {anchor.id})

    assert result.candidate.id == "other-zero"
    assert offered == [("other-zero", "other-zero-2")]


def test_contrast_pick_weakens_missing_metadata_and_handles_one_item(monkeypatch):
    anchor = _pick_candidate("only", year=None, genres=())
    offered = []

    def choose(items):
        offered.append(tuple(items))
        return items[0]

    monkeypatch.setattr(smod.random, "choice", choose)

    result = smod.select_contrast_pick_candidate((anchor,), anchor, {anchor.id})

    assert result.candidate == anchor
    assert result.pool_reset is True
    assert result.shown_ids == frozenset({anchor.id})
    assert offered == [(anchor,)]


def test_metadata_refresh_selection_uses_thresholds_age_and_stable_ties():
    now = datetime(2026, 9, 2, 12, 0, 0)
    rows = {
        "10": {"status": "watching"},
        "2": {"status": "planned"},
        "3": {"status": "completed"},
        "4": {"status": "dropped"},
        "5": {"status": "rewatching"},
        "6": {"status": "on_hold"},
        "7": {"status": "planned"},
        "8": {"status": "completed"},
    }
    titles = {
        "10": {"kind": "tv", "meta_updated_at": (now - timedelta(days=7)).isoformat()},
        "2": {"kind": "tv", "meta_updated_at": (now - timedelta(days=7)).isoformat()},
        "3": {"kind": "tv", "meta_updated_at": (now - timedelta(days=30)).isoformat()},
        "4": {"kind": "tv", "meta_updated_at": (now - timedelta(days=29)).isoformat()},
        "5": {"kind": "tv", "meta_updated_at": (now - timedelta(days=6)).isoformat()},
        "6": {"kind": "tv"},
        "7": {"kind": ""},
        "8": {"kind": "tv", "meta_updated_at": "повреждено"},
    }

    selected = smod._select_metadata_refresh_ids(rows, titles, now)

    # Нет/битая метка старее любой валидной; равный возраст разрешается по
    # числовому ID. Свежие active/terminal и missing-kind сюда не попадают.
    assert selected == ["6", "8", "3", "2", "10"]


def test_metadata_refresh_selection_is_limited_to_fifty_per_domain():
    now = datetime(2026, 9, 2, 12, 0, 0)
    rows = {str(title_id): {"status": "planned"} for title_id in range(60, 0, -1)}
    titles = {title_id: {"kind": "tv"} for title_id in rows}

    selected = smod._select_metadata_refresh_ids(rows, titles, now)

    assert selected == [str(title_id) for title_id in range(1, 51)]


def test_metadata_refresh_selection_treats_future_timestamp_as_stale():
    now = datetime(2026, 9, 2, 12, 0, 0)
    rows = {"1": {"status": "planned"}}
    titles = {
        "1": {
            "kind": "tv",
            "meta_updated_at": (now + timedelta(days=365)).isoformat(),
        },
    }

    assert smod._select_metadata_refresh_ids(rows, titles, now) == ["1"]


# ════════════════════════════════════════════════════════════════
#  Регрессия: фильтр нерелевантных kind в sync_stats_all
# ════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_stats_all_filters_special_before_aggregating(monkeypatch):
    """Спецвыпуск не попадает в titles и не раздувает счётчик студии."""
    stats = storage._empty_stats_all()
    export_anime = [
        {
            "target_id": tid,
            "target_type": "Anime",
            "target_title": f"Title {tid}",
            "target_title_ru": f"Тайтл {tid}",
            "score": 8,
            "status": "completed",
            "rewatches": 0,
            "episodes": 1,
        }
        for tid in (1, 2, 3)
    ]
    metadata = {
        "1": {
            "kind": "tv",
            "poster_url": "https://cdn.example/1.jpg",
            "release_status": "released",
            "studios": ["Studio Deen"],
        },
        "2": {"kind": "ova", "studios": ["Studio Deen"]},
        "3": {"kind": "special", "studios": ["Studio Deen"]},
    }

    async def fake_export(session, media):
        return export_anime if media == "anime" else []

    async def fake_meta(media, ids, session=None):
        assert media == "anime"
        assert ids == ["1", "2", "3"]
        return {tid: metadata[tid] for tid in ids}

    async def fake_collect(session, current, fav=None):
        return current

    saved = []
    now = datetime(2026, 9, 2, 12, 0, 0)
    monkeypatch.setattr("stats.load_stats_all", lambda use_cache=False: stats)
    monkeypatch.setattr("stats._utcnow", lambda: now)
    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", fake_meta)
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr("stats.save_stats_all", lambda data: saved.append(data))

    result, ok = await smod.sync_stats_all(session=object())

    assert ok is True
    assert set(result["anime"]["titles"]) == {"1", "2"}
    assert result["anime"]["titles"]["1"]["poster_url"] == (
        "https://cdn.example/1.jpg"
    )
    assert result["anime"]["titles"]["1"]["release_status"] == "released"
    assert result["anime"]["titles"]["1"]["meta_updated_at"] == now.isoformat()
    assert result["anime"]["titles"]["2"]["meta_updated_at"] == now.isoformat()
    assert result["anime"]["aggregates"]["studios"] == {"Studio Deen": 2}
    assert saved == [result]


# ════════════════════════════════════════════════════════════════
#  Избранное: _collect_favourites (джойн с titles)
# ════════════════════════════════════════════════════════════════

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
    monkeypatch.setattr("stats.fetch_favourites", fake_fetch)

    class S:
        pass
    stats = await smod._collect_favourites(S(), stats)
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
    monkeypatch.setattr("stats.fetch_favourites", fake_fetch)

    class S: 
        pass
    stats = await smod._collect_favourites(S(), stats)
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

    monkeypatch.setattr("stats.fetch_favourites", fake_fetch)

    class S:
        pass
    result = await smod._collect_favourites(S(), stats, fav=None)

    assert fetched is False       # повторного фетча не было
    assert result["favourites"]["anime"] == [{"id": "1", "title": "Старое", "url": "/animes/1"}]


# ════════════════════════════════════════════════════════════════
#  SMOKE-тесты билдеров (поймали бы оба прод-бага)
#    1. build_stats_all_messages undefined после ручного мержа
#    2. двойной домен в ссылках
# ════════════════════════════════════════════════════════════════

def _populated_stats():
    stats = storage._empty_stats_all()
    stats["anime"]["aggregates"] = smod.recompute_aggregates("anime", {
        "1": _anime_rec(score=9, year=2013),
    })
    stats["manga"]["aggregates"] = smod.recompute_aggregates("manga", {})
    stats["favourites"]["anime"] = [
        {"id": "1", "title": "Эрго Прокси", "url": "/animes/790", "score": 9}
    ]
    return stats

def test_smoke_build_stats_all_returns_list():
    msgs = smod.build_stats_all_messages(_populated_stats())
    assert isinstance(msgs, list)
    assert all(isinstance(m, str) for m in msgs)
    assert msgs  # непустой


def test_stats_all_normalizes_aware_updated_at_to_utc_date():
    stats = _populated_stats()
    stats["updated_at"] = "2026-08-06T01:30:00+03:00"

    message = smod.build_stats_all_messages(stats)[0]

    assert "актуально на 05.08.2026" in message
    assert "актуально на 06.08.2026" not in message


@pytest.mark.parametrize("kind", ["light_novel", "ranobe"])
def test_stats_all_translates_ranobe_kinds(kind):
    stats = _populated_stats()
    stats["manga"]["aggregates"].update({
        "total_completed": 4,
        "kinds": {"manga": 1, kind: 3},
    })

    manga_message = smod.build_stats_all_messages(stats)[1]

    assert "Манга" in manga_message
    assert "Ранобэ" in manga_message
    assert "light_novel" not in manga_message
    assert "ranobe" not in manga_message

def test_smoke_build_favourites_returns_list():
    msgs = smod.build_favourites_messages(_populated_stats())
    assert isinstance(msgs, list) and all(isinstance(m, str) for m in msgs)

def test_smoke_build_current_returns_list():
    cur = {"period": "2026-Q2", "period_start": "2026-04-01T00:00:00",
           "tracking_since": "2026-04-01T00:00:00", "events": []}
    msgs = smod.build_current_stats_messages(cur, _populated_stats())
    assert isinstance(msgs, list) and all(isinstance(m, str) for m in msgs)


def test_current_and_quarterly_reports_keep_distinct_structure():
    cur = {"period": "2026-Q2", "period_start": "2026-04-01T00:00:00",
           "tracking_since": "2026-04-01T00:00:00", "events": []}

    current = smod.build_current_stats_messages(cur, _populated_stats())
    quarterly = smod.build_quarterly_report_messages(
        cur,
        _populated_stats(),
        {"period": "2026-Q1", "anime_completed": 1, "manga_completed": 0},
    )

    assert len(current) == 2
    assert "КВАРТАЛЬНЫЙ ОТЧЁТ" not in current[0]
    assert len(quarterly) == 3
    assert "КВАРТАЛЬНЫЙ ОТЧЁТ" in quarterly[0]
    assert "Сравнение" in quarterly[2]


def test_quarter_report_links_contain_canonical_domain_once():
    stats = _populated_stats()
    stats["anime"]["titles"] = {
        "1": {
            **_anime_rec(score=9),
            "title": "Эрго Прокси",
            "url": "https://shikimori.io/animes/790-ergo-proxy",
        },
    }
    cur = {
        "period": "2026-Q2",
        "period_start": "2026-04-01T00:00:00",
        "tracking_since": "2026-04-01T00:00:00",
        "events": [{
            "id": "1",
            "media": "anime",
            "event": "completed",
            "score": 9,
        }],
    }

    reports = (
        smod.build_current_stats_messages(cur, stats),
        smod.build_quarterly_report_messages(cur, stats, prev_quarter=None),
    )

    for messages_list in reports:
        assert isinstance(messages_list, list)
        assert all(isinstance(message, str) for message in messages_list)
        hrefs = re.findall(r'href="([^"]*)"', "\n".join(messages_list))
        assert hrefs == [
            "https://shikimori.io/animes/790-ergo-proxy",
        ]


def test_prepare_quarter_report_collects_each_media_and_event_type():
    anime_completed = {"title": "Anime completed", "score": 2}
    anime_dropped = {"title": "Anime dropped", "score": 0}
    manga_completed = {"title": "Manga completed", "score": 3}
    manga_dropped = {"title": "Manga dropped", "score": 0}
    stats = {
        "anime": {"titles": {
            "a-completed": anime_completed,
            "a-dropped": anime_dropped,
        }},
        "manga": {"titles": {
            "m-completed": manga_completed,
            "m-dropped": manga_dropped,
        }},
    }
    cur = {"events": [
        {"id": "a-completed", "media": "anime", "event": "completed", "score": 9},
        {"id": "a-dropped", "media": "anime", "event": "dropped"},
        {"media": "anime", "event": "planned"},
        {"id": "m-completed", "media": "manga", "event": "completed", "score": 8},
        {"id": "m-dropped", "media": "manga", "event": "dropped"},
        {"media": "manga", "event": "planned"},
        None,
        "broken",
        {},
    ]}

    report = smod._prepare_quarter_report(cur, stats)

    assert report == {
        "anime": {
            "completed": [{"title": "Anime completed", "score": 9}],
            "dropped": [{"title": "Anime dropped", "score": 0}],
            "planned": 1,
        },
        "manga": {
            "completed": [{"title": "Manga completed", "score": 8}],
            "dropped": [{"title": "Manga dropped", "score": 0}],
            "planned": 1,
        },
    }


@pytest.mark.parametrize("invalid_events", [
    None,
    "broken",
    {"media": "anime", "event": "planned"},
    42,
    True,
])
def test_quarter_reports_treat_non_list_events_as_empty(invalid_events):
    cur = {"period": "2026-Q2", "period_start": "2026-04-01T00:00:00",
           "tracking_since": "2026-04-01T00:00:00", "events": invalid_events}

    prepared = smod._prepare_quarter_report(cur, _populated_stats())
    current = smod.build_current_stats_messages(cur, _populated_stats())
    quarterly = smod.build_quarterly_report_messages(
        cur, _populated_stats(), prev_quarter=None,
    )

    assert prepared == {
        "anime": {"completed": [], "dropped": [], "planned": 0},
        "manga": {"completed": [], "dropped": [], "planned": 0},
    }
    assert len(current) == 2
    assert len(quarterly) == 2
    assert all(isinstance(message, str) for message in current + quarterly)


def test_quarter_reports_normalize_event_id_and_score_fields():
    stats = _populated_stats()
    stats["anime"]["titles"] = {
        "1": {
            **_anime_rec(score=2),
            "title": "Valid completed",
            "url": "/animes/1",
        },
    }
    cur = {
        "period": "2026-Q2",
        "period_start": "2026-04-01T00:00:00",
        "tracking_since": "2026-04-01T00:00:00",
        "events": [
            {
                "id": ["1"],
                "media": "anime",
                "event": "completed",
                "score": 10,
            },
            {
                "id": 1,
                "media": "anime",
                "event": "completed",
                "score": "9",
            },
        ],
    }

    prepared = smod._prepare_quarter_report(cur, stats)
    reports = (
        smod.build_current_stats_messages(cur, stats),
        smod.build_quarterly_report_messages(cur, stats, prev_quarter=None),
    )

    assert prepared["anime"]["completed"] == [{
        **stats["anime"]["titles"]["1"],
        "score": 9,
    }]
    for messages_list in reports:
        assert isinstance(messages_list, list)
        assert all(isinstance(message, str) for message in messages_list)
        assert "Средняя оценка: <b>9.0</b>" in messages_list[0]

def test_smoke_empty_stats_no_crash():
    # Пустая структура не должна ронять билдеры
    empty = storage._empty_stats_all()
    assert smod.build_stats_all_messages(empty)
    assert smod.build_favourites_messages(empty)

@pytest.mark.asyncio
async def test_smoke_async_report_builders(monkeypatch):
    monkeypatch.setattr("handlers.load_stats_all", lambda: _populated_stats())
    monkeypatch.setattr("handlers.load_stats_current", lambda: {
        "period": "2026-Q2", "period_start": "2026-04-01T00:00:00",
        "tracking_since": "2026-04-01T00:00:00", "events": []})
    for builder in (handlers._stats_report_all, handlers._stats_report_current,
                    handlers._stats_report_favourites):
        msgs = await builder()
        assert isinstance(msgs, list) and all(isinstance(m, str) for m in msgs)


# ── Регрессия: ссылки содержат домен РОВНО один раз (нет двойного домена) ──

def test_links_single_domain_in_favourites():
    stats = storage._empty_stats_all()
    # Полный URL из GraphQL — провокация двойного домена
    stats["favourites"]["anime"] = [
        {"id": "1", "title": "Тест", "url": "https://shikimori.io/animes/226", "score": 10}
    ]
    msg = smod.build_favourites_messages(stats)[0]
    # Домен должен встречаться ровно один раз в href
    hrefs = re.findall(r'href="([^"]*)"', msg)
    assert hrefs, "должна быть ссылка"
    for href in hrefs:
        assert href.count("shikimori.io") == 1, f"двойной домен: {href}"
        assert href.startswith("https://shikimori.io/"), href


# ════════════════════════════════════════════════════════════════
#  favourites-fix (unit 2): metadata-retry в sync_stats_all
#  Битая мета (пустой kind) дозапрашивается, ваншот пересобирается и
#  вычищается самоочисткой (43→39 в проде). Полностью повторный пустой ответ
#  остаётся no-op; впервые полученный release_status сохраняется отдельно.
# ════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_refreshes_legacy_planned_record_for_pick(monkeypatch):
    stats = storage._empty_stats_all()
    stats["manga"]["titles"] = {
        "111": _manga_record(
            "Запланированная манга",
            "manga",
            status="planned",
            chapters_read=0,
        ),
    }
    monkeypatch.setattr(
        "stats.load_stats_all",
        lambda *args, **kwargs: copy.deepcopy(stats),
    )

    async def fake_export(session, media):
        if media == "manga":
            return [_export_manga_row("111", status="planned", chapters=0)]
        return []

    calls = []

    async def fake_meta(media, ids, session=None):
        calls.append((media, list(ids)))
        return {
            "111": {
                "url": "/mangas/111",
                "poster_url": "https://cdn.example/new.jpg",
                "kind": "manga",
                "release_status": "released",
                "year": 2026,
                "shiki_score": 7.5,
                "genres": ["Драма"],
                "themes": [],
                "demographic": [],
                "chapters_total": 20,
                "volumes_total": 3,
                "publishers": ["Example"],
            },
        }

    async def fake_collect(session, value, fav=None):
        return value

    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", fake_meta)
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr("stats.save_stats_all", lambda value: None)
    now = datetime(2026, 9, 2, 12, 0, 0)
    monkeypatch.setattr("stats._utcnow", lambda: now)

    result, ok = await smod.sync_stats_all(session=object())

    assert ok is True
    assert calls == [("manga", ["111"])]
    refreshed = result["manga"]["titles"]["111"]
    assert refreshed["meta_updated_at"] == now.isoformat()
    assert refreshed["poster_url"] == "https://cdn.example/new.jpg"
    assert refreshed["release_status"] == "released"
    catalog = smod.build_pick_catalog(result)
    assert catalog is not None
    assert [candidate.id for candidate in catalog.manga] == ["111"]


@pytest.mark.asyncio
async def test_sync_keeps_correctness_fetches_outside_maintenance_limit(monkeypatch):
    stats = storage._empty_stats_all()
    stats["anime"]["titles"] = {
        str(title_id): _anime_rec(status="planned")
        for title_id in range(1, 61)
    }
    stats["anime"]["titles"]["900"] = _anime_rec(
        status="planned",
        kind="",
    )
    stats["manga"]["titles"] = {
        str(title_id): _manga_record(
            f"Манга {title_id}",
            "manga",
            status="planned",
            chapters_read=0,
        )
        for title_id in range(1001, 1052)
    }

    anime_rows = [
        {
            "target_id": title_id,
            "target_title": f"Anime {title_id}",
            "target_title_ru": f"Аниме {title_id}",
            "score": 0,
            "status": "planned",
            "rewatches": 0,
            "episodes": 0,
        }
        for title_id in range(1, 61)
    ]
    anime_rows.extend([
        {
            "target_id": 900,
            "target_title": "Broken",
            "target_title_ru": "Битая мета",
            "score": 0,
            "status": "planned",
            "rewatches": 0,
            "episodes": 0,
        },
        {
            "target_id": 901,
            "target_title": "New",
            "target_title_ru": "Новая запись",
            "score": 0,
            "status": "planned",
            "rewatches": 0,
            "episodes": 0,
        },
    ])
    manga_rows = [
        _export_manga_row(str(title_id), status="planned", chapters=0)
        for title_id in range(1001, 1052)
    ]

    async def fake_export(session, media):
        return anime_rows if media == "anime" else manga_rows

    calls = []

    async def fake_meta(media, ids, session=None):
        calls.append((media, list(ids)))
        kind = "tv" if media == "anime" else "manga"
        prefix = "animes" if media == "anime" else "mangas"
        return {
            title_id: {
                "kind": kind,
                "url": f"/{prefix}/{title_id}",
                "release_status": "released",
            }
            for title_id in ids
        }

    async def fake_collect(session, value, fav=None):
        return value

    now = datetime(2026, 9, 2, 12, 0, 0)
    monkeypatch.setattr("stats.load_stats_all", lambda **kwargs: copy.deepcopy(stats))
    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", fake_meta)
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr("stats.save_stats_all", lambda value: None)
    monkeypatch.setattr("stats._utcnow", lambda: now)

    result, ok = await smod.sync_stats_all(session=object())

    assert ok is True
    assert calls == [
        ("anime", ["901", "900"]),
        ("anime", [str(title_id) for title_id in range(1, 51)]),
        ("manga", [str(title_id) for title_id in range(1001, 1051)]),
    ]
    anime_titles = result["anime"]["titles"]
    manga_titles = result["manga"]["titles"]
    assert anime_titles["900"]["meta_updated_at"] == now.isoformat()
    assert anime_titles["901"]["meta_updated_at"] == now.isoformat()
    assert sum("meta_updated_at" in anime_titles[str(i)] for i in range(1, 61)) == 50
    assert sum("meta_updated_at" in manga_titles[str(i)] for i in range(1001, 1052)) == 50


@pytest.mark.asyncio
async def test_sync_refreshes_full_metadata_aggregates_and_preserves_user_state(
    monkeypatch,
):
    now = datetime(2026, 9, 2, 12, 0, 0)
    old_stamp = (now - timedelta(days=31)).isoformat()
    stats = storage._empty_stats_all()
    anime = _anime_rec(
        score=4,
        rewatches=1,
        episodes_watched=2,
        meta_updated_at=old_stamp,
    )
    anime.update({
        "title": "Старое аниме",
        "title_en": "Old anime",
        "url": "/animes/1",
        "poster_url": "https://cdn.example/old-anime.jpg",
        "release_status": "ongoing",
    })
    manga = _manga_record("Старая манга", "manga")
    manga.update({
        "score": 3,
        "rewatches": 1,
        "volumes_read": 1,
        "meta_updated_at": old_stamp,
        "poster_url": "https://cdn.example/old-manga.jpg",
        "release_status": "ongoing",
    })
    stats["anime"]["titles"] = {"1": anime}
    stats["manga"]["titles"] = {"2": manga}

    exports = {
        "anime": [{
            "target_id": 1,
            "target_title": "Current anime",
            "target_title_ru": "Актуальное аниме",
            "score": 9,
            "status": "completed",
            "rewatches": 4,
            "episodes": 11,
        }],
        "manga": [{
            "target_id": 2,
            "target_title": "Current manga",
            "target_title_ru": "Актуальная манга",
            "score": 8,
            "status": "completed",
            "rewatches": 3,
            "chapters": 22,
            "volumes": 5,
        }],
    }
    metadata = {
        "anime": {
            "1": {
                "url": "/animes/1-current",
                "poster_url": "https://cdn.example/new-anime.jpg",
                "kind": "movie",
                "release_status": "released",
                "year": 2025,
                "shiki_score": 8.8,
                "genres": ["Драма"],
                "themes": ["Взросление"],
                "demographic": ["Сэйнэн"],
                "episodes_total": 1,
                "duration": 120,
                "rating": "R-17",
                "origin": "Оригинал",
                "studios": ["New Studio"],
            },
        },
        "manga": {
            "2": {
                "url": "/mangas/2-current",
                "poster_url": "https://cdn.example/new-manga.jpg",
                "kind": "manhwa",
                "release_status": "released",
                "year": 2024,
                "shiki_score": 8.2,
                "genres": ["Триллер"],
                "themes": ["Выживание"],
                "demographic": ["Сэйнэн"],
                "chapters_total": 50,
                "volumes_total": 8,
                "publishers": ["New Publisher"],
            },
        },
    }

    async def fake_export(session, media):
        return exports[media]

    async def fake_meta(media, ids, session=None):
        assert ids == (["1"] if media == "anime" else ["2"])
        return metadata[media]

    async def fake_collect(session, value, fav=None):
        return value

    monkeypatch.setattr("stats.load_stats_all", lambda **kwargs: copy.deepcopy(stats))
    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", fake_meta)
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr("stats.save_stats_all", lambda value: None)
    monkeypatch.setattr("stats._utcnow", lambda: now)

    result, ok = await smod.sync_stats_all(session=object())

    assert ok is True
    refreshed_anime = result["anime"]["titles"]["1"]
    assert (
        refreshed_anime["score"],
        refreshed_anime["status"],
        refreshed_anime["episodes_watched"],
        refreshed_anime["rewatches"],
    ) == (9, "completed", 11, 4)
    assert refreshed_anime["poster_url"] == "https://cdn.example/new-anime.jpg"
    assert refreshed_anime["release_status"] == "released"
    assert refreshed_anime["meta_updated_at"] == now.isoformat()
    anime_aggregates = result["anime"]["aggregates"]
    assert anime_aggregates["by_year"] == {"2025": 1}
    assert anime_aggregates["genres"] == {"Драма": 1}
    assert anime_aggregates["themes"] == {"Взросление": 1}
    assert anime_aggregates["demographic"] == {"Сэйнэн": 1}
    assert anime_aggregates["studios"] == {"New Studio": 1}

    refreshed_manga = result["manga"]["titles"]["2"]
    assert (
        refreshed_manga["score"],
        refreshed_manga["status"],
        refreshed_manga["chapters_read"],
        refreshed_manga["volumes_read"],
        refreshed_manga["rewatches"],
    ) == (8, "completed", 22, 5, 3)
    assert refreshed_manga["poster_url"] == "https://cdn.example/new-manga.jpg"
    assert refreshed_manga["release_status"] == "released"
    assert refreshed_manga["meta_updated_at"] == now.isoformat()
    manga_aggregates = result["manga"]["aggregates"]
    assert manga_aggregates["by_year"] == {"2024": 1}
    assert manga_aggregates["genres"] == {"Триллер": 1}
    assert manga_aggregates["publishers"] == {"New Publisher": 1}


@pytest.mark.asyncio
async def test_partial_and_failed_maintenance_responses_preserve_retry_state(
    monkeypatch,
):
    now = datetime(2026, 9, 2, 12, 0, 0)
    old_stamp = (now - timedelta(days=8)).isoformat()
    state = storage._empty_stats_all()
    state["anime"]["titles"] = {
        title_id: {
            **_anime_rec(
                status="planned",
                score=0,
                rewatches=0,
                episodes_watched=0,
            ),
            "title": f"Аниме {title_id}",
            "title_en": f"Anime {title_id}",
            "url": f"/animes/{title_id}",
            "poster_url": f"https://cdn.example/old-{title_id}.jpg",
            "release_status": "anons",
            "meta_updated_at": old_stamp,
        }
        for title_id in ("1", "2")
    }
    rows = [{
        "target_id": int(title_id),
        "target_title": f"Anime {title_id}",
        "target_title_ru": f"Аниме {title_id}",
        "score": 0,
        "status": "planned",
        "rewatches": 0,
        "episodes": 0,
    } for title_id in ("1", "2")]

    async def fake_export(session, media):
        return rows if media == "anime" else []

    calls = []

    async def fake_meta(media, ids, session=None):
        calls.append(list(ids))
        if len(calls) == 1:
            return {
                "1": {
                    "kind": "tv",
                    "url": "/animes/1",
                    "poster_url": "https://cdn.example/new-1.jpg",
                },
            }
        if len(calls) == 2:
            return {}
        return {
            "2": {
                "kind": "tv",
                "url": "/animes/2",
                "poster_url": "https://cdn.example/new-2.jpg",
                "release_status": "released",
            },
        }

    async def fake_collect(session, value, fav=None):
        return value

    saves = []

    def save(value):
        nonlocal state
        state = copy.deepcopy(value)
        saves.append(copy.deepcopy(value))

    monkeypatch.setattr("stats.load_stats_all", lambda **kwargs: copy.deepcopy(state))
    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", fake_meta)
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr("stats.save_stats_all", save)
    monkeypatch.setattr("stats._utcnow", lambda: now)

    first, _ = await smod.sync_stats_all(session=object())
    assert first["anime"]["titles"]["1"]["meta_updated_at"] == now.isoformat()
    assert first["anime"]["titles"]["1"]["release_status"] == "anons"
    assert first["anime"]["titles"]["2"]["meta_updated_at"] == old_stamp
    assert first["anime"]["titles"]["2"]["poster_url"].endswith("old-2.jpg")

    second, _ = await smod.sync_stats_all(session=object())
    assert second["anime"]["titles"]["2"]["meta_updated_at"] == old_stamp
    assert second["anime"]["titles"]["2"]["poster_url"].endswith("old-2.jpg")

    third, _ = await smod.sync_stats_all(session=object())
    assert third["anime"]["titles"]["2"]["meta_updated_at"] == now.isoformat()
    assert third["anime"]["titles"]["2"]["poster_url"].endswith("new-2.jpg")
    assert calls == [["1", "2"], ["2"], ["2"]]
    assert len(saves) == 2


@pytest.mark.asyncio
async def test_sync_repairs_empty_kind_and_filters_oneshot(monkeypatch):
    # stats_all: один нормальный completed-тайтл + один с битой метой (пустой kind)
    stats = storage._empty_stats_all()
    stats["manga"]["titles"] = {
        "111":    _manga_record("Нормальная манга", "manga"),
        "120393": _manga_record("Elfen Lied Tokubetsu-hen", ""),   # битая мета
    }

    monkeypatch.setattr("stats.load_stats_all",
                        lambda *a, **k: copy.deepcopy(stats))

    async def fake_export(session, media):
        if media == "manga":
            return [_export_manga_row("111"), _export_manga_row("120393")]
        return []   # аниме пусто
    monkeypatch.setattr("stats.fetch_list_export", fake_export)

    # GraphQL теперь возвращает настоящий вид ваншота
    async def fake_meta(media, ids, session=None):
        if media == "manga" and "120393" in ids:
            return {"120393": {"kind": "one_shot", "url": "/mangas/120393",
                               "year": 2005}}
        return {}
    monkeypatch.setattr("stats.fetch_meta_batch", fake_meta)

    # избранное не трогаем (без сети)
    async def fake_collect(session, st, fav=None):
        return st
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    saved = {}
    monkeypatch.setattr("stats.save_stats_all", lambda data: saved.update(data))

    result, ok = await smod.sync_stats_all()

    assert ok is True
    titles = result["manga"]["titles"]
    # Ваншот починен (kind заполнен) и вычищен самоочисткой
    assert "120393" not in titles
    assert "111" in titles
    # «Прочитано» = 1, а не 2 (ваншот больше не считается)
    assert result["manga"]["aggregates"]["total_completed"] == 1


@pytest.mark.asyncio
async def test_sync_announced_empty_kind_stores_status_and_remains_retryable(
    monkeypatch,
):
    stats = storage._empty_stats_all()
    # planned-тайтл с пустым kind (анонс — вид ещё неизвестен)
    stats["manga"]["titles"] = {
        "999": _manga_record("Анонс", "", status="planned", chapters_read=0),
    }
    monkeypatch.setattr("stats.load_stats_all",
                        lambda *a, **k: copy.deepcopy(stats))

    async def fake_export(session, media):
        if media == "manga":
            return [_export_manga_row("999", status="planned", chapters=0)]
        return []
    monkeypatch.setattr("stats.fetch_list_export", fake_export)

    meta_calls = []

    async def fake_meta(media, ids, session=None):
        meta_calls.append((media, list(ids)))
        # Анонс: GraphQL вернул статус, но kind по-прежнему пустой.
        if media == "manga" and "999" in ids:
            return {
                "999": {
                    "kind": "",
                    "release_status": "anons",
                    "url": "",
                    "year": None,
                },
            }
        return {}
    monkeypatch.setattr("stats.fetch_meta_batch", fake_meta)

    async def fake_collect(session, st, fav=None):
        return st
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr("stats.save_stats_all", lambda *a, **k: None)

    result, ok = await smod.sync_stats_all()

    # retry БЫЛ предпринят для безвидового анонса (это и есть фикс Codacy)
    assert ("manga", ["999"]) in meta_calls
    # Запись цела, kind остался пустым (не выдумали вид), а точный статус
    # сохранён для чистой фильтрации /pick.
    assert "999" in result["manga"]["titles"]
    assert result["manga"]["titles"]["999"]["kind"] == ""
    assert result["manga"]["titles"]["999"]["release_status"] == "anons"
    catalog = smod.build_pick_catalog(result)
    assert catalog is not None
    assert catalog.manga == ()
    assert catalog.ranobe == ()
    assert catalog.unresolved_count == 0


@pytest.mark.asyncio
async def test_sync_kind_repair_preserves_known_release_status(monkeypatch):
    stats = storage._empty_stats_all()
    announced = _manga_record(
        "Анонс",
        "",
        status="planned",
        chapters_read=0,
    )
    announced["release_status"] = "anons"
    stats["manga"]["titles"] = {"999": announced}
    monkeypatch.setattr(
        "stats.load_stats_all",
        lambda *args, **kwargs: copy.deepcopy(stats),
    )

    async def fake_export(session, media):
        if media == "manga":
            return [_export_manga_row("999", status="planned", chapters=0)]
        return []

    async def fake_meta(media, ids, session=None):
        if media == "manga" and "999" in ids:
            return {
                "999": {
                    "kind": "manga",
                    "url": "/mangas/999",
                    "year": 2027,
                },
            }
        return {}

    collect_calls = []
    fav_marker = object()

    async def fake_collect(session, current, fav):
        collect_calls.append((session, current, fav))
        return current

    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", fake_meta)
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr("stats.save_stats_all", lambda *args, **kwargs: None)

    result, ok = await smod.sync_stats_all(fav=fav_marker)

    assert ok is True
    repaired = result["manga"]["titles"]["999"]
    assert repaired["kind"] == "manga"
    assert repaired["release_status"] == "anons"
    assert len(collect_calls) == 1
    assert collect_calls[0][2] is fav_marker
    catalog = smod.build_pick_catalog(result)
    assert catalog is not None
    assert catalog.manga == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken_value",
    [None, "secret scalar", ["secret list"]],
)
async def test_sync_recovers_non_dict_title_values_without_crashing(
    monkeypatch,
    caplog,
    broken_value,
):
    stats = storage._empty_stats_all()
    stats["anime"]["titles"] = {"101": broken_value}
    row = {
        "target_id": 101,
        "target_title": "Current title",
        "target_title_ru": "Актуальное название",
        "score": 9,
        "status": "watching",
        "rewatches": 2,
        "episodes": 7,
    }
    session = object()
    fetch_meta = AsyncMock(return_value={})

    async def fake_export(current_session, media):
        assert current_session is session
        return [row] if media == "anime" else []

    async def fake_collect(current_session, value, fav=None):
        return value

    monkeypatch.setattr(
        "stats.load_stats_all",
        lambda **kwargs: copy.deepcopy(stats),
    )
    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", fetch_meta)
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr("stats.save_stats_all", lambda value: None)

    caplog.set_level(logging.INFO)
    result, ok = await smod.sync_stats_all(session=session)

    assert ok is True
    fetch_meta.assert_awaited_once_with("anime", ["101"], session=session)
    assert result["anime"]["titles"]["101"] == {
        "title": "Актуальное название",
        "title_en": "Current title",
        "score": 9,
        "status": "watching",
        "rewatches": 2,
        "url": "",
        "poster_url": "",
        "kind": "",
        "release_status": "",
        "year": None,
        "shiki_score": None,
        "genres": [],
        "themes": [],
        "demographic": [],
        "episodes_watched": 7,
        "episodes_total": None,
        "duration": None,
        "rating": None,
        "origin": None,
        "studios": [],
    }
    assert "повреждённых title-записей: 1" in caplog.text
    assert "secret scalar" not in caplog.text
    assert "secret list" not in caplog.text


@pytest.mark.asyncio
async def test_sync_fully_recovers_malformed_record_outside_maintenance_limit(
    monkeypatch,
):
    now = datetime(2026, 9, 3, 10, 0, 0)
    current_stamp = now.isoformat()
    stats = storage._empty_stats_all()
    maintenance_ids = [str(title_id) for title_id in range(100, 151)]
    stats["anime"]["titles"] = {
        "10": ["broken"],
        "11": {
            **_anime_rec(
                status="planned",
                score=1,
                rewatches=0,
                episodes_watched=0,
                meta_updated_at=current_stamp,
            ),
            "title": "Сосед",
        },
        "999": "obsolete broken record",
        **{
            title_id: _anime_rec(
                status="planned",
                score=0,
                rewatches=0,
                episodes_watched=0,
            )
            for title_id in maintenance_ids
        },
    }
    stats["manga"]["titles"] = {
        "20": {
            **_manga_record("Соседняя манга", "manga", status="watching"),
            "meta_updated_at": current_stamp,
        },
    }
    anime_rows = [
        {
            "target_id": 10,
            "target_title": "Recovered",
            "target_title_ru": "Восстановлено",
            "score": 9,
            "status": "completed",
            "rewatches": 3,
            "episodes": 12,
        },
        {
            "target_id": 11,
            "target_title": "Neighbour",
            "target_title_ru": "Сосед",
            "score": 7,
            "status": "watching",
            "rewatches": 1,
            "episodes": 6,
        },
        *[
            {
                "target_id": int(title_id),
                "target_title": f"Anime {title_id}",
                "target_title_ru": f"Аниме {title_id}",
                "score": 0,
                "status": "planned",
                "rewatches": 0,
                "episodes": 0,
            }
            for title_id in maintenance_ids
        ],
    ]
    manga_rows = [{
        "target_id": 20,
        "target_title": "Manga neighbour",
        "target_title_ru": "Соседняя манга",
        "score": 8,
        "status": "completed",
        "rewatches": 2,
        "chapters": 40,
        "volumes": 5,
    }]
    full_metadata = {
        "url": "/animes/10",
        "poster_url": "https://cdn.example/10.jpg",
        "kind": "tv",
        "release_status": "released",
        "year": 2024,
        "shiki_score": 8.5,
        "genres": ["Драма"],
        "themes": ["Музыка"],
        "demographic": ["Сэйнэн"],
        "episodes_total": 12,
        "duration": 24,
        "rating": "R-17",
        "origin": "Оригинал",
        "studios": ["Recovery Studio"],
    }
    session = object()

    async def fake_export(current_session, media):
        assert current_session is session
        return anime_rows if media == "anime" else manga_rows

    fetch_meta = AsyncMock(side_effect=[
        {"10": full_metadata},
        {
            title_id: {
                "kind": "tv",
                "url": f"/animes/{title_id}",
            }
            for title_id in maintenance_ids[:50]
        },
    ])

    async def fake_collect(current_session, value, fav=None):
        return value

    monkeypatch.setattr(
        "stats.load_stats_all",
        lambda **kwargs: copy.deepcopy(stats),
    )
    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", fetch_meta)
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr("stats.save_stats_all", lambda value: None)
    monkeypatch.setattr("stats._utcnow", lambda: now)

    result, ok = await smod.sync_stats_all(session=session)

    assert ok is True
    assert fetch_meta.await_args_list == [
        call("anime", ["10"], session=session),
        call("anime", maintenance_ids[:50], session=session),
    ]
    recovered = result["anime"]["titles"]["10"]
    assert recovered == {
        "title": "Восстановлено",
        "title_en": "Recovered",
        "score": 9,
        "status": "completed",
        "rewatches": 3,
        "url": "/animes/10",
        "poster_url": "https://cdn.example/10.jpg",
        "kind": "tv",
        "release_status": "released",
        "year": 2024,
        "shiki_score": 8.5,
        "genres": ["Драма"],
        "themes": ["Музыка"],
        "demographic": ["Сэйнэн"],
        "episodes_watched": 12,
        "episodes_total": 12,
        "duration": 24,
        "rating": "R-17",
        "origin": "Оригинал",
        "studios": ["Recovery Studio"],
        "meta_updated_at": current_stamp,
    }
    assert "999" not in result["anime"]["titles"]
    assert result["anime"]["titles"]["11"]["score"] == 7
    assert result["anime"]["titles"]["11"]["episodes_watched"] == 6
    assert result["anime"]["titles"]["11"]["rewatches"] == 1
    manga = result["manga"]["titles"]["20"]
    assert (
        manga["score"],
        manga["status"],
        manga["chapters_read"],
        manga["volumes_read"],
        manga["rewatches"],
    ) == (8, "completed", 40, 5, 2)
    aggregates = result["anime"]["aggregates"]
    assert aggregates["total_completed"] == 1
    assert aggregates["genres"] == {"Драма": 1}
    assert aggregates["studios"] == {"Recovery Studio": 1}
    assert "meta_updated_at" not in result["anime"]["titles"]["150"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_result", [RuntimeError("offline"), {}])
async def test_sync_retries_malformed_record_after_failed_or_partial_metadata(
    monkeypatch,
    first_result,
):
    state = storage._empty_stats_all()
    state["manga"]["titles"] = {"30": None}
    row = {
        "target_id": 30,
        "target_title": "Current manga",
        "target_title_ru": "Актуальная манга",
        "score": 6,
        "status": "watching",
        "rewatches": 4,
        "chapters": 18,
        "volumes": 3,
    }
    session = object()
    full_metadata = {
        "kind": "manga",
        "url": "/mangas/30",
        "year": 2020,
        "genres": ["Приключения"],
    }
    fetch_meta = AsyncMock(side_effect=[first_result, {"30": full_metadata}])

    async def fake_export(current_session, media):
        return [row] if media == "manga" else []

    async def fake_collect(current_session, value, fav=None):
        return value

    def save(value):
        nonlocal state
        state = copy.deepcopy(value)

    monkeypatch.setattr(
        "stats.load_stats_all",
        lambda **kwargs: copy.deepcopy(state),
    )
    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", fetch_meta)
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr("stats.save_stats_all", save)

    first, first_ok = await smod.sync_stats_all(session=session)
    second, second_ok = await smod.sync_stats_all(session=session)

    safe = first["manga"]["titles"]["30"]
    assert first_ok is True
    assert safe["kind"] == ""
    assert "meta_updated_at" not in safe
    assert (
        safe["score"],
        safe["status"],
        safe["chapters_read"],
        safe["volumes_read"],
        safe["rewatches"],
    ) == (6, "watching", 18, 3, 4)
    assert second_ok is True
    repaired = second["manga"]["titles"]["30"]
    assert repaired["kind"] == "manga"
    assert repaired["genres"] == ["Приключения"]
    assert "meta_updated_at" in repaired
    assert fetch_meta.await_args_list == [
        call("manga", ["30"], session=session),
        call("manga", ["30"], session=session),
    ]


@pytest.mark.asyncio
async def test_sync_malformed_record_privacy_failure_preserves_file_and_cache(
    monkeypatch,
    tmp_path,
):
    initial = storage._empty_stats_all()
    initial["anime"]["titles"] = {"40": ["broken", "private"]}
    stats_file = tmp_path / "stats_all.json"
    stats_file.write_text(
        json.dumps(initial, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "STATS_ALL_FILE", stats_file)
    monkeypatch.setattr(storage, "_stats_all_cache", None)
    monkeypatch.setattr(storage, "_stats_all_cache_ts", 0.0)
    row = {
        "target_id": 40,
        "target_title": "Private",
        "target_title_ru": "Приватный",
        "score": 8,
        "status": "completed",
        "rewatches": 0,
        "episodes": 12,
    }

    async def fake_export(session, media):
        return [row] if media == "anime" else []

    async def private_meta(media, ids, session=None):
        raise shiki_api.ProfilePrivacyError("fetch_meta_batch(anime)")

    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", private_meta)
    monkeypatch.setattr(
        "stats.save_stats_all",
        lambda value: pytest.fail("privacy failure опубликовал recovery"),
    )

    with pytest.raises(shiki_api.ProfilePrivacyError):
        await smod.sync_stats_all(session=object())

    assert json.loads(stats_file.read_text(encoding="utf-8")) == initial
    assert storage.load_stats_all() == initial


# ════════════════════════════════════════════════════════════════
#  record_current_event + sync_stats_all (перенесено из test_polling)
# ════════════════════════════════════════════════════════════════

def _completed_event(tid, score, media="anime"):
    return {"id": str(tid), "media": media, "event": "completed",
            "score": score, "recorded_at": "2026-04-01T00:00:00+00:00"}


# ── коррекция оценки в том же квартале ──

def test_score_change_updates_existing_completed_event():
    """score_changed по тайтлу с completed-событием квартала ⇒ обновляет его score.
    Кейс «Атака титанов: случайно 3 → исправил»."""
    cur = storage._empty_stats_current(utils.current_quarter())
    cur["events"].append(_completed_event(123, 3))

    out = smod.record_current_event(cur, {"target": {"id": 123}}, "score_changed", "anime", 9)

    completed = [e for e in out["events"] if e["id"] == "123" and e["event"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["score"] == 9
    assert all(e["event"] != "score_changed" for e in out["events"])


def test_score_set_updates_existing_completed_event_without_duplicate():
    """Первая оценка после завершения обновляет запись, но не создаёт completed."""
    cur = storage._empty_stats_current(utils.current_quarter())
    cur["events"].append(_completed_event(123, None))

    out = smod.record_current_event(cur, {"target": {"id": 123}}, "score_set", "anime", 8)

    assert out["events"] == [_completed_event(123, 8)]


def test_score_change_without_completed_is_noop():
    """score_changed по тайтлу вне событий квартала ⇒ ничего не добавляем/не меняем."""
    cur = storage._empty_stats_current(utils.current_quarter())
    out = smod.record_current_event(cur, {"target": {"id": 999}}, "score_changed", "anime", 9)
    assert out["events"] == []


def test_score_removed_clears_existing_completed_without_duplicate():
    """Отмена оценки очищает score завершения текущего квартала без нового события."""
    cur = storage._empty_stats_current(utils.current_quarter())
    cur["events"].append(_completed_event(123, 5))

    out = smod.record_current_event(
        cur, {"target": {"id": 123}}, "score_removed", "anime", None,
    )

    assert out["events"] == [_completed_event(123, None)]


def test_score_removed_without_completed_is_noop():
    """Отмена оценки вне завершений текущего квартала не создаёт событие."""
    cur = storage._empty_stats_current(utils.current_quarter())

    out = smod.record_current_event(
        cur, {"target": {"id": 999}}, "score_removed", "anime", None,
    )

    assert out["events"] == []


def test_record_current_event_distinguishes_same_id_across_media():
    """Одинаковые числовые ID аниме и манги не считаются одним событием."""
    cur = storage._empty_stats_current(utils.current_quarter())
    entry = {"target": {"id": 123}}

    smod.record_current_event(cur, entry, "completed", "anime", 7)
    smod.record_current_event(cur, entry, "completed", "manga", 8)

    assert [
        (event["id"], event["media"], event["event"], event["score"])
        for event in cur["events"]
    ] == [
        ("123", "anime", "completed", 7),
        ("123", "manga", "completed", 8),
    ]


@pytest.mark.parametrize(
    ("event_type", "score", "expected_manga_score"),
    [
        ("score_set", 8, 8),
        ("score_changed", 9, 9),
        ("score_removed", None, None),
    ],
)
def test_score_event_updates_only_matching_media(
    event_type,
    score,
    expected_manga_score,
):
    """Оценка меняется у совпавшей пары media + id, а не у первого такого ID."""
    cur = storage._empty_stats_current(utils.current_quarter())
    cur["events"] = [
        _completed_event(123, 3, "anime"),
        _completed_event(123, 6, "manga"),
    ]

    smod.record_current_event(
        cur,
        {"target": {"id": 123}},
        event_type,
        "manga",
        score,
    )

    assert cur["events"] == [
        _completed_event(123, 3, "anime"),
        _completed_event(123, expected_manga_score, "manga"),
    ]


@pytest.mark.asyncio
async def test_sync_stats_all_total_failure_preserves_and_flags_false(monkeypatch):
    """Оба экспорта упали (429) ⇒ возвращаем ПРЕЖНИЙ stats_all нетронутым и ok=False,
    save не вызывается. Гарантия «429 не ломает stats_all»."""
    import stats as stats_mod
    preserved = {"_sentinel": "keep-me"}
    saved = []

    async def fake_export(session, media):
        return None

    monkeypatch.setattr(stats_mod, "fetch_list_export", fake_export)
    monkeypatch.setattr("stats.load_stats_all", lambda use_cache=True: preserved)
    monkeypatch.setattr("stats.save_stats_all", lambda d: saved.append(d))

    result_stats, ok = await smod.sync_stats_all()

    assert ok is False
    assert result_stats is preserved
    assert saved == []


@pytest.mark.asyncio
async def test_sync_stats_all_private_export_dominates_partial_success(monkeypatch):
    """Один доступный экспорт не маскирует privacy failure второго."""
    import stats as stats_mod

    preserved = {"_sentinel": "keep-me"}
    saved = []

    async def fake_export(session, media):
        if media == "anime":
            return []
        raise shiki_api.ProfilePrivacyError("fetch_list_export(manga)")

    monkeypatch.setattr(stats_mod, "fetch_list_export", fake_export)
    monkeypatch.setattr("stats.load_stats_all", lambda use_cache=True: preserved)
    monkeypatch.setattr("stats.save_stats_all", lambda data: saved.append(data))

    with pytest.raises(shiki_api.ProfilePrivacyError):
        await smod.sync_stats_all()

    assert preserved == {"_sentinel": "keep-me"}
    assert saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("privacy_stage", ["metadata", "favourites"])
async def test_sync_stats_all_late_privacy_failure_preserves_process_cache(
    monkeypatch,
    tmp_path,
    privacy_stage,
):
    """Поздний privacy failure не публикует частичный sync через process cache."""
    initial = storage._empty_stats_all()
    stats_file = tmp_path / "stats_all.json"
    stats_file.write_text(
        json.dumps(initial, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "STATS_ALL_FILE", stats_file)
    monkeypatch.setattr(storage, "_stats_all_cache", None)
    monkeypatch.setattr(storage, "_stats_all_cache_ts", 0.0)

    exports = {
        "anime": [
            {
                "target_id": 1,
                "target_type": "Anime",
                "target_title": "Ergo Proxy",
                "target_title_ru": "Эрго Прокси",
                "score": 8,
                "status": "completed",
                "rewatches": 0,
                "episodes": 23,
            },
        ],
        "manga": [
            {
                "target_id": 2,
                "target_type": "Manga",
                "target_title": "Berserk",
                "target_title_ru": "Берсерк",
                "score": 9,
                "status": "completed",
                "rewatches": 0,
                "chapters": 10,
                "volumes": 2,
            },
        ],
    }

    async def fake_export(session, media):
        return exports[media]

    async def fake_meta(media, ids, session=None):
        if privacy_stage == "metadata" and media == "manga":
            raise shiki_api.ProfilePrivacyError("fetch_meta_batch(manga)")
        kind = "tv" if media == "anime" else "manga"
        return {tid: {"kind": kind} for tid in ids}

    async def fake_collect(session, stats, fav=None):
        if privacy_stage == "favourites":
            raise shiki_api.ProfilePrivacyError("fetch_favourites")
        return stats

    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", fake_meta)
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr(
        "stats.save_stats_all",
        lambda data: pytest.fail("privacy failure сохранил частичный stats_all"),
    )

    with pytest.raises(shiki_api.ProfilePrivacyError):
        await smod.sync_stats_all(session=object())

    assert storage.load_stats_all() == initial
