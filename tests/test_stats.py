"""
Тесты модуля статистики и избранного (ветка stats).

Покрывают то, что появилось в ветке и НЕ покрыто другими файлами:
форматтеры отчётов, агрегацию, фильтр мусора по kind, сбор избранного,
нормализацию URL, утилиты, и smoke-тесты на билдеры сообщений.

Намеренно НЕ дублирует:
  test_messages.py  — build_message, h()
  test_parsers.py   — extract_score, classify_event, _strip_html
  test_favourites.py— build_favourite_message
  test_media.py     — get_media_info

Тесты ветки favourites-fix (unit 2, metadata-retry) — в конце файла,
под своим секционным заголовком.

Дисциплина: падает на непропатченном, проходит на пропатченном.
"""

import copy
import re
from unittest.mock import AsyncMock

import pytest

import main


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
#  Утилиты: _safe_int / _safe_float / _utcnow / _rel_url
# ════════════════════════════════════════════════════════════════

def test_safe_int_valid():
    assert main._safe_int(5) == 5
    assert main._safe_int("7") == 7

def test_safe_int_invalid_returns_default():
    assert main._safe_int(None) == 0
    assert main._safe_int("abc") == 0
    assert main._safe_int("abc", default=-1) == -1

def test_safe_float_valid():
    assert main._safe_float(7.5) == 7.5
    assert main._safe_float("8.1") == 8.1

def test_safe_float_invalid_returns_default():
    assert main._safe_float(None) is None
    assert main._safe_float("xyz") is None
    assert main._safe_float("xyz", default=0.0) == 0.0

def test_utcnow_is_naive():
    dt = main._utcnow()
    assert dt.tzinfo is None  # наивное UTC, сравнимо с fromisoformat


# ── _rel_url: регрессия на баг двойного домена (GraphQL отдаёт полный URL) ──

def test_rel_url_strips_full_https():
    assert main._rel_url("https://shikimori.io/animes/226-elfen-lied") == "/animes/226-elfen-lied"

def test_rel_url_strips_full_http():
    assert main._rel_url("http://shikimori.io/mangas/25") == "/mangas/25"

def test_rel_url_keeps_relative():
    assert main._rel_url("/animes/30-eva") == "/animes/30-eva"

def test_rel_url_empty_and_none():
    assert main._rel_url("") == ""
    assert main._rel_url(None) == ""

def test_rel_url_domain_only():
    assert main._rel_url("https://shikimori.io") == ""


# ════════════════════════════════════════════════════════════════
#  is_relevant — фильтр значимости (сама функция, не замокана)
# ════════════════════════════════════════════════════════════════

def test_is_relevant_anime_allowed_kinds():
    for kind in ("tv", "movie", "ova", "ona"):
        assert main.is_relevant("anime", kind) is True, kind

def test_is_relevant_anime_drops_specials_and_clips():
    for kind in ("special", "tv_special", "music", "pv", "cm"):
        assert main.is_relevant("anime", kind) is False, kind

def test_is_relevant_manga_blocks_oneshot_doujin():
    assert main.is_relevant("manga", "one_shot") is False
    assert main.is_relevant("manga", "doujin") is False

def test_is_relevant_manga_allows_regular():
    assert main.is_relevant("manga", "manga") is True

def test_is_relevant_empty_kind_is_false():
    assert main.is_relevant("anime", "") is False


# ════════════════════════════════════════════════════════════════
#  Квартальные даты
# ════════════════════════════════════════════════════════════════

def test_current_quarter():
    from datetime import datetime
    assert main.current_quarter(datetime(2026, 1, 15)) == "2026-Q1"
    assert main.current_quarter(datetime(2026, 4, 1)) == "2026-Q2"
    assert main.current_quarter(datetime(2026, 7, 31)) == "2026-Q3"
    assert main.current_quarter(datetime(2026, 12, 1)) == "2026-Q4"

