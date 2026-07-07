# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import random
import time

import pytest

import config
import messages
from messages import (
    _strip_html,
    build_favourite_message,
    build_message,
    build_startup_snapshot,
    classify_event,
    extract_score,
    extract_score_change,
    format_rate_entry,
)
from utils import _utcnow, h


def fixed_choice(seq):
    return seq[0]


def make_entry(description, title="Ergo Proxy", url="/animes/790-ergo-proxy"):
    return {
        "description": description,
        "target": {
            "name": title,
            "url": url,
        },
        "created_at": "2025-01-01T12:00:00.000Z",
    }


# ==========================================================
# h()
# ==========================================================

def test_h_escapes_angle_brackets():
    assert h("<Ergo Proxy>") == "&lt;Ergo Proxy&gt;"


def test_h_escapes_ampersand():
    assert h("A&B") == "A&amp;B"


def test_h_escapes_quotes():
    assert h('"test"') == "&quot;test&quot;"


def test_h_plain_text():
    assert h("Evangelion") == "Evangelion"


# ==========================================================
# build_message()
# ==========================================================

@pytest.mark.parametrize("desc, score, key", [
    ("просмотрено", None, "completed_no_score"),
    ("оценено на 3", 3, "completed_score_low"),
    ("оценено на 6", 6, "completed_score_mid"),
    ("оценено на 9", 9, "completed_score_high"),
    ("оценено на 10", 10, "completed_score_perfect"),
])
def test_build_message_completed_selects_bank_by_score(monkeypatch, desc, score, key):
    # с fixed_choice шаблон детерминирован -> точная сверка ВЫБРАННОГО банка,
    # а не «цифра где-то в тексте» (та проходит на любом банке -> мутационно дырява)
    monkeypatch.setattr(random, "choice", fixed_choice)
    msg = build_message(make_entry(desc))
    title = f'<a href="{messages.SHIKI_BASE_URL}/animes/790-ergo-proxy">Ergo Proxy</a>'
    expected = messages.MESSAGES["anime"][key][0].format(
        n=messages._DISPLAY_NAME_HTML, title=title,
        score=score if score is not None else "?",
    )
    assert msg == expected


def test_build_message_manga_uses_manga_bank(monkeypatch):
    # media_type определяется по target.type (не по url) -> проверяем банк manga
    monkeypatch.setattr(random, "choice", fixed_choice)
    entry = {
        "description": "оценено на 3",
        "target": {"name": "Berserk", "url": "/mangas/25-berserk", "type": "Manga"},
        "created_at": "2025-01-01T12:00:00.000Z",
    }
    msg = build_message(entry)
    title = f'<a href="{messages.SHIKI_BASE_URL}/mangas/25-berserk">Berserk</a>'
    expected = messages.MESSAGES["manga"]["completed_score_low"][0].format(
        n=messages._DISPLAY_NAME_HTML, title=title, score=3,
    )
    assert msg == expected


def test_score_changed_uses_change_bank(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)
    msg = build_message(make_entry("изменена оценка с 5 на 8"))
    title = f'<a href="{messages.SHIKI_BASE_URL}/animes/790-ergo-proxy">Ergo Proxy</a>'
    expected = messages.MESSAGES["score_changed"][0].format(
        n=messages._DISPLAY_NAME_HTML, title=title, old=5, new=8,
    )
    assert msg == expected


def test_html_title_escape(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry(
            "оценено на 8",
            "<Ergo & Proxy>"
        )
    )

    assert "&lt;Ergo &amp; Proxy&gt;" in msg


# ==========================================================
# links
# ==========================================================

def test_message_contains_shikimori_link(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry("оценено на 8")
    )

    assert '<a href="' in msg
    assert "ergo-proxy" in msg


def test_message_without_url(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry(
            "оценено на 8",
            url=""
        )
    )

    assert '<a href="' not in msg


# ── экранирование DISPLAY_NAME в HTML-шаблонах (Codacy MEDIUM) ──────

def test_display_name_html_constant_is_escaped():
    """DISPLAY_NAME из env экранируется для HTML — иначе < > & в имени → Telegram 400."""
    import config
    assert messages._DISPLAY_NAME_HTML == h(config.DISPLAY_NAME)


def test_favourite_message_uses_escaped_name(monkeypatch):
    monkeypatch.setattr(messages, "_DISPLAY_NAME_HTML", "Ампер&амп;Санд")
    item = {"id": 1, "name": "X", "russian": "Икс", "url": None}
    text = messages.build_favourite_message("animes", item)
    assert "Ампер&амп;Санд" in text


