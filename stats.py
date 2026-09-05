# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Статистика ShikiUpdatesBot.

Доменный слой: агрегирование списков, синхронизация stats_all, события
текущего квартала, снапшоты кварталов, построение типизированных отчётов
(/stats, /favs, квартальный). Зависит от config/utils/storage/shiki_api,
messages/report_model; знают о нём только хендлеры.
"""

import json
import random
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp

from config import (
    QUARTERS_DIR,
    SHIKI_BASE_URL,
    log,
)
from messages import (
    _avg_score_from_dist,
    _pct_diff,
)
from report_model import (
    Bold,
    Italic,
    Line,
    Link,
    Report,
    Row,
    Rows,
    Section,
    Text,
    Unit,
    line,
    section,
    unit,
)
from shiki_api import (
    _STAT_STATUSES,
    RANOBE_KINDS,
    ProfilePrivacyError,
    fetch_favourites,
    fetch_list_export,
    fetch_meta_batch,
    is_relevant,
)
from storage import (
    _atomic_write,
    load_stats_all,
    save_stats_all,
)
from utils import (
    _is_partial_quarter,
    _parse_iso_utc,
    _rel_url,
    _safe_int,
    _utcnow,
    quarter_label,
    tracking_period_label,
)

# ═══════════════════════════════════════════════════════════════════
#  СТАТИСТИКА: АГРЕГАЦИЯ, СИНХРОНИЗАЦИЯ, ОТЧЁТЫ, РОТАЦИЯ КВАРТАЛА
# ═══════════════════════════════════════════════════════════════════

# Локализация kind для разбивки в шапке статистики
_KIND_RU_ANIME: dict[str, str] = {
    "tv":    "Сериалы",
    "movie": "Фильмы",
    "ova":   "OVA",
    "ona":   "ONA",
}

_KIND_RU_MANGA: dict[str, str] = {
    "manga":       "Манга",
    "manhwa":      "Манхва",
    "manhua":      "Маньхуа",
    "light_novel": "Ранобэ",
    "novel":       "Новеллы",
    "ranobe":      "Ранобэ",
}

PICK_CATEGORY_ANIME = "anime"
PICK_CATEGORY_MANGA = "manga"
PICK_CATEGORY_RANOBE = "ranobe"
PICK_CATEGORY_UNKNOWN = "unknown"
_MANGA_PRESENTATION_KINDS: frozenset[str] = frozenset({
    "manga",
    "manhwa",
    "manhua",
})

_META_ACTIVE_STATUSES: frozenset[str] = frozenset({
    "planned",
    "watching",
    "rewatching",
    "on_hold",
})
_META_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "completed",
    "dropped",
})
_META_ACTIVE_MAX_AGE = timedelta(days=7)
_META_TERMINAL_MAX_AGE = timedelta(days=30)
_META_MAINTENANCE_LIMIT = 50


@dataclass(frozen=True)
class PickCandidate:
    """Нормализованный локальный кандидат для меню /pick."""

    id: str
    category: str
    title: str
    url: str
    year: int | None
    genres: tuple[str, ...]
    title_en: str = ""
    poster_url: str = ""
    kind: str = ""
    shiki_score: float | None = None
    themes: tuple[str, ...] = ()
    demographic: tuple[str, ...] = ()
    episodes_total: int | None = None
    duration: int | None = None
    rating: str = ""
    origin: str = ""
    studios: tuple[str, ...] = ()
    chapters_total: int | None = None
    volumes_total: int | None = None
    publishers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PickCatalog:
    """Классифицированный planned-срез одного stats_all snapshot."""

    anime: tuple[PickCandidate, ...]
    manga: tuple[PickCandidate, ...]
    ranobe: tuple[PickCandidate, ...]
    unresolved_count: int
    updated_at: str | None

    def candidates_for(self, category: str) -> tuple[PickCandidate, ...]:
        """Вернуть неизменяемый пул известной пользовательской категории."""
        if category == PICK_CATEGORY_ANIME:
            return self.anime
        if category == PICK_CATEGORY_MANGA:
            return self.manga
        if category == PICK_CATEGORY_RANOBE:
            return self.ranobe
        return ()


@dataclass(frozen=True)
class PickSelection:
    """Результат выбора и новое состояние цикла без повторов."""

    candidate: PickCandidate | None
    shown_ids: frozenset[str]
    pool_reset: bool


def classify_manga_presentation_kind(kind: object) -> str:
    """Классифицировать manga-domain kind без догадок и побочных эффектов."""
    if not isinstance(kind, str):
        return PICK_CATEGORY_UNKNOWN
    normalized = kind.strip().lower()
    if normalized in RANOBE_KINDS:
        return PICK_CATEGORY_RANOBE
    if normalized in _MANGA_PRESENTATION_KINDS:
        return PICK_CATEGORY_MANGA
    return PICK_CATEGORY_UNKNOWN


def _pick_year(value: object) -> int | None:
    """Оставить только пригодный для десятилетия календарный год."""
    if type(value) is int and 1 <= value <= 9999:
        return value
    return None


def _pick_positive_int(value: object) -> int | None:
    """Оставить только положительное целое локальное значение."""
    if type(value) is int and value > 0:
        return value
    return None


def _pick_score(value: object) -> float | None:
    """Оставить только правдоподобную оценку Shikimori."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        score = float(value)
        if 0 < score <= 10:
            return score
    return None


def _pick_text(value: object) -> str:
    """Нормализовать необязательное локальное текстовое поле."""
    return value.strip() if isinstance(value, str) else ""


def _pick_genres(value: object) -> tuple[str, ...]:
    """Нормализовать жанры, сохранив исходный порядок и регистр."""
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for raw_genre in value:
        if not isinstance(raw_genre, str):
            continue
        genre = raw_genre.strip()
        key = genre.casefold()
        if not genre or key in seen:
            continue
        seen.add(key)
        result.append(genre)
    return tuple(result)


def _pick_candidate(
    title_id: object,
    record: object,
    category: str,
) -> PickCandidate | None:
    """Построить безопасного кандидата из одной title record."""
    if not _pick_record_is_eligible(record):
        return None
    candidate_id = str(title_id).strip()
    if not candidate_id:
        return None
    raw_title = record.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    raw_url = record.get("url")
    return PickCandidate(
        id=candidate_id,
        category=category,
        title=title or "Без названия",
        url=raw_url.strip() if isinstance(raw_url, str) else "",
        year=_pick_year(record.get("year")),
        genres=_pick_genres(record.get("genres")),
        title_en=_pick_text(record.get("title_en")),
        poster_url=_pick_text(record.get("poster_url")),
        kind=_pick_text(record.get("kind")).lower(),
        shiki_score=_pick_score(record.get("shiki_score")),
        themes=_pick_genres(record.get("themes")),
        demographic=_pick_genres(record.get("demographic")),
        episodes_total=_pick_positive_int(record.get("episodes_total")),
        duration=_pick_positive_int(record.get("duration")),
        rating=_pick_text(record.get("rating")),
        origin=_pick_text(record.get("origin")),
        studios=_pick_genres(record.get("studios")),
        chapters_total=_pick_positive_int(record.get("chapters_total")),
        volumes_total=_pick_positive_int(record.get("volumes_total")),
        publishers=_pick_genres(record.get("publishers")),
    )


def _pick_record_is_eligible(record: object) -> bool:
    """Оставить planned-запись, если она не помечена точным анонсом."""
    if not isinstance(record, dict) or record.get("status") != "planned":
        return False
    release_status = record.get("release_status")
    return not (
        isinstance(release_status, str)
        and release_status.strip().lower() == "anons"
    )


