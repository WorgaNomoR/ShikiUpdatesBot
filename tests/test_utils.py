# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты для utils.py — чистых хелперов (даты/кварталы, HTML, приведение типов).

Покрывают модуль в изоляции: каждый ассерт обязан падать на сломанной
реализации (правка мутацией). Сетевых/IO-зависимостей нет.
"""

from datetime import datetime, timedelta

from utils import (
    _fmt_dt_short,
    _human_ago,
    _is_partial_quarter,
    _quarter_end,
    _rel_url,
    _safe_float,
    _safe_int,
    _utcnow,
    current_quarter,
    h,
    quarter_label,
    quarter_start,
    tracking_period_label,
)


# ── h: экранирование HTML ──────────────────────────────────────────
def test_h_escapes_special_chars():
    assert h("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"


def test_h_coerces_non_str():
    assert h(42) == "42"


def test_h_plain_text_unchanged():
    assert h("привет") == "привет"


# ── _rel_url: нормализация ссылок ──────────────────────────────────
def test_rel_url_strips_https_domain():
    assert _rel_url("https://shikimori.io/animes/123-name") == "/animes/123-name"


def test_rel_url_strips_http_domain():
    assert _rel_url("http://shikimori.one/mangas/5") == "/mangas/5"


def test_rel_url_already_relative_unchanged():
    assert _rel_url("/animes/7") == "/animes/7"


def test_rel_url_empty_and_none():
    assert _rel_url("") == ""
    assert _rel_url(None) == ""


def test_rel_url_strips_whitespace():
    assert _rel_url("  /animes/7  ") == "/animes/7"


def test_rel_url_domain_only_yields_empty():
    assert _rel_url("https://shikimori.io") == ""


# ── _utcnow ────────────────────────────────────────────────────────
def test_utcnow_is_naive():
    now = _utcnow()
    assert isinstance(now, datetime)
    assert now.tzinfo is None


# ── current_quarter / quarter_start ────────────────────────────────
def test_current_quarter_known_dates():
    assert current_quarter(datetime(2026, 1, 15)) == "2026-Q1"
    assert current_quarter(datetime(2026, 4, 1)) == "2026-Q2"
    assert current_quarter(datetime(2026, 9, 30)) == "2026-Q3"
    assert current_quarter(datetime(2026, 12, 31)) == "2026-Q4"


def test_quarter_start_first_day():
    assert quarter_start(datetime(2026, 5, 20)) == datetime(2026, 4, 1)
    assert quarter_start(datetime(2026, 1, 1)) == datetime(2026, 1, 1)
    assert quarter_start(datetime(2026, 11, 9)) == datetime(2026, 10, 1)


# ── quarter_label ──────────────────────────────────────────────────
def test_quarter_label_known():
    assert quarter_label("2026-Q2") == "апрель — июнь 2026"
    assert quarter_label("2026-Q4") == "октябрь — декабрь 2026"


def test_quarter_label_malformed_returns_input():
    assert quarter_label("мусор") == "мусор"


# ── _quarter_end ───────────────────────────────────────────────────
def test_quarter_end_regular_quarter():
    assert _quarter_end("2026-Q2") == datetime(2026, 6, 30)


def test_quarter_end_q4_is_dec31():
    assert _quarter_end("2026-Q4") == datetime(2026, 12, 31)


def test_quarter_end_q1():
    assert _quarter_end("2026-Q1") == datetime(2026, 3, 31)


def test_quarter_end_bad_input_none():
    assert _quarter_end("не-квартал") is None


# ── tracking_period_label ──────────────────────────────────────────
def test_tracking_period_label_full_range():
    cur = {"period": "2026-Q2", "tracking_since": "2026-04-25T10:00:00"}
    assert tracking_period_label(cur) == "с 25.04.2026 по 30.06.2026"


def test_tracking_period_label_degrades_without_dates():
    cur = {"period": "2026-Q2"}
    assert tracking_period_label(cur) == "апрель — июнь 2026"


# ── _is_partial_quarter ────────────────────────────────────────────
def test_is_partial_true_when_started_late():
    cur = {"tracking_since": "2026-04-25T00:00:00",
           "period_start": "2026-04-01T00:00:00"}
    assert _is_partial_quarter(cur) is True


def test_is_partial_false_when_from_start():
    cur = {"tracking_since": "2026-04-01T00:00:00",
           "period_start": "2026-04-01T00:00:00"}
    assert _is_partial_quarter(cur) is False


def test_is_partial_false_when_missing():
    assert _is_partial_quarter({}) is False


# ── _safe_int ──────────────────────────────────────────────────────
def test_safe_int_parses_string():
    assert _safe_int("5") == 5
    assert _safe_int(5) == 5  # int проходит насквозь


def test_safe_int_default_on_garbage():
    assert _safe_int("abc") == 0
    assert _safe_int(None) == 0
    assert _safe_int("abc", default=7) == 7


# ── _safe_float ────────────────────────────────────────────────────
def test_safe_float_parses_string():
    assert _safe_float("8.73") == 8.73
    assert _safe_float(7.5) == 7.5  # float проходит насквозь


def test_safe_float_zero_and_negative_use_default():
    assert _safe_float("0") is None
    assert _safe_float("-2") is None
    assert _safe_float("0", default=1.0) == 1.0


def test_safe_float_garbage_uses_default():
    assert _safe_float("x") is None
    assert _safe_float(None, default=2.5) == 2.5


# ── _fmt_dt_short / _human_ago: относительное время стартового снапшота ──
def test_fmt_dt_short_numeric_locale_free():
    assert _fmt_dt_short(datetime(2026, 7, 2, 14, 30)) == "02.07.2026 14:30"
    assert _fmt_dt_short(datetime(2026, 1, 9, 8, 5)) == "09.01.2026 08:05"
    assert _fmt_dt_short(datetime(2026, 12, 31, 23, 59)) == "31.12.2026 23:59"


def test_human_ago_buckets():
    now = datetime(2026, 7, 2, 15, 0, 0)
    assert _human_ago(now - timedelta(seconds=30), now) == "только что"
    assert _human_ago(now - timedelta(minutes=5), now) == "5 мин назад"
    assert _human_ago(now - timedelta(hours=2), now) == "2 ч назад"
    assert _human_ago(now - timedelta(days=3), now) == "3 д назад"


def test_human_ago_future_clamps_to_just_now():
    now = datetime(2026, 7, 2, 15, 0, 0)
    assert _human_ago(now + timedelta(hours=1), now) == "только что"
