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


def _utcnow() -> datetime:
    """Наивное UTC-время (без tzinfo) через не-устаревший API.
    Замена datetime.utcnow(), удалённой в будущих версиях Python.
    Возвращает тот же naive-UTC, что и раньше — форматы хранения и
    сравнения дат не меняются.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    except Exception:
        pass
    return quarter_label(period)


def _is_partial_quarter(cur: dict) -> bool:
    """True, если отслеживание началось позже календарного начала квартала."""
    try:
        ts = cur.get("tracking_since")
        ps = cur.get("period_start")
        if ts and ps:
            return datetime.fromisoformat(ts) > datetime.fromisoformat(ps)
    except Exception:
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