def build_pick_catalog(stats_all: object) -> PickCatalog | None:
    """Собрать planned-каталог или вернуть None для непригодной структуры."""
    if not isinstance(stats_all, dict):
        return None
    sections: dict[str, dict] = {}
    for media in (PICK_CATEGORY_ANIME, PICK_CATEGORY_MANGA):
        section = stats_all.get(media)
        if not isinstance(section, dict) or not isinstance(section.get("titles"), dict):
            return None
        sections[media] = section["titles"]

    anime: list[PickCandidate] = []
    manga: list[PickCandidate] = []
    ranobe: list[PickCandidate] = []
    unresolved_count = 0

    for title_id, record in sections[PICK_CATEGORY_ANIME].items():
        candidate = _pick_candidate(title_id, record, PICK_CATEGORY_ANIME)
        if candidate is not None:
            anime.append(candidate)

    for title_id, record in sections[PICK_CATEGORY_MANGA].items():
        if not _pick_record_is_eligible(record):
            continue
        category = classify_manga_presentation_kind(record.get("kind"))
        if category == PICK_CATEGORY_UNKNOWN:
            unresolved_count += 1
            continue
        candidate = _pick_candidate(title_id, record, category)
        if candidate is None:
            continue
        if category == PICK_CATEGORY_MANGA:
            manga.append(candidate)
        else:
            ranobe.append(candidate)

    updated_at = stats_all.get("updated_at")
    return PickCatalog(
        anime=tuple(anime),
        manga=tuple(manga),
        ranobe=tuple(ranobe),
        unresolved_count=unresolved_count,
        updated_at=updated_at if isinstance(updated_at, str) else None,
    )


def _pick_remaining(
    candidates: tuple[PickCandidate, ...],
    shown_ids: frozenset[str],
) -> tuple[tuple[PickCandidate, ...], frozenset[str], bool]:
    """Вернуть непоказанный остаток или начать новый цикл."""
    remaining = tuple(candidate for candidate in candidates if candidate.id not in shown_ids)
    if remaining:
        return remaining, shown_ids, False
    return candidates, frozenset(), bool(candidates)


def select_pick_candidate(
    candidates: tuple[PickCandidate, ...],
    shown_ids: object = (),
) -> PickSelection:
    """Равномерно выбрать непоказанный вариант и безопасно сбросить цикл."""
    shown = frozenset(str(item) for item in shown_ids) if shown_ids else frozenset()
    remaining, cycle_shown, pool_reset = _pick_remaining(candidates, shown)
    if not remaining:
        return PickSelection(None, shown, False)
    candidate = random.choice(remaining)  # nosec B311  (выбор рекомендации — не крипта)
    return PickSelection(candidate, cycle_shown | {candidate.id}, pool_reset)


def _pick_decade(candidate: PickCandidate) -> int | None:
    """Вернуть начало десятилетия только при известном годе."""
    if candidate.year is None:
        return None
    return candidate.year // 10 * 10


def _pick_genre_keys(candidate: PickCandidate) -> frozenset[str]:
    """Подготовить регистронезависимое множество жанров для сравнения."""
    return frozenset(genre.casefold() for genre in candidate.genres)


def select_contrast_pick_candidate(
    candidates: tuple[PickCandidate, ...],
    anchor: PickCandidate,
    shown_ids: object = (),
) -> PickSelection:
    """Выбрать контраст: другое десятилетие, затем минимум общих жанров."""
    shown = frozenset(str(item) for item in shown_ids) if shown_ids else frozenset()
    remaining, cycle_shown, pool_reset = _pick_remaining(candidates, shown)
    if not remaining:
        return PickSelection(None, shown, False)

    if len(candidates) > 1:
        without_anchor = tuple(item for item in remaining if item.id != anchor.id)
        if not without_anchor and pool_reset:
            without_anchor = tuple(item for item in candidates if item.id != anchor.id)
        if without_anchor:
            remaining = without_anchor

    anchor_decade = _pick_decade(anchor)
    if anchor_decade is not None:
        different_decade = tuple(
            item
            for item in remaining
            if _pick_decade(item) is not None and _pick_decade(item) != anchor_decade
        )
        if different_decade:
            remaining = different_decade

    anchor_genres = _pick_genre_keys(anchor)
    if anchor_genres:
        known_genres = tuple(item for item in remaining if item.genres)
        if known_genres:
            overlaps = {
                item.id: len(anchor_genres & _pick_genre_keys(item))
                for item in known_genres
            }
            minimum = min(overlaps.values())
            remaining = tuple(item for item in known_genres if overlaps[item.id] == minimum)

    candidate = random.choice(remaining)  # nosec B311  (выбор рекомендации — не крипта)
    return PickSelection(candidate, cycle_shown | {candidate.id}, pool_reset)


def _merge_title_record(
    media: str,
    export_row: dict,
    meta: dict | None,
    previous: dict | None = None,
    meta_updated_at: str | None = None,
) -> dict:
    """
    Собираем одну запись titles{} из строки экспорта и метаданных GraphQL.
    meta может быть None — тогда метаданные пустые, но пользовательские данные есть.
    При ремонте сохраняем уже известный release_status, если свежий ответ пуст.
    """
    meta = meta or {}
    previous = previous or {}
    record = {
        "title":    export_row.get("target_title_ru") or export_row.get("target_title") or "???",
        "title_en": export_row.get("target_title") or "",
        "score":    _safe_int(export_row.get("score")),       # 0 = без оценки
        "status":   (export_row.get("status") or "").lower(),
        "rewatches": _safe_int(export_row.get("rewatches")),
        "url":       meta.get("url") or "",
        "poster_url": meta.get("poster_url") or "",
        "kind":      meta.get("kind") or "",
        "release_status": meta.get("release_status") or previous.get("release_status") or "",
        "year":      meta.get("year"),
        "shiki_score": meta.get("shiki_score"),
        "genres":      meta.get("genres") or [],
        "themes":      meta.get("themes") or [],
        "demographic": meta.get("demographic") or [],
    }
    if media == "anime":
        record.update({
            "episodes_watched": _safe_int(export_row.get("episodes")),
            "episodes_total":   meta.get("episodes_total"),
            "duration":         meta.get("duration"),
            "rating":           meta.get("rating"),
            "origin":           meta.get("origin"),
            "studios":          meta.get("studios") or [],
        })
    else:
        record.update({
            "chapters_read":  _safe_int(export_row.get("chapters")),
            "volumes_read":   _safe_int(export_row.get("volumes")),
            "chapters_total": meta.get("chapters_total"),
            "volumes_total":  meta.get("volumes_total"),
            "publishers":     meta.get("publishers") or [],
        })
    if meta_updated_at is not None:
        record["meta_updated_at"] = meta_updated_at
    return record


def _metadata_id_sort_key(title_id: str) -> tuple[int, int, str]:
    """Устойчивый порядок числовых ID с защитным строковым фолбэком."""
    try:
        return 0, int(title_id), title_id
    except ValueError:
        return 1, 0, title_id


def _select_metadata_refresh_ids(
    valid_rows: dict[str, dict],
    titles: dict[str, dict],
    now: datetime,
) -> list[str]:
    """Выбрать oldest-first кандидатов одного media-домена в фиксированный бюджет."""
    candidates: list[tuple[datetime, tuple[int, int, str], str]] = []
    for title_id, row in valid_rows.items():
        record = titles.get(title_id)
        if not isinstance(record, dict) or not (record.get("kind") or ""):
            continue

        status = (row.get("status") or "").lower()
        if status in _META_ACTIVE_STATUSES:
            max_age = _META_ACTIVE_MAX_AGE
        elif status in _META_TERMINAL_STATUSES:
            max_age = _META_TERMINAL_MAX_AGE
        else:
            continue

        updated_at = _parse_iso_utc(record.get("meta_updated_at"))
        if updated_at is not None and updated_at > now:
            updated_at = None
        if updated_at is not None and now - updated_at < max_age:
            continue

        candidates.append((
            updated_at or datetime.min,
            _metadata_id_sort_key(title_id),
            title_id,
        ))

    candidates.sort()
    return [
        title_id
        for _, _, title_id in candidates[:_META_MAINTENANCE_LIMIT]
    ]


