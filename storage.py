# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Файловое хранилище ShikiUpdatesBot.

Слой персистентности: атомарная запись и загрузка JSON-состояния
(подписчики, виденные события/избранное, статистика, текущий квартал)
под DATA_DIR. Зависит только от config (пути, логгер) и utils (даты);
о доменной логике статистики не знает — она зависит от него, не наоборот.
"""

import json
from pathlib import Path

from config import (
    SEEN_FAVS_FILE,
    SEEN_IDS_FILE,
    STATS_ALL_FILE,
    STATS_CURRENT_FILE,
    SUBS_FILE,
    log,
)
from utils import _utcnow, current_quarter, quarter_start

# ═══════════════════════════════════════════════════════════════════
#  АТОМАРНАЯ ЗАПИСЬ
# ═══════════════════════════════════════════════════════════════════

def _atomic_write(path: "Path | str", data: str) -> None:
    """Атомарная запись файла: пишем во временный файл, затем rename.
    Защищает от повреждения данных при аварийном завершении процесса.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp  = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)  # атомарная операция на уровне ОС


# ═══════════════════════════════════════════════════════════════════
#  seen_ids — ВИДЕННЫЕ СОБЫТИЯ ИСТОРИИ
# ═══════════════════════════════════════════════════════════════════

def load_seen_ids() -> set[int]:
    """Загружаем уже виденные ID из JSON-файла."""
    path = Path(SEEN_IDS_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("seen_ids", []))
        except (json.JSONDecodeError, KeyError):
            log.warning("Не удалось прочитать %s, начинаем с нуля.", SEEN_IDS_FILE)
    return set()


def save_seen_ids(seen_ids: set[int]) -> None:
    """Сохраняем виденные ID в JSON-файл (атомарно)."""
    _atomic_write(
        SEEN_IDS_FILE,
        json.dumps({"seen_ids": list(seen_ids)}, ensure_ascii=False, indent=2),
    )


# ═══════════════════════════════════════════════════════════════════
#  subscribers — ПОДПИСЧИКИ
# ═══════════════════════════════════════════════════════════════════