def test_broadcast_header_escapes_special_chars(monkeypatch):
    import importlib

    import config
    monkeypatch.setattr(config, "DISPLAY_NAME", "A<b>&Co", raising=False)
    importlib.reload(messages)
    try:
        assert "A&lt;b&gt;&amp;Co" in messages.BROADCAST_HEADER
        assert "A<b>&Co" not in messages.BROADCAST_HEADER
    finally:
        monkeypatch.undo()
        importlib.reload(messages)
# ==========================================================
# build_startup_snapshot — стартовый health-снапшот (owner-gate)
# ==========================================================
def _snap(**over):
    base = dict(
        display_name="Пётр", shiki_user="WNR", check_interval_sec=600,
        subscriber_count=3, seen_ids_count=1240, seen_favs_count=37,
        stats_updated_at=_utcnow().isoformat(), last_backup_at=time.time(),
    )
    base.update(over)
    return build_startup_snapshot(**base)


def test_startup_snapshot_normal_state():
    txt = _snap()
    assert txt.startswith("🟢 Бот запущен")
    assert "Имя: Пётр" in txt and "Шики-логин: WNR" in txt
    assert "проверка каждые 10 мин" in txt          # 600 сек -> 10 мин
    assert "Подписчиков: 3" in txt
    assert "история 1240" in txt and "избранное 37" in txt
    assert "события за простой догоним" in txt
    assert "Последняя синхронизация статистики:" in txt
    assert "нет данных" not in txt                   # обе метки свежие


def test_startup_snapshot_full_wipe_collapses_to_banner():
    txt = _snap(subscriber_count=0, seen_ids_count=0, seen_favs_count=0,
                stats_updated_at=None, last_backup_at=None)
    assert "Чистый инстанс" in txt
    assert "не догоним" in txt
    assert "нет данных" not in txt                   # схлопнуто в один баннер
    assert "🗂 Отслеживание:" not in txt             # обычной строки отслеживания нет
    assert "Последняя синхронизация статистики:" not in txt


def test_startup_snapshot_tracking_not_initialized_but_stats_present():
    # seen_ids пусто, но stats_all есть -> не вайп, а предупреждение
    txt = _snap(seen_ids_count=0, stats_updated_at=_utcnow().isoformat(),
                last_backup_at=None)
    assert "⚠️ Отслеживание не инициализировано" in txt
    assert "уйдут в тишину" in txt
    assert "Чистый инстанс" not in txt
    assert "Последняя синхронизация статистики:" in txt
    assert "💾 Последний плановый бэкап: нет данных" in txt    # бэкапа не было


def test_startup_snapshot_survives_bad_timestamps():
    txt = _snap(stats_updated_at="не-дата", last_backup_at="тоже-не-число")
    # кривые метки не роняют билдер, деградируют в 'нет данных'
    assert "🟢 Бот запущен" in txt
    assert "нет данных" in txt


# ==========================================================
# _strip_html
# ==========================================================

def test_strip_html_bold():
    assert _strip_html("оценено на <b>7</b>") == "оценено на 7"


def test_strip_html_strong():
    assert _strip_html("оценено на <strong>8</strong>") == "оценено на 8"


def test_strip_html_multiple_tags():
    assert (
        _strip_html("изменена оценка с <b>5</b> на <i>9</i>")
        == "изменена оценка с 5 на 9"
    )


# ==========================================================
# extract_score
# ==========================================================

def test_extract_score_ru():
    assert extract_score("оценено на 9") == 9


def test_extract_score_alt_ru_male():
    assert extract_score("выставил оценку 8") == 8


def test_extract_score_alt_ru_female():
    assert extract_score("выставила оценку 6") == 6


def test_extract_score_rated():
    assert extract_score("rated 7") == 7


def test_extract_score_scored():
    assert extract_score("scored 10") == 10


def test_extract_score_html_bold():
    assert extract_score("оценено на <b>7</b>") == 7


def test_extract_score_html_strong():
    assert extract_score("оценено на <strong>8</strong>") == 8


def test_extract_score_invalid():
    assert extract_score("какой-то текст") is None


def test_extract_score_empty():
    assert extract_score("") is None


# ==========================================================
# extract_score_change
# ==========================================================

def test_extract_score_change_ru():
    assert extract_score_change(
        "изменена оценка с 5 на 9"
    ) == (5, 9)


def test_extract_score_change_html():
    assert extract_score_change(
        "изменена оценка с <b>5</b> на <b>9</b>"
    ) == (5, 9)


def test_extract_score_change_latin_c_homoglyph():
    # Shikimori шлёт латинскую "c" (U+0063), не кириллическую "с" (U+0441);
    # реальная строка ещё и оборачивает оценки в <b>. Регресс на "?/10 вместо ?".
    assert extract_score_change(
        "Изменена оценка c <b>6</b> на <b>7</b>"
    ) == (6, 7)


def test_extract_score_change_invalid():
    assert extract_score_change(
        "изменена оценка"
    ) is None