def _bump(counter: dict, key, n: int = 1) -> None:
    """counter[key] += n, с защитой от None/пустых ключей."""
    if key is None or key == "":
        return
    counter[str(key)] = counter.get(str(key), 0) + n


def recompute_aggregates(media: str, titles: dict, existing_by_quarter: dict | None = None) -> dict:
    """
    Полный пересчёт агрегатов из titles{}.
    by_quarter не вычисляется отсюда (он накапливается при ротации квартала) —
    передаём существующий, чтобы не потерять.

    Все жанрово-оценочные агрегаты считаются ТОЛЬКО по completed.
    Счётчики статусов (total_*) — по всем записям.
    """
    agg: dict = {
        "total_completed":  0,
        "total_dropped":    0,
        "total_watching":   0,
        "total_planned":    0,
        "total_on_hold":    0,
        "total_rewatching": 0,
        "score_dist":   {},
        "genres":       {},
        "themes":       {},
        "demographic":  {},
        "kinds":        {},
        "by_year":      {},
        "by_quarter":   existing_by_quarter or {},
        "avg_shiki_completed": None,  # средний рейтинг Shikimori по завершённым с оценкой
    }
    if media == "anime":
        agg.update({
            "studios": {}, "origins": {}, "ratings": {},
            "total_episodes_watched": 0,
            "total_hours_watched":    0.0,
        })
    else:
        agg.update({
            "publishers": {},
            "total_chapters_read": 0,
            "total_volumes_read":  0,
        })

    status_counter = {
        "completed":  "total_completed",
        "dropped":    "total_dropped",
        "watching":   "total_watching",
        "planned":    "total_planned",
        "on_hold":    "total_on_hold",
        "rewatching": "total_rewatching",
    }

    total_minutes = 0
    shiki_scores: list[float] = []   # рейтинги Shikimori по completed с личной оценкой

    for rec in titles.values():
        status = rec.get("status", "")
        # Счётчик статусов
        if status in status_counter:
            agg[status_counter[status]] += 1

        # Жанровые/оценочные агрегаты — только completed
        if status != "completed":
            continue

        _bump(agg["score_dist"], rec.get("score", 0))
        # Рейтинг Shikimori — собираем только если есть личная оценка (для честного сравнения)
        if _safe_int(rec.get("score")) > 0 and isinstance(rec.get("shiki_score"), (int, float)):
            shiki_scores.append(float(rec["shiki_score"]))
        for g in rec.get("genres", []):
            _bump(agg["genres"], g)
        for t in rec.get("themes", []):
            _bump(agg["themes"], t)
        for d in rec.get("demographic", []):
            _bump(agg["demographic"], d)
        _bump(agg["kinds"], rec.get("kind"))
        if rec.get("year"):
            _bump(agg["by_year"], rec.get("year"))

        if media == "anime":
            for s in rec.get("studios", []):
                _bump(agg["studios"], s)
            _bump(agg["origins"], rec.get("origin"))
            _bump(agg["ratings"], rec.get("rating"))

            eps = _safe_int(rec.get("episodes_watched"))
            agg["total_episodes_watched"] += eps
            dur = rec.get("duration")
            if isinstance(dur, int) and dur > 0 and eps > 0:
                total_minutes += dur * eps
        else:
            for p in rec.get("publishers", []):
                _bump(agg["publishers"], p)
            agg["total_chapters_read"] += _safe_int(rec.get("chapters_read"))
            agg["total_volumes_read"]  += _safe_int(rec.get("volumes_read"))

    if media == "anime":
        agg["total_hours_watched"] = round(total_minutes / 60, 1)

    if shiki_scores:
        agg["avg_shiki_completed"] = round(sum(shiki_scores) / len(shiki_scores), 2)

    return agg


# Sentinel «аргумент fav не передан» — отличаем от явного None. None означает
# «избранное уже пытались получить в этом цикле и оно недоступно» → НЕ рефетчим
# (иначе на упавшем цикле бьём эндпоинт повторно — анти-паттерн для rate-limit);
# _UNSET означает «прямой/standalone-вызов, фетчим сами».
_UNSET = object()


async def _collect_favourites(
    session: "aiohttp.ClientSession | None",
    stats: dict,
    fav=_UNSET,
) -> dict:
    """
    Собирает избранное в структуру stats["favourites"].

    fav: готовый ответ API (уже скачанный в цикле) — используем и НЕ ходим в
    сеть повторно. fav=_UNSET (не передан, standalone-вызов) — фетчим сами через
    session. fav=None (передан явно = «в этом цикле избранное недоступно») —
    оставляем прежнее, БЕЗ повторного фетча.

    Для аниме/манги/ранобэ джойнит оценку и название из titles{} (если тайтл
    там есть); если нет — берёт название из ответа API. Персонажи/люди —
    имя+ссылка из API (в titles{} их нет, ссылки/оценки не будет — это ок).

    fetch_favourites возвращает None при сбое — тогда оставляем прежнее
    избранное (не затираем хорошие данные пустотой при ошибке сети).

    Категоризация Shikimori ненадёжна (режиссёры лежат в mangakas, и т.п.),
    поэтому people+mangakas+seyu+producers сливаем в один блок "people"
    («Люди индустрии»). Ранобэ — отдельный блок, но джойнит по namespace манги.
    """
    if fav is _UNSET:
        if session is None:
            # Защита: fetch_favourites(None) упал бы внутри на session.get(...).
            # В норме не случается (sync_stats_all передаёт session,
            # check_and_notify_favourites — готовый fav).
            log.error("_collect_favourites: fav не передан, а session=None — оставляем прежнее.")
            return stats
        fav = await fetch_favourites(session)
    if fav is None:
        # Либо фетч вернул None, либо явно передали None (недоступно в цикле) —
        # в обоих случаях оставляем прежнее, повторно НЕ фетчим.
        log.info("_collect_favourites: избранное недоступно — оставляем прежнее.")
        return stats

    # API-категория → (выходной ключ stats, ключ titles для джойна или None).
    # ranobe джойнит по titles манги: id ранобэ лежат в namespace манги,
    # и если тайтл есть в списке пользователя — подтянем ссылку/оценку.
    cat_map = {
        "animes":     ("anime",      "anime"),
        "mangas":     ("manga",      "manga"),
        "ranobe":     ("ranobe",     "manga"),
        "characters": ("characters", None),
        "people":     ("people",     None),
        "mangakas":   ("people",     None),
        "seyu":       ("people",     None),
        "producers":  ("people",     None),
    }

    result: dict[str, list] = {
        "anime": [], "manga": [], "ranobe": [], "characters": [], "people": [],
    }
    # Защита от дублей в слитом блоке людей (на случай, если Shikimori положит
    # одного человека в несколько категорий — в норме не случается).
    seen_people: set[str] = set()

    for api_cat, (out_key, media_key) in cat_map.items():
        items = fav.get(api_cat) or []
        titles = stats.get(media_key, {}).get("titles", {}) if media_key else {}
        for item in items:
            iid = item.get("id")
            if iid is None:
                continue
            tid = str(iid)
            if out_key == "people":
                if tid in seen_people:
                    continue
                seen_people.add(tid)
            # russian бывает пустой строкой (не null) — фолбэк на name,
            # иначе получим пустую жирную строку.
            api_name = item.get("russian") or item.get("name") or "???"
            api_url = _rel_url(item.get("url"))

            if media_key and tid in titles:
                # Джойн с архивом: берём название и оценку оттуда
                rec = titles[tid]
                entry = {
                    "id": tid,
                    "title": rec.get("title") or api_name,
                    "url": _rel_url(rec.get("url")) or api_url,
                }
                score = _safe_int(rec.get("score"))
                if score > 0:
                    entry["score"] = score
            else:
                # Нет в архиве (или персонаж/человек) — только имя+ссылка
                entry = {"id": tid, "title": api_name, "url": api_url}
            result[out_key].append(entry)

    stats["favourites"] = result
    counts = {k: len(v) for k, v in result.items() if v}
    log.info("_collect_favourites: собрано избранное: %s", counts or "пусто")
    return stats