def test_quarter_start():
    from datetime import datetime
    assert main.quarter_start(datetime(2026, 5, 20)) == datetime(2026, 4, 1)
    assert main.quarter_start(datetime(2026, 1, 1)) == datetime(2026, 1, 1)


# ════════════════════════════════════════════════════════════════
#  Форматтеры
# ════════════════════════════════════════════════════════════════

def test_section_header():
    assert main._section_header("🎬", "АНИМЕ") == "<b>━━━━━ 🎬 АНИМЕ ━━━━━</b>"

def test_fmt_mono_rows_empty():
    assert main._fmt_mono_rows([]) == ""

def test_fmt_mono_rows_basic_alignment():
    out = main._fmt_mono_rows([("Экшен", 66), ("Триллер", 45)])
    assert "<code>" in out and "</code>" in out
    assert "66" in out and "45" in out

def test_fmt_mono_rows_percent():
    out = main._fmt_mono_rows([("Экшен", 50)], show_percent=True, total=100)
    assert "50%" in out

def test_fmt_mono_rows_percent_skipped_without_total():
    out = main._fmt_mono_rows([("Экшен", 50)], show_percent=True, total=0)
    assert "%" not in out

def test_fmt_mono_rows_html_escape():
    out = main._fmt_mono_rows([("A & B", 5)])
    assert "&amp;" in out

def test_top_block_empty_counter():
    assert main._top_block("🎭", "Жанры", {}, 8) == []

def test_top_block_structure():
    block = main._top_block("🎨", "Студии", {"MAPPA": 12, "Bones": 9}, 6)
    assert len(block) == 2
    assert "Студии" in block[0]
    assert "<code>" in block[1]

def test_score_dist_block_empty():
    assert main._score_dist_block({}) == []

def test_score_dist_block_star_marker_and_order():
    # score_dist: оценка -> сколько раз; нули (без оценки) игнорируются
    block = main._score_dist_block({"10": 5, "9": 8, "0": 3})
    body = block[1]
    assert "★10" in body and "★9" in body
    assert "★0" not in body  # нулевая оценка не показывается
    # порядок по убыванию: ★10 раньше ★9
    assert body.index("★10") < body.index("★9")

def test_fmt_kinds_order_and_skip_zero():
    out = main._fmt_kinds({"tv": 61, "movie": 36, "ova": 0}, main._KIND_RU_ANIME)
    assert "Сериалы 61" in out and "Фильмы 36" in out
    assert "OVA" not in out  # ноль пропускается

def test_avg_score_from_dist():
    # (10*1 + 8*2) / 3 = 8.67
    assert main._avg_score_from_dist({"10": 1, "8": 2}) == pytest.approx(8.67, abs=0.01)

def test_avg_score_from_dist_ignores_zero():
    # нули (без оценки) не участвуют
    assert main._avg_score_from_dist({"8": 1, "0": 100}) == 8.0

def test_avg_score_from_dist_empty():
    assert main._avg_score_from_dist({}) is None


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
    agg = main.recompute_aggregates("anime", titles)
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
    agg = main.recompute_aggregates("anime", titles)
    assert agg["genres"].get("Экшен") == 1
    assert "Драма" not in agg["genres"]  # дроп не учитывается в жанрах

def test_aggregates_score_dist():
    titles = {
        "1": _anime_rec(score=9),
        "2": _anime_rec(score=9),
        "3": _anime_rec(score=7),
    }
    agg = main.recompute_aggregates("anime", titles)
    # Ключи score_dist — строки (для JSON-совместимости)
    assert agg["score_dist"].get("9") == 2
    assert agg["score_dist"].get("7") == 1

def test_aggregates_avg_shiki_only_with_personal_score():
    # shiki берётся только если есть личная оценка
    titles = {
        "1": _anime_rec(score=8, shiki_score=7.0),
        "2": _anime_rec(score=0, shiki_score=9.0),  # без личной оценки -> shiki не учитывается
    }
    agg = main.recompute_aggregates("anime", titles)
    assert agg["avg_shiki_completed"] == 7.0

