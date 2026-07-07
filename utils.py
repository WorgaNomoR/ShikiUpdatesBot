# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Чистые утилиты ShikiUpdatesBot.

Изолированный модуль нижнего уровня: ничего не импортирует из проекта
(только stdlib). Зависимости — односторонние: любой модуль может тянуть
отсюда хелперы, сам utils не знает ни о config, ни о ком-либо ещё.

Содержимое:
  • h / _rel_url            — экранирование HTML и нормализация ссылок;
  • _utcnow / quarter_*      — работа с датами и кварталами;
  • _safe_int / _safe_float  — аккуратное приведение типов из API.
"""

import html
import re
from datetime import datetime, timedelta, timezone


def h(text: str) -> str:
    """Экранируем спецсимволы HTML — защита от поломки разметки в Telegram.
    Экранирует: & -> &amp;  < -> &lt;  > -> &gt;
    Применять ко всем пользовательским данным из API перед вставкой в сообщение.
    """
    return html.escape(str(text))


def _rel_url(url: str) -> str:
    """
    Приводим URL к относительному виду ('/animes/123-name').

    GraphQL Shikimori отдаёт ПОЛНЫЙ url ('https://shikimori.io/animes/...'),
    а REST history — относительный. Весь код формирования ссылок приклеивает
    SHIKI_BASE_URL спереди, поэтому полный url давал бы двойной домен и битую
    ссылку. Нормализуем к относительному при сохранении — один источник истины.
    """
    url = (url or "").strip()
    if not url:
        return ""
    # Отрезаем схему+домен, если url полный
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            rest = url[len(prefix):]
            slash = rest.find("/")
            return rest[slash:] if slash != -1 else ""
    return url


# Латинские буквы, визуально неотличимые от кириллических (омоглифы).
# Значения — соответствующая кириллица. Регистр держим отдельными парами.
_HOMOGLYPH_MAP = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р",
    "x": "х", "y": "у", "k": "к",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
    "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х",
}

# Одиночные латинские токены, которые на деле — русские предлоги-связки.
# Только они чинятся вне mixed-script контекста (см. _normalize_homoglyphs).
_STANDALONE_HOMOGLYPHS = {"c": "с", "o": "о"}

# Токен — максимальный ран латиницы+кириллицы; цифры/пунктуация рвут его,
# поэтому предлог «c» между пробелами становится отдельным токеном.
_TOKEN_RE = re.compile(r"[A-Za-z\u0400-\u04FF]+")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


def _normalize_homoglyphs(text: str) -> str:
    """Латинские двойники кириллицы → кириллица перед русским матчингом.

    Shikimori подмешивает латинские буквы-омоглифы в русские строки истории
    (латинская "c" U+0063 вместо кириллической "с" в «оценка c 8 на 9»), и
    кириллические регулярки промахиваются. Один общий проход вместо
    посимвольных [xy]-классов на каждом стыке (issue #28).

    Скоуп — чтобы не испортить легитимную латиницу (english-форматы
    "rated"/"scored", английские названия, URL):
      • омоглифы внутри mixed-script токена (в токене уже есть кириллица)
        чинятся поголовно — слово заведомо русское;
      • одиночная латинская связка-предлог (c→с, o→о) чинится по белому
        списку;
      • чисто латинский многобуквенный токен не трогаем.
    """
    def _repair(match: "re.Match[str]") -> str:
        token = match.group(0)
        if len(token) == 1:
            # Одиночный токен: чиним только известные связки-предлоги.
            return _STANDALONE_HOMOGLYPHS.get(token, token)
        if _CYRILLIC_RE.search(token):
            # Mixed-script: в слове есть кириллица — латинские двойники в нём
            # заведомо мусор, меняем все.
            return "".join(_HOMOGLYPH_MAP.get(ch, ch) for ch in token)
        # Чисто латинский токен — оставляем как есть.
        return token

    return _TOKEN_RE.sub(_repair, text)


def _utcnow() -> datetime:
    """Наивное UTC-время (без tzinfo) через не-устаревший API.
    Замена datetime.utcnow(), удалённой в будущих версиях Python.
    Возвращает тот же naive-UTC, что и раньше — форматы хранения и
    сравнения дат не меняются.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fmt_dt_short(dt: datetime) -> str:
    """Дата-время в numeric-форме '02.07.2026 14:30' — локаль-независимо и в
    той же конвенции, что футер свежести в build_stats_all_messages (%d.%m.%Y).
    Человеко-читаемость даёт соседнее '(N назад)' от _human_ago."""
    return dt.strftime("%d.%m.%Y %H:%M")


def _human_ago(dt: datetime, now: datetime | None = None) -> str:
    """Грубое «сколько назад» относительно now (по умолчанию _utcnow):
    'только что' / 'N мин назад' / 'N ч назад' / 'N д назад'. Оба времени —
    наивный UTC (как _utcnow). Отрицательная разница (часы вперёд) — 'только что'."""
    now = now or _utcnow()
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "только что"
    if secs < 3600:
        return f"{int(secs // 60)} мин назад"
    if secs < 86400:
        return f"{int(secs // 3600)} ч назад"
    return f"{int(secs // 86400)} д назад"


def current_quarter(dt: datetime | None = None) -> str:
    """'2026-Q2' для UTC-даты (по умолчанию — сейчас)."""
    if dt is None:
        dt = _utcnow()
    q = (dt.month - 1) // 3 + 1
    return f"{dt.year}-Q{q}"


def quarter_start(dt: datetime | None = None) -> datetime:
    """Первый день текущего (или переданного) квартала, UTC."""
    if dt is None:
        dt = _utcnow()
    q = (dt.month - 1) // 3 + 1
    return datetime(dt.year, (q - 1) * 3 + 1, 1)


def quarter_label(period: str) -> str:
    """'2026-Q2' -> 'апрель — июнь 2026'. При ошибке возвращает исходную строку."""
    _names = {
        "Q1": "январь — март",
        "Q2": "апрель — июнь",
        "Q3": "июль — сентябрь",
        "Q4": "октябрь — декабрь",
    }
    try:
        year, q = period.split("-", 1)
        return f"{_names.get(q, q)} {year}"
    except Exception:
        return period


def _quarter_end(period: str) -> datetime | None:
    """Последний день квартала по строке '2026-Q2' (для отображения диапазона)."""
    try:
        year_s, q_s = period.split("-Q", 1)
        year, q = int(year_s), int(q_s)
        # Первый месяц следующего квартала минус один день
        end_month = q * 3  # последний месяц квартала (3,6,9,12)
        if end_month == 12:
            return datetime(year, 12, 31)
        return datetime(year, end_month + 1, 1) - timedelta(days=1)
    except Exception:
        return None


def tracking_period_label(cur: dict) -> str:
    """
    Человекочитаемый диапазон фактического отслеживания текущего квартала.
    'с 25.04.2026 по 30.06.2026' если бот стартовал в середине,
    'с 01.04.2026 по 30.06.2026' если с начала.
    При проблемах с датами — деградирует до quarter_label.
    """
    period = cur.get("period") or current_quarter()
    try:
        ts_raw = cur.get("tracking_since") or cur.get("period_start")
        start = datetime.fromisoformat(ts_raw) if ts_raw else None
        end = _quarter_end(period)
        if start and end:
            return f"с {start.strftime('%d.%m.%Y')} по {end.strftime('%d.%m.%Y')}"
    except (ValueError, TypeError):
        pass
    return quarter_label(period)


def _is_partial_quarter(cur: dict) -> bool:
    """True, если отслеживание началось позже календарного начала квартала."""
    try:
        ts = cur.get("tracking_since")
        ps = cur.get("period_start")
        if ts and ps:
            return datetime.fromisoformat(ts) > datetime.fromisoformat(ps)
    except (ValueError, TypeError):
        pass
    return False


def _safe_int(value, default: int = 0) -> int:
    """Аккуратно приводим к int — score/episodes из экспорта бывают строками."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float | None = None) -> float | None:
    """Приводим к float — GraphQL score приходит как строка '8.73'. None если не вышло."""
    try:
        f = float(value)
        return f if f > 0 else default
    except (TypeError, ValueError):
        return default