async def sync_stats_all(
    session: "aiohttp.ClientSession | None" = None,
    fav=_UNSET,
) -> tuple[dict, bool]:
    """
    Главная функция актуализации stats_all.

    1. Скачиваем list_export для аниме и манги.
    2. Сверяем с titles{} в stats_all — находим новые/изменившиеся записи
       (новый id, либо изменился score/status/episodes/chapters).
    3. Новые, безвидовые и повреждённые записи обогащаем одним correctness-
       батчем GraphQL. Для остальных обновляем пользовательский стейт из экспорта.
    4. Отдельно обновляем до 50 самых старых метаданных каждого media-домена.
    5. Пересчитываем агрегаты, сохраняем.

    Вызывается при старте бота и периодически из цикла. Уведомлений не шлёт.
    Возвращает (stats_all, ok): ok=False, если ни один экспорт не скачался
    (тогда stats_all — прежний, нетронутый); ok=True при частичном/полном успехе.
    """
    # boot-throttle: переданную сессию переиспользуем (одну на весь старт),
    # иначе открываем свою короткоживущую и рекурсивно прогоняем тело.
    if session is None:
        async with aiohttp.ClientSession() as own:
            return await sync_stats_all(session=own, fav=fav)

    stats = load_stats_all(use_cache=False)

    export_anime = await fetch_list_export(session, "anime")
    export_manga = await fetch_list_export(session, "manga")

    if export_anime is None and export_manga is None:
        log.warning("sync_stats_all: оба экспорта недоступны — пропускаем синхронизацию.")
        return stats, False

    # load_stats_all обновляет процессный кэш и возвращает тот же объект.
    # Изолируем рабочие изменения, чтобы поздний privacy failure не опубликовал
    # частичный результат через кэш до атомарного сохранения.
    stats = deepcopy(stats)
    sync_time = _utcnow()
    meta_updated_at = sync_time.isoformat()

    changed = False

    for media, export in (("anime", export_anime), ("manga", export_manga)):
        if export is None:
            log.info("sync_stats_all: экспорт %s недоступен, пропускаем эту половину.", media)
            continue

        titles = stats[media]["titles"]

        # Релевантные строки экспорта: с валидным id и известным статусом
        valid_rows: dict[str, dict] = {}
        for row in export:
            tid = str(row.get("target_id") or "")
            status = (row.get("status") or "").lower()
            if tid and status in _STAT_STATUSES:
                valid_rows[tid] = row

        # ID, которым нужны метаданные (отсутствуют в titles)
        new_ids = [tid for tid in valid_rows if tid not in titles]

        # Синтаксически валидный JSON может хранить вместо title-record любое
        # значение. Чиним только записи актуального экспорта; отсутствующие ID
        # ниже удалит обычная очистка без попытки разобрать их содержимое.
        malformed_ids = [
            tid for tid in valid_rows
            if tid in titles and not isinstance(titles[tid], dict)
        ]

        # Ремонт битой меты (Codacy / баг «ваншоты не фильтруются»):
        # записи с пустым kind — это тайтлы, у которых мета не доехала при
        # первом заносе (GraphQL не вернул элемент → ВСЕ мета-поля пусты разом).
        # Они навсегда оставались в titles{} (new_ids их не видит, самоочистка
        # пропускает пустой kind) и, если completed, врали в счётчике.
        # Дозапрашиваем их повторно. При новом точном release_status обновляем
        # запись даже с пустым kind; полностью повторный ответ остаётся no-op.
        retry_ids = [
            tid for tid in valid_rows
            if (
                tid in titles
                and isinstance(titles[tid], dict)
                and not (titles[tid].get("kind") or "")
            )
        ]

        # Новые записи и оба вида ремонта — correctness-работа: один общий
        # запрос всегда идёт отдельно и не расходует maintenance-батч.
        need_meta = new_ids + retry_ids + malformed_ids
        meta_map: dict[str, dict] = {}
        if need_meta:
            log.info(
                "sync_stats_all(%s): тайтлов для обогащения: %d "
                "(новых %d, kind-ремонт %d, повреждённых %d)",
                media,
                len(need_meta),
                len(new_ids),
                len(retry_ids),
                len(malformed_ids),
            )
            try:
                meta_map = await fetch_meta_batch(media, need_meta, session=session)
            except ProfilePrivacyError:
                raise
            except Exception as e:
                log.error("sync_stats_all(%s): fetch_meta_batch упал: %s", media, e)

        maintenance_ids = _select_metadata_refresh_ids(
            valid_rows,
            titles,
            sync_time,
        )
        maintenance_meta: dict[str, dict] = {}
        if maintenance_ids:
            log.info(
                "sync_stats_all(%s): планово обновляем метаданные: %d",
                media,
                len(maintenance_ids),
            )
            try:
                maintenance_meta = await fetch_meta_batch(
                    media,
                    maintenance_ids,
                    session=session,
                )
            except ProfilePrivacyError:
                raise
            except Exception as e:
                log.error(
                    "sync_stats_all(%s): maintenance fetch_meta_batch упал: %s",
                    media,
                    e,
                )

        skipped_irrelevant = 0
        repaired = 0

        # Обновляем / создаём записи
        for tid, row in valid_rows.items():
            if tid in titles:
                rec = titles[tid]
                fresh = meta_map.get(tid)

                # Непригодное значение заменяем канонической записью в любом
                # случае. Только реально полученная мета заслуживает timestamp;
                # missing-kind фолбэк останется correctness-кандидатом.
                if not isinstance(rec, dict):
                    applied_meta = fresh if isinstance(fresh, dict) else None
                    titles[tid] = _merge_title_record(
                        media,
                        row,
                        applied_meta,
                        meta_updated_at=(
                            meta_updated_at if applied_meta is not None else None
                        ),
                    )
                    changed = True
                    continue

                # Ремонт битой меты: запись с пустым kind, и сейчас GraphQL
                # вернул непустой kind → пересобираем ЦЕЛИКОМ (url/year/жанры/
                # kind — всё, что побилось вместе с kind). Дальнейшая
                # самоочистка по kind вынесет ставшие нерелевантными (ваншоты).
                # Если kind снова пуст, сохраняем только впервые полученный
                # точный release_status; повторный такой ответ остаётся no-op.
                if not (rec.get("kind") or ""):
                    if fresh and (fresh.get("kind") or ""):
                        titles[tid] = _merge_title_record(
                            media,
                            row,
                            fresh,
                            previous=rec,
                            meta_updated_at=meta_updated_at,
                        )
                        repaired += 1
                        changed = True
                        continue
                    if (
                        fresh
                        and fresh.get("release_status")
                        and rec.get("release_status") != fresh["release_status"]
                    ):
                        rec["release_status"] = fresh["release_status"]
                        changed = True

                refreshed = maintenance_meta.get(tid)
                if refreshed is not None:
                    titles[tid] = _merge_title_record(
                        media,
                        row,
                        refreshed,
                        previous=rec,
                        meta_updated_at=meta_updated_at,
                    )
                    changed = True
                    continue

                # Существующая запись — обновляем только пользовательский стейт,
                # если она не попала в успешный maintenance-ответ.
                new_score  = _safe_int(row.get("score"))
                new_status = (row.get("status") or "").lower()
                new_rew    = _safe_int(row.get("rewatches"))
                if media == "anime":
                    new_progress = _safe_int(row.get("episodes"))
                    if (rec.get("score") != new_score or rec.get("status") != new_status
                            or rec.get("episodes_watched") != new_progress
                            or rec.get("rewatches") != new_rew):
                        rec["score"] = new_score
                        rec["status"] = new_status
                        rec["episodes_watched"] = new_progress
                        rec["rewatches"] = new_rew
                        changed = True
                else:
                    new_ch = _safe_int(row.get("chapters"))
                    new_vol = _safe_int(row.get("volumes"))
                    if (rec.get("score") != new_score or rec.get("status") != new_status
                            or rec.get("chapters_read") != new_ch
                            or rec.get("volumes_read") != new_vol
                            or rec.get("rewatches") != new_rew):
                        rec["score"] = new_score
                        rec["status"] = new_status
                        rec["chapters_read"] = new_ch
                        rec["volumes_read"] = new_vol
                        rec["rewatches"] = new_rew
                        changed = True
            else:
                # Новая запись. Фильтруем по kind тем же критерием, что и
                # уведомления (is_relevant): спецвыпуски, клипы, PV и т.п.
                # не должны попадать в статистику.
                # kind берём только из метаданных — в list_export его нет.
                meta = meta_map.get(tid)
                kind = (meta or {}).get("kind", "")
                # Если метаданные пришли и kind явно нерелевантный — пропускаем.
                # Если метаданные НЕ пришли (kind пустой, сбой API) — заносим
                # запись, чтобы не потерять реальный тайтл; отфильтруется
                # при следующей синхронизации, когда метаданные подтянутся.
                if kind and not is_relevant(media, kind):
                    skipped_irrelevant += 1
                    continue
                titles[tid] = _merge_title_record(
                    media,
                    row,
                    meta,
                    meta_updated_at=(
                        meta_updated_at if meta is not None else None
                    ),
                )
                changed = True

        if skipped_irrelevant:
            log.info("sync_stats_all(%s): пропущено нерелевантных по kind: %d",
                     media, skipped_irrelevant)
        if repaired:
            log.info("sync_stats_all(%s): дозапрошена битая мета (kind был пуст): %d",
                     media, repaired)
        if malformed_ids:
            log.warning(
                "sync_stats_all(%s): восстановлено повреждённых title-записей: %d",
                media,
                len(malformed_ids),
            )
        # Чистка существующих записей, чей kind не проходит фильтр
        # (самоочистка при изменении критерия или после обновления метаданных).
        stale_kind = [
            tid for tid, rec in titles.items()
            if (
                isinstance(rec, dict)
                and rec.get("kind")
                and not is_relevant(media, rec["kind"])
            )
        ]
        for tid in stale_kind:
            del titles[tid]
            changed = True
        if stale_kind:
            log.info("sync_stats_all(%s): удалено нерелевантных по kind из titles: %d",
                     media, len(stale_kind))

        # Удаляем записи, которых больше нет в экспорте (тайтл убран из списка)
        removed = [tid for tid in titles if tid not in valid_rows]
        for tid in removed:
            del titles[tid]
            changed = True
        if removed:
            log.info("sync_stats_all(%s): удалено отсутствующих в экспорте: %d", media, len(removed))

        # Пересчитываем агрегаты (сохраняя by_quarter)
        existing_bq = stats[media].get("aggregates", {}).get("by_quarter")
        stats[media]["aggregates"] = recompute_aggregates(media, titles, existing_bq)

    # Собираем избранное (джойн с уже построенными titles)
    try:
        before = json.dumps(stats.get("favourites"), ensure_ascii=False, sort_keys=True)
        stats = await _collect_favourites(session, stats, fav=fav)
        after = json.dumps(stats.get("favourites"), ensure_ascii=False, sort_keys=True)
        if before != after:
            changed = True
    except ProfilePrivacyError:
        raise
    except Exception as e:
        log.error("sync_stats_all: сбор избранного упал: %s", e)

    if changed:
        save_stats_all(stats)
        log.info("sync_stats_all: stats_all.json обновлён.")
    else:
        log.info("sync_stats_all: изменений нет.")

    return stats, True