def test_aggregates_episodes_and_hours():
    titles = {"1": _anime_rec(episodes_watched=25, duration=24)}  # 600 мин = 10 ч
    agg = main.recompute_aggregates("anime", titles)
    assert agg["total_episodes_watched"] == 25
    assert agg["total_hours_watched"] == 10.0

def test_aggregates_manga_chapters():
    titles = {"1": dict(status="completed", score=8, kind="manga",
                        genres=[], themes=[], demographic=[], publishers=["Young Ace"],
                        chapters_read=100, volumes_read=10)}
    agg = main.recompute_aggregates("manga", titles)
    assert agg["total_chapters_read"] == 100
    assert agg["total_volumes_read"] == 10
    assert agg["publishers"].get("Young Ace") == 1


# ════════════════════════════════════════════════════════════════
#  Регрессия: фильтр мусора по kind (баг с раздутым счётчиком студии)
# ════════════════════════════════════════════════════════════════

def test_garbage_kind_inflates_nothing_after_filter():
    """
    Регрессия. Спецвыпуски НЕ должны попадать в titles (фильтруются в
    sync_stats_all через is_relevant). Здесь проверяем следствие: если в
    titles только релевантные записи, счётчик студии = числу релевантных,
    а не раздут спецвыпусками (был баг: Studio Deen 11 вместо 8 из-за
    того, что спецвыпуски Цикад накручивали счётчик).
    """
    # titles уже отфильтрованы (как после sync) — только tv/ova
    titles = {
        "1": _anime_rec(kind="tv",  studios=["Studio Deen"]),
        "2": _anime_rec(kind="ova", studios=["Studio Deen"]),
    }
    agg = main.recompute_aggregates("anime", titles)
    # Ровно 2 — оба релевантны, спецвыпусков нет
    assert agg["studios"].get("Studio Deen") == 2


# ════════════════════════════════════════════════════════════════
#  Избранное: _collect_favourites (джойн с titles)
# ════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_collect_favourites_join_with_titles(monkeypatch):
    stats = main._empty_stats_all()
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
    monkeypatch.setattr(main, "fetch_favourites", fake_fetch)

    class S:
        pass
    stats = await main._collect_favourites(S(), stats)
    fa = {e["id"]: e for e in stats["favourites"]["anime"]}

    assert fa["790"].get("score") == 9            # оценка из titles
    assert "score" not in fa["5114"]              # score=0 -> не показываем
    assert fa["9999"]["title"] == "Не в списке"   # не в titles -> имя из API
    assert "score" not in fa["9999"]

@pytest.mark.asyncio
async def test_collect_favourites_api_fail_keeps_previous(monkeypatch):
    stats = main._empty_stats_all()
    stats["favourites"]["anime"] = [{"id": "1", "title": "Старое", "url": "/animes/1"}]

    async def fake_fetch(session):
        return None  # сбой API
    monkeypatch.setattr(main, "fetch_favourites", fake_fetch)

    class S: 
        pass
    stats = await main._collect_favourites(S(), stats)
    # Прежнее избранное не затёрто
    assert stats["favourites"]["anime"] == [{"id": "1", "title": "Старое", "url": "/animes/1"}]


# ════════════════════════════════════════════════════════════════
#  SMOKE-тесты билдеров (поймали бы оба прод-бага)
#    1. build_stats_all_messages undefined после ручного мержа
#    2. двойной домен в ссылках
# ════════════════════════════════════════════════════════════════

def _populated_stats():
    stats = main._empty_stats_all()
    stats["anime"]["aggregates"] = main.recompute_aggregates("anime", {
        "1": _anime_rec(score=9, year=2013),
    })
    stats["manga"]["aggregates"] = main.recompute_aggregates("manga", {})
    stats["favourites"]["anime"] = [
        {"id": "1", "title": "Эрго Прокси", "url": "/animes/790", "score": 9}
    ]
    return stats

def test_smoke_build_stats_all_returns_list():
    msgs = main.build_stats_all_messages(_populated_stats())
    assert isinstance(msgs, list)
    assert all(isinstance(m, str) for m in msgs)
    assert msgs  # непустой