def load_subscribers() -> dict[int, str]:
    """
    Загружаем подписчиков из JSON.
    Формат хранилища: {"subscribers": {"123456": "Имя", "789012": "Имя2"}}
    Возвращаем dict[chat_id: int, name: str].
    """
    path = Path(SUBS_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {int(k): v for k, v in data.get("subscribers", {}).items()}
        except (json.JSONDecodeError, KeyError, ValueError):
            log.warning("Не удалось прочитать %s, начинаем с пустого списка.", SUBS_FILE)
    return {}


def save_subscribers(subs: dict[int, str]) -> None:
    """Сохраняем подписчиков в JSON (атомарно)."""
    _atomic_write(
        SUBS_FILE,
        json.dumps({"subscribers": {str(k): v for k, v in subs.items()}}, ensure_ascii=False, indent=2),
    )


# ═══════════════════════════════════════════════════════════════════
#  seen_favourites — ВИДЕННОЕ ИЗБРАННОЕ
# ═══════════════════════════════════════════════════════════════════

def load_seen_favourites() -> set[str]:
    """
    Загружаем ID уже виденных записей избранного.
    Ключи хранятся как строки вида "anime_123" — категория + ID,
    чтобы избежать коллизий между разными категориями с одинаковыми ID.
    """
    path = Path(SEEN_FAVS_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("seen_favourites", []))
        except (json.JSONDecodeError, KeyError):
            log.warning("Не удалось прочитать %s, начинаем с нуля.", SEEN_FAVS_FILE)
    return set()


def save_seen_favourites(seen: set[str]) -> None:
    """Сохраняем виденные ID избранного в JSON (атомарно)."""
    _atomic_write(
        SEEN_FAVS_FILE,
        json.dumps({"seen_favourites": list(seen)}, ensure_ascii=False, indent=2),
    )


# ═══════════════════════════════════════════════════════════════════
#  stats_all.json — ЗАГРУЗКА / СОХРАНЕНИЕ (+ in-memory кэш)
# ═══════════════════════════════════════════════════════════════════

_stats_all_cache: dict | None = None
_stats_all_cache_ts: float = 0.0
_STATS_ALL_CACHE_TTL: int = 300  # секунд


def _empty_stats_all() -> dict:
    """Пустая структура stats_all.json."""
    return {
        "updated_at": None,
        "anime": {"titles": {}, "aggregates": {}},
        "manga": {"titles": {}, "aggregates": {}},
        "favourites": {"anime": [], "manga": [], "ranobe": [],
                       "characters": [], "people": []},
    }


def load_stats_all(use_cache: bool = True) -> dict:
    """
    Загружаем stats_all.json (с коротким in-memory кэшем).
    При ошибке — пустая структура, бот не падает.
    """
    global _stats_all_cache, _stats_all_cache_ts

    if use_cache and _stats_all_cache is not None:
        age = _utcnow().timestamp() - _stats_all_cache_ts
        if age < _STATS_ALL_CACHE_TTL:
            return _stats_all_cache

    data = _empty_stats_all()
    try:
        if STATS_ALL_FILE.exists():
            raw = json.loads(STATS_ALL_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "anime" in raw and "manga" in raw:
                data = raw
            else:
                log.warning("load_stats_all: неожиданная структура, сбрасываем.")
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("load_stats_all: не удалось прочитать файл: %s", e)

    _stats_all_cache = data
    _stats_all_cache_ts = _utcnow().timestamp()
    return data


def save_stats_all(data: dict) -> None:
    """Сохраняем stats_all.json атомарно + обновляем кэш."""
    global _stats_all_cache, _stats_all_cache_ts
    try:
        data["updated_at"] = _utcnow().isoformat()
        _atomic_write(STATS_ALL_FILE, json.dumps(data, ensure_ascii=False, indent=2))
        _stats_all_cache = data
        _stats_all_cache_ts = _utcnow().timestamp()
    except Exception as e:
        log.error("save_stats_all: не удалось записать файл: %s", e)


# ═══════════════════════════════════════════════════════════════════
#  stats_current.json — ТЕКУЩИЙ КВАРТАЛ
# ═══════════════════════════════════════════════════════════════════

def _empty_stats_current(period: str, tracking_since: str | None = None) -> dict:
    """
    Пустая структура текущего квартала.
    period_start — календарное начало квартала (для метки периода).
    tracking_since — реальная дата, с которой бот начал собирать события.
      При ротации = начало квартала (полные данные).
      При первом запуске в середине квартала = дата запуска (данные неполные).
      Если None — берётся календарное начало квартала.
    """
    qs = quarter_start().isoformat()
    return {
        "period": period,
        "period_start": qs,
        "tracking_since": tracking_since or qs,
        "last_report_sent": None,
        "last_backup_at": None,   # время последнего авто-бэкапа (для еженедельной отправки)
        "events": [],   # [{id, media, event, score, recorded_at}]
    }


def load_stats_current() -> dict:
    """
    Загружаем события текущего квартала. При ошибке/отсутствии — пустой квартал.

    Если файла ещё нет (истинно первый запуск), фиксируем tracking_since = max(
    начало квартала, сейчас). Это даёт честную дату «статистика собирается с …»,
    когда бота впервые запустили в середине квартала. Дата сразу сохраняется,
    чтобы не сбрасывалась при последующих перезапусках.
    """
    try:
        if STATS_CURRENT_FILE.exists():
            data = json.loads(STATS_CURRENT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "period" in data and "events" in data:
                # Бэкофилл для файлов, созданных до появления поля tracking_since
                if "tracking_since" not in data:
                    data["tracking_since"] = data.get("period_start") or quarter_start().isoformat()
                # Бэкофилл для файлов до появления last_backup_at (еженедельный авто-бэкап)
                data.setdefault("last_backup_at", None)
                return data
            log.warning("load_stats_current: неожиданная структура, сбрасываем.")
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("load_stats_current: %s", e)

    # Истинно первый запуск (или сброс) — фиксируем фактическую дату старта
    now = _utcnow()
    qs = quarter_start(now)
    tracking_since = (now if now > qs else qs).isoformat()
    fresh = _empty_stats_current(current_quarter(now), tracking_since=tracking_since)
    save_stats_current(fresh)
    log.info("load_stats_current: создан новый stats_current, отслеживание с %s.", tracking_since)
    return fresh


def save_stats_current(data: dict) -> None:
    try:
        _atomic_write(STATS_CURRENT_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        log.error("save_stats_current: %s", e)