def record_current_event(
    cur: dict, entry: dict, event_type: str, media_type: str, score: int | None,
) -> dict:
    """
    Фиксируем событие истории в stats_current (для хронологии квартала).
    Учитываем только значимые для статистики типы.
    Дедупликация: один (media_type, id, event_type) на квартал.
    """
    # Первая, изменённая или отменённая оценка внутри квартала обновляет score
    # уже записанного completed-события того же тайтла. Если completed-события
    # в этом квартале нет, оценку не добавляем: в отчёте тайтла всё равно нет.
    if event_type in ("score_set", "score_changed", "score_removed"):
        if event_type != "score_removed" and score is None:
            return cur
        try:
            tid = str((entry.get("target") or {}).get("id") or "")
            if not tid:
                return cur
            new_score = None if event_type == "score_removed" else score
            for ev in cur.get("events", []):
                if (
                    ev.get("media") == media_type
                    and ev.get("id") == tid
                    and ev.get("event") == "completed"
                ):
                    if ev.get("score") != new_score:
                        ev["score"] = new_score
                        log.info(
                            "Обновлена оценка в квартале: id=%s → %s",
                            tid,
                            new_score,
                        )
                    break
        except Exception as e:
            log.error("record_current_event(%s): %s", event_type, e)
        return cur

    if event_type not in ("completed", "dropped", "planned", "rewatching"):
        return cur
    try:
        target = entry.get("target") or {}
        tid = str(target.get("id") or "")
        if not tid:
            return cur
        # Дедуп
        for ev in cur.get("events", []):
            if (
                ev.get("media") == media_type
                and ev.get("id") == tid
                and ev.get("event") == event_type
            ):
                return cur
        cur.setdefault("events", []).append({
            "id":          tid,
            "media":       media_type,
            "event":       event_type,
            "score":       score,
            "recorded_at": _utcnow().isoformat(),
        })
    except Exception as e:
        log.error("record_current_event: %s", e)
    return cur


def _quarter_events(cur: dict) -> list[dict]:
    """Валидные словари событий квартала; повреждённое состояние игнорируем."""
    raw_events = cur.get("events")
    if not isinstance(raw_events, list):
        return []
    events = []
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        normalized = dict(event)
        if normalized.get("event") in ("completed", "dropped"):
            tid = normalized.get("id")
            if isinstance(tid, bool) or not isinstance(tid, (str, int)):
                continue
            normalized["id"] = str(tid)
        if normalized.get("score") is not None:
            normalized["score"] = _safe_int(normalized["score"])
        events.append(normalized)
    return events