def test_smoke_build_favourites_returns_list():
    msgs = main.build_favourites_messages(_populated_stats())
    assert isinstance(msgs, list) and all(isinstance(m, str) for m in msgs)

def test_smoke_build_current_returns_list():
    cur = {"period": "2026-Q2", "period_start": "2026-04-01T00:00:00",
           "tracking_since": "2026-04-01T00:00:00", "events": []}
    msgs = main.build_current_stats_messages(cur, _populated_stats())
    assert isinstance(msgs, list) and all(isinstance(m, str) for m in msgs)

def test_smoke_empty_stats_no_crash():
    # Пустая структура не должна ронять билдеры
    empty = main._empty_stats_all()
    assert main.build_stats_all_messages(empty)
    assert main.build_favourites_messages(empty)

@pytest.mark.asyncio
async def test_smoke_async_report_builders(monkeypatch):
    monkeypatch.setattr(main, "load_stats_all", lambda: _populated_stats())
    monkeypatch.setattr(main, "load_stats_current", lambda: {
        "period": "2026-Q2", "period_start": "2026-04-01T00:00:00",
        "tracking_since": "2026-04-01T00:00:00", "events": []})
    for builder in (main._stats_report_all, main._stats_report_current,
                    main._stats_report_favourites):
        msgs = await builder()
        assert isinstance(msgs, list) and all(isinstance(m, str) for m in msgs)


# ── Регрессия: ссылки содержат домен РОВНО один раз (нет двойного домена) ──

def test_links_single_domain_in_favourites():
    stats = main._empty_stats_all()
    # Полный URL из GraphQL — провокация двойного домена
    stats["favourites"]["anime"] = [
        {"id": "1", "title": "Тест", "url": "https://shikimori.io/animes/226", "score": 10}
    ]
    msg = main.build_favourites_messages(stats)[0]
    # Домен должен встречаться ровно один раз в href
    hrefs = re.findall(r'href="([^"]*)"', msg)
    assert hrefs, "должна быть ссылка"
    for href in hrefs:
        assert href.count("shikimori.io") == 1, f"двойной домен: {href}"
        assert href.startswith("https://shikimori.io/"), href


# ════════════════════════════════════════════════════════════════
#  favourites-fix (unit 2): metadata-retry в sync_stats_all
#  Битая мета (пустой kind) дозапрашивается, ваншот пересобирается и
#  вычищается самоочисткой (43→39 в проде). Анонс (мета снова пустая) —
#  retry пробуется, но это no-op (запись цела, без записи на диск).
# ════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_repairs_empty_kind_and_filters_oneshot(monkeypatch):
    # stats_all: один нормальный completed-тайтл + один с битой метой (пустой kind)
    stats = main._empty_stats_all()
    stats["manga"]["titles"] = {
        "111":    _manga_record("Нормальная манга", "manga"),
        "120393": _manga_record("Elfen Lied Tokubetsu-hen", ""),   # битая мета
    }

    monkeypatch.setattr(main, "load_stats_all",
                        lambda *a, **k: copy.deepcopy(stats))

    async def fake_export(session, media):
        if media == "manga":
            return [_export_manga_row("111"), _export_manga_row("120393")]
        return []   # аниме пусто
    monkeypatch.setattr(main, "fetch_list_export", fake_export)

    # GraphQL теперь возвращает настоящий вид ваншота
    async def fake_meta(media, ids):
        if media == "manga" and "120393" in ids:
            return {"120393": {"kind": "one_shot", "url": "/mangas/120393",
                               "year": 2005}}
        return {}
    monkeypatch.setattr(main, "fetch_meta_batch", fake_meta)

    # избранное не трогаем (без сети)
    async def fake_collect(session, st, fav=None):
        return st
    monkeypatch.setattr(main, "_collect_favourites", fake_collect)
    saved = {}
    monkeypatch.setattr(main, "save_stats_all", lambda data: saved.update(data))

    result, ok = await main.sync_stats_all()

    assert ok is True
    titles = result["manga"]["titles"]
    # Ваншот починен (kind заполнен) и вычищен самоочисткой
    assert "120393" not in titles
    assert "111" in titles
    # «Прочитано» = 1, а не 2 (ваншот больше не считается)
    assert result["manga"]["aggregates"]["total_completed"] == 1


