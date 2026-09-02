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
import re
from unittest.mock import MagicMock

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
    chooser = MagicMock(side_effect=lambda items: items[0])
    monkeypatch.setattr(smod.random, "choice", chooser)

    result = smod.select_contrast_pick_candidate((anchor,), anchor, {anchor.id})

    assert result.candidate == anchor
    assert result.pool_reset is True
    assert result.shown_ids == frozenset({anchor.id})
    assert tuple(chooser.call_args.args[0]) == (anchor,)


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
    monkeypatch.setattr("stats.load_stats_all", lambda use_cache=False: stats)
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
async def test_sync_leaves_legacy_planned_poster_refresh_to_issue_57(monkeypatch):
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

    async def forbidden_meta(*args, **kwargs):
        pytest.fail("#60 запустил eager poster refresh вместо будущего #57")

    async def fake_collect(session, value, fav=None):
        return value

    monkeypatch.setattr("stats.fetch_list_export", fake_export)
    monkeypatch.setattr("stats.fetch_meta_batch", forbidden_meta)
    monkeypatch.setattr("stats._collect_favourites", fake_collect)
    monkeypatch.setattr("stats.save_stats_all", lambda value: None)

    result, ok = await smod.sync_stats_all(session=object())

    assert ok is True
    assert "poster_url" not in result["manga"]["titles"]["111"]


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