def _quarter_titles(cur: dict, stats_all: dict, media: str, event: str) -> list[dict]:
    """
    Возвращает записи titles{} для тайтлов, у которых в текущем квартале
    было событие event ("completed"|"dropped"), джойня события с stats_all.
    Для completed подставляем score из события (на момент завершения).
    """
    titles = (stats_all.get(media) or {}).get("titles") or {}
    out = []
    seen = set()
    for ev in _quarter_events(cur):
        if ev.get("media") != media or ev.get("event") != event:
            continue
        tid = ev.get("id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        rec = titles.get(tid)
        if rec:
            merged = dict(rec)
            # score события приоритетнее (актуально на момент завершения квартала)
            if event == "completed" and ev.get("score") is not None:
                merged["score"] = ev["score"]
            out.append(merged)
        else:
            # Метаданных нет (тайтл не успел попасть в stats_all) — минимальная запись
            out.append({
                "title": "???", "url": "", "score": ev.get("score") or 0,
                "genres": [], "themes": [], "demographic": [],
            })
    return out


def _header_line(emoji: str, title: str) -> Line:
    """Типизированный акцентированный заголовок архиблока."""
    border = "━" * 5
    return line(Bold(f"{border} {emoji} {title} {border}"))


def _counter_rows(
    pairs: list[tuple[str, int]],
    *,
    show_percent: bool = False,
    total: int = 0,
) -> Rows:
    """Логические выровненные rows без готового HTML."""
    return Rows(tuple(
        Row(
            str(name),
            str(count),
            f"  {round(count / total * 100)}%" if show_percent and total > 0 else "",
        )
        for name, count in pairs
    ))


def _top_pairs(counter: dict, count: int) -> list[tuple[str, int]]:
    """Топ-N пар в прежнем стабильном порядке по убыванию count."""
    return sorted(counter.items(), key=lambda pair: pair[1], reverse=True)[:count]


def _top_section(
    emoji: str,
    title: str,
    counter: dict,
    count: int,
    *,
    show_percent: bool = False,
    total: int = 0,
) -> Section | None:
    pairs = _top_pairs(counter, count)
    if not pairs:
        return None
    return section(
        line(f"{emoji} ", Bold(title)),
        _counter_rows(pairs, show_percent=show_percent, total=total),
    )


def _score_section(dist: dict) -> Section | None:
    pairs = [(_safe_int(score), count) for score, count in dist.items() if _safe_int(score) > 0]
    if not pairs:
        return None
    pairs.sort(key=lambda pair: pair[0], reverse=True)
    return section(
        line("📊 ", Bold("Оценки")),
        _counter_rows([(f"★{score}", count) for score, count in pairs]),
    )


def _status_section(
    aggregate: dict,
    *,
    completed_label: str,
    watching_label: str,
) -> Section | None:
    pairs = [
        (completed_label, aggregate.get("total_completed", 0)),
        ("Брошено", aggregate.get("total_dropped", 0)),
        (watching_label, aggregate.get("total_watching", 0)),
        ("В планах", aggregate.get("total_planned", 0)),
        ("Отложено", aggregate.get("total_on_hold", 0)),
    ]
    pairs = [(name, count) for name, count in pairs if count]
    if not pairs:
        return None
    return section(line("📦 ", Bold("Статусы")), _counter_rows(pairs))


def _kinds_section(kinds: dict, labels: dict) -> Section | None:
    if not kinds:
        return None
    pairs = [(name, kinds.get(key, 0)) for key, name in labels.items() if kinds.get(key, 0)]
    pairs.extend((str(key), count) for key, count in kinds.items() if key not in labels and count)
    if not pairs:
        return None
    return section(line("🎞 ", Bold("Типы")), _counter_rows(pairs))


def _title_inline(record: dict) -> Link | Text:
    title = str(record.get("title") or "???")
    relative_url = _rel_url(record.get("url"))
    if relative_url:
        return Link(title, f"{SHIKI_BASE_URL}{relative_url}")
    return Text(title)


def _build_quarter_sections(
    records: list[dict],
    media: str,
) -> tuple[list[Line], list[Section]]:
    """Логические sections статистики по завершённым тайтлам квартала."""
    lead_lines: list[Line] = []
    sections: list[Section] = []
    if not records:
        return lead_lines, sections

    scores = [record["score"] for record in records if _safe_int(record.get("score")) > 0]
    if scores:
        average = round(sum(scores) / len(scores), 1)
        score_parts: list[Text | Bold | Italic] = [Text("⭐ Средняя оценка: "), Bold(str(average))]
        shiki_scores = [
            record["shiki_score"]
            for record in records
            if _safe_int(record.get("score")) > 0
            and isinstance(record.get("shiki_score"), (int, float))
        ]
        if shiki_scores:
            shiki_average = round(sum(shiki_scores) / len(shiki_scores), 1)
            difference = round(average - shiki_average, 1)
            sign = "+" if difference >= 0 else ""
            score_parts.extend([
                Text("  "),
                Italic(f"(Shikimori: {shiki_average}, {sign}{difference})"),
            ])
        lead_lines.append(Line(tuple(score_parts)))
        distribution: dict = {}
        for score in scores:
            _bump(distribution, score)
        score_section = _score_section(distribution)
        if score_section:
            sections.append(score_section)

    top = sorted(
        [record for record in records if _safe_int(record.get("score")) > 0],
        key=lambda record: record["score"],
        reverse=True,
    )[:3]
    if top:
        top_lines = [line("🏆 ", Bold("Топ по оценке:"))]
        for index, record in enumerate(top, 1):
            top_lines.append(line(
                f"  {index}. ",
                _title_inline(record),
                " — ⭐",
                str(record["score"]),
            ))
        sections.append(section(*top_lines))

    years = [
        (record["year"], str(record.get("title") or "???"))
        for record in records
        if isinstance(record.get("year"), int) and record["year"] > 1900
    ]
    if years:
        oldest = min(years, key=lambda item: item[0])
        newest = max(years, key=lambda item: item[0])
        average_year = round(sum(year for year, _ in years) / len(years))
        if oldest[0] == newest[0]:
            chronology = line("🗓️ Год выпуска: ", Bold(str(oldest[0])))
        else:
            chronology = line(
                "🗓️ Хронология: ",
                Bold(str(oldest[0])),
                " (",
                oldest[1],
                ") → ",
                Bold(str(newest[0])),
                " (",
                newest[1],
                f"),  ср. {average_year}",
            )
        sections.append(section(chronology))

    genres: dict = {}
    themes: dict = {}
    demographic: dict = {}
    for record in records:
        for genre in record.get("genres", []):
            _bump(genres, genre)
        for theme in record.get("themes", []):
            _bump(themes, theme)
        for audience in record.get("demographic", []):
            _bump(demographic, audience)

    completed_count = len(records)
    if media == "anime":
        studios: dict = {}
        origins: dict = {}
        total_episodes = 0
        total_minutes = 0
        for record in records:
            for studio in record.get("studios", []):
                _bump(studios, studio)
            _bump(origins, record.get("origin"))
            episodes = _safe_int(record.get("episodes_watched"))
            total_episodes += episodes
            duration = record.get("duration")
            if isinstance(duration, int) and duration > 0 and episodes > 0:
                total_minutes += duration * episodes
        if total_episodes:
            total_line = line(
                "📺 Эпизодов: ",
                Bold(str(total_episodes)),
                f"  (~{round(total_minutes / 60, 1)} ч.)",
            )
            if sections:
                sections[-1] = section(*sections[-1].items, total_line)
            else:
                lead_lines.append(total_line)
        candidates = (
            _top_section("🎭", "Жанры", genres, 8, show_percent=True, total=completed_count),
            _top_section("🏷", "Темы", themes, 8, show_percent=True, total=completed_count),
            _top_section("👥", "Аудитория", demographic, 99, show_percent=True, total=completed_count),
            _top_section("🎨", "Студии", studios, 6),
            _top_section("📚", "Источники", origins, 99),
        )
    else:
        publishers: dict = {}
        total_chapters = 0
        for record in records:
            for publisher in record.get("publishers", []):
                _bump(publishers, publisher)
            total_chapters += _safe_int(record.get("chapters_read"))
        if total_chapters:
            total_line = line("📖 Глав прочитано: ", Bold(str(total_chapters)))
            if sections:
                sections[-1] = section(*sections[-1].items, total_line)
            else:
                lead_lines.append(total_line)
        candidates = (
            _top_section("🎭", "Жанры", genres, 8, show_percent=True, total=completed_count),
            _top_section("🏷", "Темы", themes, 8, show_percent=True, total=completed_count),
            _top_section("👥", "Аудитория", demographic, 99, show_percent=True, total=completed_count),
            _top_section("🏢", "Издатели", publishers, 6),
        )
    sections.extend(candidate for candidate in candidates if candidate is not None)
    return lead_lines, sections


def _media_quarter_unit(
    media: str,
    completed: list[dict],
    dropped: list[dict],
    planned: int,
    *prefix_sections: Section,
) -> Unit:
    """Собрать самостоятельную anime/manga тему квартального отчёта."""
    if media == "anime":
        header = _header_line("🎬", "АНИМЕ")
        completed_label = "✅ Завершено: "
    else:
        header = _header_line("📚", "МАНГА")
        completed_label = "✅ Прочитано: "
    summary = [line(completed_label, Bold(str(len(completed))))]
    if dropped:
        summary.append(line(f"🗑 Брошено: {len(dropped)}"))
    if planned:
        summary.append(line(f"📋 В планируемое: {planned}"))
    if not completed and not dropped and not planned:
        summary.append(line(Italic("Пока ничего не завершено.")))
    lead_lines, detail_sections = _build_quarter_sections(completed, media)
    summary.extend(lead_lines)
    return unit(
        *prefix_sections,
        section(header),
        section(*summary),
        *detail_sections,
    )


def build_favourites_messages(stats: dict) -> Report:
    """Типизированный отчёт по всем непустым категориям любимого."""
    favourites = stats.get("favourites") or {}
    blocks = [
        ("🎬", "Аниме", favourites.get("anime") or []),
        ("📚", "Манга", favourites.get("manga") or []),
        ("📖", "Ранобэ", favourites.get("ranobe") or []),
        ("👤", "Персонажи", favourites.get("characters") or []),
        ("🎨", "Люди индустрии", favourites.get("people") or []),
    ]
    header = section(line("❤️ ", Bold("ЛЮБИМОЕ")))
    if not any(items for _, _, items in blocks):
        return Report((unit(header, section(line(Italic("Список любимого пока пуст.")))),))

    sections = [header]
    for emoji, title, items in blocks:
        if not items:
            continue
        item_lines = [line(f"{emoji} ", Bold(title), f" ({len(items)})")]
        for item in items:
            score = item.get("score")
            parts = [Text("  • "), _title_inline(item)]
            if isinstance(score, int) and score > 0:
                parts.append(Text(f" — ⭐{score}"))
            item_lines.append(Line(tuple(parts)))
        sections.append(section(*item_lines))
    return Report((Unit(tuple(sections)),))


def build_stats_all_messages(stats: dict) -> Report:
    """Типизированный отчёт за всё время с отдельными anime/manga units."""
    a_agg = (stats.get("anime") or {}).get("aggregates") or {}
    m_agg = (stats.get("manga") or {}).get("aggregates") or {}

    updated = _parse_iso_utc(stats.get("updated_at"))
    upd_str = ""
    if updated is not None:
        upd_str = updated.strftime("%d.%m.%Y")

    # Пустая статистика — одно короткое сообщение
    if a_agg.get("total_completed", 0) == 0 and m_agg.get("total_completed", 0) == 0:
        return Report((unit(
            section(line("📊 ", Bold("СТАТИСТИКА ЗА ВСЁ ВРЕМЯ"))),
            section(line(Italic("Статистика ещё не собрана. Дай боту немного времени."))),
        ),))

    a_total = a_agg.get("total_completed", 0)
    m_total = m_agg.get("total_completed", 0)

    # ── Аниме ───────────────────────────────────
    anime_sections = [section(line("📊 ", Bold("СТАТИСТИКА ЗА ВСЁ ВРЕМЯ")))]
    if upd_str:
        anime_sections[0] = section(
            line("📊 ", Bold("СТАТИСТИКА ЗА ВСЁ ВРЕМЯ")),
            line(Italic(f"актуально на {upd_str}")),
        )
    anime_sections.append(section(_header_line("🎬", "АНИМЕ")))

    # Акцент сверху: сколько посмотрено · эпизоды/время, средняя оценка
    eps = a_agg.get("total_episodes_watched", 0)
    hrs = a_agg.get("total_hours_watched", 0)
    anime_summary_parts = [Text("✅ Завершено: "), Bold(str(a_total))]
    if eps:
        anime_summary_parts.append(Text(f"   ·   📺 {eps} эп (~{hrs} ч)"))
    anime_summary = [Line(tuple(anime_summary_parts))]
    avg_a = _avg_score_from_dist(a_agg.get("score_dist", {}))
    if avg_a is not None:
        average_parts = [Text("⭐ Средняя: "), Bold(str(avg_a))]
        avg_shiki_a = a_agg.get("avg_shiki_completed")
        if isinstance(avg_shiki_a, (int, float)):
            diff = round(avg_a - avg_shiki_a, 1)
            sign = "+" if diff >= 0 else ""
            average_parts.extend([
                Text("   "),
                Italic(f"Shikimori: {round(avg_shiki_a, 1)} ({sign}{diff})"),
            ])
        anime_summary.append(Line(tuple(average_parts)))
    anime_sections.append(section(*anime_summary))

    # Детализация блоками
    for block in (
        _status_section(
            a_agg,
            completed_label="Завершено",
            watching_label="Смотрю",
        ),
        _kinds_section(a_agg.get("kinds", {}), _KIND_RU_ANIME),
        _score_section(a_agg.get("score_dist", {})),
        _top_section("🎭", "Жанры", a_agg.get("genres", {}), 8, show_percent=True, total=a_total),
        _top_section("🏷", "Темы", a_agg.get("themes", {}), 8, show_percent=True, total=a_total),
        _top_section("👥", "Аудитория", a_agg.get("demographic", {}), 99, show_percent=True, total=a_total),
        _top_section("🎨", "Студии", a_agg.get("studios", {}), 6),
        _top_section("📚", "Источники", a_agg.get("origins", {}), 99),
        _top_section("🔞", "Рейтинги", a_agg.get("ratings", {}), 99),
    ):
        if block:
            anime_sections.append(block)

    # ── Манга ───────────────────────────────────
    manga_sections = [section(_header_line("📚", "МАНГА"))]

    ch = m_agg.get("total_chapters_read", 0)
    vol = m_agg.get("total_volumes_read", 0)
    manga_summary_parts = [Text("✅ Прочитано: "), Bold(str(m_total))]
    if ch:
        manga_summary_parts.append(Text(f"   ·   📖 {ch} гл · {vol} томов"))
    manga_summary = [Line(tuple(manga_summary_parts))]
    avg_m = _avg_score_from_dist(m_agg.get("score_dist", {}))
    if avg_m is not None:
        average_parts = [Text("⭐ Средняя: "), Bold(str(avg_m))]
        avg_shiki_m = m_agg.get("avg_shiki_completed")
        if isinstance(avg_shiki_m, (int, float)):
            diff = round(avg_m - avg_shiki_m, 1)
            sign = "+" if diff >= 0 else ""
            average_parts.extend([
                Text("   "),
                Italic(f"Shikimori: {round(avg_shiki_m, 1)} ({sign}{diff})"),
            ])
        manga_summary.append(Line(tuple(average_parts)))
    manga_sections.append(section(*manga_summary))

    for block in (
        _status_section(
            m_agg,
            completed_label="Прочитано",
            watching_label="Читаю",
        ),
        _kinds_section(m_agg.get("kinds", {}), _KIND_RU_MANGA),
        _score_section(m_agg.get("score_dist", {})),
        _top_section("🎭", "Жанры", m_agg.get("genres", {}), 8, show_percent=True, total=m_total),
        _top_section("🏷", "Темы", m_agg.get("themes", {}), 8, show_percent=True, total=m_total),
        _top_section("👥", "Аудитория", m_agg.get("demographic", {}), 99, show_percent=True, total=m_total),
        _top_section("🏢", "Издатели", m_agg.get("publishers", {}), 6),
    ):
        if block:
            manga_sections.append(block)

    return Report((Unit(tuple(anime_sections)), Unit(tuple(manga_sections))))


def _prepare_quarter_report(cur: dict, stats_all: dict) -> dict:
    """Общие входные данные текущего и итогового квартальных отчётов."""
    events = _quarter_events(cur)
    return {
        "anime": {
            "completed": _quarter_titles(cur, stats_all, "anime", "completed"),
            "dropped": _quarter_titles(cur, stats_all, "anime", "dropped"),
            "planned": sum(
                1 for event in events
                if event.get("media") == "anime"
                and event.get("event") == "planned"
            ),
        },
        "manga": {
            "completed": _quarter_titles(cur, stats_all, "manga", "completed"),
            "dropped": _quarter_titles(cur, stats_all, "manga", "dropped"),
            "planned": sum(
                1 for event in events
                if event.get("media") == "manga"
                and event.get("event") == "planned"
            ),
        },
    }


def build_current_stats_messages(cur: dict, stats_all: dict) -> Report:
    """Типизированный текущий квартальный отчёт с двумя delivery units."""
    title_label = tracking_period_label(cur)

    report = _prepare_quarter_report(cur, stats_all)
    anime = report["anime"]
    manga = report["manga"]

    header_lines = [line("📊 ", Bold(f"Статистика {title_label}"))]
    if _is_partial_quarter(cur):
        header_lines.append(line(Italic(
            "⚠️ Квартал отслеживается не с самого начала — данные неполные."
        )))

    anime_unit = _media_quarter_unit(
        "anime",
        anime["completed"],
        anime["dropped"],
        anime["planned"],
        section(*header_lines),
    )
    manga_unit = _media_quarter_unit(
        "manga",
        manga["completed"],
        manga["dropped"],
        manga["planned"],
    )
    return Report((anime_unit, manga_unit))


def build_quarterly_report_messages(
    cur: dict,
    stats_all: dict,
    prev_quarter: dict | None,
) -> Report:
    """Типизированный итог квартала для owner delivery."""
    title_label = tracking_period_label(cur)

    report = _prepare_quarter_report(cur, stats_all)
    anime = report["anime"]
    manga = report["manga"]

    header_lines = [
        line("📊 ", Bold("КВАРТАЛЬНЫЙ ОТЧЁТ")),
        line(Bold(title_label)),
    ]
    if _is_partial_quarter(cur):
        header_lines.append(line(Italic(
            "⚠️ Квартал отслеживался не с самого начала — данные неполные."
        )))

    units = [_media_quarter_unit(
        "anime",
        anime["completed"],
        anime["dropped"],
        anime["planned"],
        section(*header_lines),
    )]
    units.append(_media_quarter_unit(
        "manga",
        manga["completed"],
        manga["dropped"],
        manga["planned"],
    ))

    extra_sections: list[Section] = []
    if prev_quarter:
        prev_a = prev_quarter.get("anime_completed", 0)
        prev_m = prev_quarter.get("manga_completed", 0)
        prev_label = quarter_label(prev_quarter.get("period") or "прошлый квартал")
        extra_sections.append(section(
            line("📈 ", Bold(f"Сравнение с {prev_label}:")),
            line(f"🎬 Аниме: {_pct_diff(len(anime['completed']), prev_a)}"),
            line(f"📚 Манга: {_pct_diff(len(manga['completed']), prev_m)}"),
        ))

    all_comp = anime["completed"] + manga["completed"]
    ach: list[str] = []
    tens = [r for r in all_comp if r.get("score") == 10]
    if len(tens) >= 3:
        ach.append(f"💎 Десятку поставил {len(tens)} раза — строгий критик!")
    elif len(tens) == 1:
        ach.append("💎 Один безоговорочный шедевр за квартал.")
    total_drops = len(anime["dropped"]) + len(manga["dropped"])
    if total_drops == 0 and all_comp:
        ach.append("🎯 Ни одного дропа — железная воля или идеальный вкус!")
    elif total_drops >= 5:
        ach.append(f"🗑️ {total_drops} дропов — знает, чего не хочет.")
    low = [r for r in all_comp if 0 < _safe_int(r.get("score")) <= 3]
    if low:
        n = len(low)
        ach.append(f"🧟 Домучил {n} тайтл{'а' if n < 5 else 'ов'} с оценкой ≤3 — стойкость.")

    if ach:
        extra_sections.append(section(
            line("🏆 ", Bold("Достижения:")),
            *(line(f"• {achievement}") for achievement in ach),
        ))

    if extra_sections:
        units.append(Unit(tuple(extra_sections)))

    return Report(tuple(units))


def _load_prev_quarter_summary(period: str) -> dict | None:
    """Краткая сводка предыдущего квартала из снапшота для сравнения."""
    try:
        path = QUARTERS_DIR / f"{period}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                "period": data.get("period"),
                "anime_completed": data.get("anime_completed", 0),
                "manga_completed": data.get("manga_completed", 0),
            }
    except Exception as e:
        log.warning("_load_prev_quarter_summary(%s): %s", period, e)
    return None