@pytest.mark.asyncio
async def test_sync_announced_empty_kind_is_noop_but_retried(monkeypatch):
    stats = main._empty_stats_all()
    # planned-тайтл с пустым kind (анонс — вид ещё неизвестен)
    stats["manga"]["titles"] = {
        "999": _manga_record("Анонс", "", status="planned", chapters_read=0),
    }
    monkeypatch.setattr(main, "load_stats_all",
                        lambda *a, **k: copy.deepcopy(stats))

    async def fake_export(session, media):
        if media == "manga":
            return [_export_manga_row("999", status="planned", chapters=0)]
        return []
    monkeypatch.setattr(main, "fetch_list_export", fake_export)

    meta_calls = []

    async def fake_meta(media, ids):
        meta_calls.append((media, list(ids)))
        # анонс: GraphQL вернул элемент, но kind по-прежнему пустой
        if media == "manga" and "999" in ids:
            return {"999": {"kind": "", "url": "", "year": None}}
        return {}
    monkeypatch.setattr(main, "fetch_meta_batch", fake_meta)

    async def fake_collect(session, st, fav=None):
        return st
    monkeypatch.setattr(main, "_collect_favourites", fake_collect)
    monkeypatch.setattr(main, "save_stats_all", lambda *a, **k: None)

    result, ok = await main.sync_stats_all()

    # retry БЫЛ предпринят для безвидового анонса (это и есть фикс Codacy)
    assert ("manga", ["999"]) in meta_calls
    # но это no-op: запись цела, kind остался пустым (не выдумали вид)
    assert "999" in result["manga"]["titles"]
    assert result["manga"]["titles"]["999"]["kind"] == ""


# ════════════════════════════════════════════════════════════════
#  polish
# ════════════════════════════════════════════════════════════════

def test_stats_menu_kb_has_close_button():
    """Меню /stats содержит кнопку ❌ Закрыть с callback_data 'stats:close'."""

    kb = main._stats_menu_kb()
    buttons = [b for row in kb.inline_keyboard for b in row]
    close = [b for b in buttons if b.callback_data == "stats:close"]
    assert len(close) == 1, "ожидал ровно одну кнопку закрытия"
    assert "Закры" in close[0].text


@pytest.mark.asyncio
async def test_cmd_stats_menu_is_reply():
    """Меню /stats шлётся ответом (reply) на команду — иначе ❌ Закрыть
    не сможет удалить саму команду (рвётся reply_to_message)."""

    message = AsyncMock()
    message.text = "/stats"

    await main.cmd_stats(message)

    message.reply.assert_awaited_once()
    message.answer.assert_not_called()


@pytest.mark.asyncio
async def test_stats_menu_close_deletes_menu_and_command():
    """stats:close удаляет и меню, и команду /stats (reply_to_message)."""

    callback = AsyncMock()
    callback.data = "stats:close"
    callback.message = AsyncMock()
    callback.message.reply_to_message = AsyncMock()

    await main.stats_menu_cb(callback)

    callback.answer.assert_awaited_once_with()
    callback.message.delete.assert_awaited_once()
    callback.message.reply_to_message.delete.assert_awaited_once()
    callback.message.answer.assert_not_called()


@pytest.mark.asyncio
async def test_stats_menu_close_without_reply_does_not_crash():
    """reply_to_message=None → закрытие удаляет только меню, без падения."""

    callback = AsyncMock()
    callback.data = "stats:close"
    callback.message = AsyncMock()
    callback.message.reply_to_message = None

    await main.stats_menu_cb(callback)

    callback.message.delete.assert_awaited_once()
    callback.answer.assert_awaited_once_with()