def test_extract_score_change_english_still_parsed():
    # Нормализация не должна ломать чисто латинские форматы.
    assert extract_score("rated 7") == 7
    assert extract_score("scored 10") == 10


def test_extract_score_homoglyph_in_russian_word():
    # Латинская "о" (U+006f) внутри «оценено» — mixed-script, чинится.
    assert extract_score("\u006fценено на 9") == 9


def test_classify_event_homoglyph_in_russian_word():
    # Латинская "o" в «брошено» — без нормализации классификатор промахнётся.
    assert classify_event("бр\u006fшено") == "dropped"


# ==========================================================
# classify_event
# ==========================================================

def test_classify_score_changed():
    assert classify_event(
        "изменена оценка с 5 на 8"
    ) == "score_changed"


def test_classify_watching_smotryu():
    assert classify_event("смотрю") == "watching"


def test_classify_watching_smotrit():
    assert classify_event("смотрит") == "watching"


def test_classify_watching_chitayu():
    assert classify_event("читаю") == "watching"


def test_classify_watching_reading():
    assert classify_event("reading") == "watching"


def test_classify_rewatching_ru():
    assert classify_event("пересматриваю") == "rewatching"


def test_classify_rereading_ru():
    assert classify_event("перечитываю") == "rewatching"


def test_classify_rewatching_en():
    assert classify_event("rewatching") == "rewatching"


def test_classify_planned():
    assert classify_event("добавлено в список") == "planned"


def test_classify_planned_english():
    assert classify_event("planned") == "planned"


def test_classify_dropped():
    assert classify_event("брошено") == "dropped"


def test_classify_completed_fallback():
    assert classify_event("просмотрено") == "completed"


def test_classify_completed_with_score():
    assert classify_event("оценено на 8") == "completed"


# ============================================================
# format_rate_entry()
# ============================================================

def test_format_rate_entry_russian_title_priority():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Ergo Proxy",
            "russian": "Эрго Прокси",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "Эрго Прокси" in result
    assert "Ergo Proxy" not in result


def test_format_rate_entry_fallback_to_english():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Ergo Proxy",
            "russian": "",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "Ergo Proxy" in result


def test_format_rate_entry_html_escape():
    item = {
        "_status": "watching",
        "anime": {
            "name": "<Ergo & Proxy>",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "&lt;Ergo &amp; Proxy&gt;" in result


def test_format_rate_entry_watching_icon():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert result.startswith("▶️")


def test_format_rate_entry_rewatching_icon():
    item = {
        "_status": "rewatching",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert result.startswith("🔁")


def test_format_rate_entry_unknown_icon():
    item = {
        "_status": "something",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert result.startswith("•")


def test_format_rate_entry_with_link():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Anime",
            "url": "/animes/1-anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert 'href="' in result
    assert "/animes/1-anime" in result


def test_format_rate_entry_without_link():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "href=" not in result


# ============================================================
# Message building
# ============================================================

def test_build_favourite_message_prefers_russian():
    item = {
        "russian": "Эрго Прокси",
        "name": "Ergo Proxy",
    }

    msg = build_favourite_message("animes", item)

    assert "Эрго Прокси" in msg


def test_build_favourite_message_english_fallback():
    item = {
        "name": "Ergo Proxy",
    }

    msg = build_favourite_message("animes", item)

    assert "Ergo Proxy" in msg


def test_build_favourite_message_html_escape():
    item = {
        "name": "<Ergo & Proxy>",
    }

    msg = build_favourite_message("animes", item)

    assert "&lt;Ergo &amp; Proxy&gt;" in msg


def test_build_favourite_message_link():
    item = {
        "name": "Ergo Proxy",
        "url": "/animes/790-ergo-proxy",
    }

    msg = build_favourite_message("animes", item)

    assert "shikimori.io/animes/790-ergo-proxy" in msg


def test_build_favourite_message_ranobe_uses_manga_bank():
    item = {"id": 74697, "name": "Re:Zero", "russian": "Re:Zero", "url": None}
    text = messages.build_favourite_message("ranobe", item)
    manga_bank = [t.format(n=config.DISPLAY_NAME, title="Re:Zero")
                  for t in messages.MESSAGES["favourites"]["manga"]]
    assert text in manga_bank


def test_build_favourite_message_industry_uses_person_bank():
    item = {"id": 34785, "name": "Rie Takahashi", "russian": "Риэ Такахаси", "url": None}
    for cat in ("seyu", "mangakas", "producers", "people"):
        text = messages.build_favourite_message(cat, item)
        person_bank = [t.format(n=config.DISPLAY_NAME, title="Риэ Такахаси")
                       for t in messages.MESSAGES["favourites"]["person"]]
        assert text in person_bank, f"категория {cat} ушла не в банк person"