def _save_quarter_snapshot(period: str, cur: dict, stats_all: dict) -> None:
    """Сохраняем замороженный снапшот квартала в quarters/<period>.json."""
    try:
        QUARTERS_DIR.mkdir(parents=True, exist_ok=True)
        comp_a = _quarter_titles(cur, stats_all, "anime", "completed")
        comp_m = _quarter_titles(cur, stats_all, "manga", "completed")
        snapshot = {
            "period": period,
            "anime_completed": len(comp_a),
            "manga_completed": len(comp_m),
            "events": cur.get("events", []),
            "anime_titles": comp_a,
            "manga_titles": comp_m,
        }
        _atomic_write(QUARTERS_DIR / f"{period}.json",
                      json.dumps(snapshot, ensure_ascii=False, indent=2))
        log.info("Снапшот квартала %s сохранён.", period)
    except Exception as e:
        log.error("_save_quarter_snapshot(%s): %s", period, e)


def _update_by_quarter(stats_all: dict, period: str, cur: dict) -> None:
    """Добавляем сводку квартала в aggregates.by_quarter для аниме и манги."""
    for media in ("anime", "manga"):
        comp = _quarter_titles(cur, stats_all, media, "completed")
        scores = [r["score"] for r in comp if _safe_int(r.get("score")) > 0]
        avg = round(sum(scores) / len(scores), 2) if scores else None
        bq = stats_all[media].setdefault("aggregates", {}).setdefault("by_quarter", {})
        entry = {"completed": len(comp), "avg_score": avg}
        if media == "anime":
            entry["episodes_watched"] = sum(_safe_int(r.get("episodes_watched")) for r in comp)
        else:
            entry["chapters_read"] = sum(_safe_int(r.get("chapters_read")) for r in comp)
        bq[period] = entry

