# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Клиент Shikimori и media-домен ShikiUpdatesBot.

Сетевой слой (list_export / GraphQL / history / favourites / rates) плюс
низкоуровневая классификация медиа (get_media_info, is_relevant, фильтры
типов, категории избранного, словари перевода). Зависит только от config и
utils; messages/stats/handlers зависят от него, не наоборот.
"""

import asyncio
import json

import aiohttp

from config import (
    SHIKI_BASE_URL,
    SHIKI_USER,
    log,
)
from utils import _rel_url, _safe_float

# ═══════════════════════════════════════════════════════════════════
#  КОНСТАНТЫ: ФИЛЬТРЫ ТИПОВ, КАТЕГОРИИ, URL, ЗАГОЛОВКИ, GraphQL
# ═══════════════════════════════════════════════════════════════════

ANIME_ALLOWED_KINDS: frozenset[str] = frozenset({
    "tv",       # TV Сериал (включает Короткие / Средние / Длинные — у них kind="tv")
    "movie",    # Фильм
    "ova",      # OVA
    "ona",      # ONA
})

MANGA_BLOCKED_KINDS: frozenset[str] = frozenset({
    "one_shot", # Ваншот
    "doujin",   # Додзинси (любительское)
})

MANGA_KINDS: frozenset[str] = frozenset({
    "manga", "manhwa", "manhua", "novel", "ranobe", "one_shot", "doujin",
})

_FAV_CATEGORIES: tuple[str, ...] = (
    "animes", "mangas", "ranobe",
    "characters", "people", "mangakas", "seyu", "producers",
)

_INDUSTRY_CATEGORIES: frozenset[str] = frozenset(
    {"people", "mangakas", "seyu", "producers"}
)

HEADERS = {
    "User-Agent": f"ShikimoriWatcherBot/1.0 (TelegramBot; monitoring {SHIKI_USER})",
    "Accept": "application/json",
}

GRAPHQL_URL       = f"{SHIKI_BASE_URL}/api/graphql"

LIST_EXPORT_ANIME = f"{SHIKI_BASE_URL}/{SHIKI_USER}/list_export/animes.json"

LIST_EXPORT_MANGA = f"{SHIKI_BASE_URL}/{SHIKI_USER}/list_export/mangas.json"

HISTORY_URL    = f"{SHIKI_BASE_URL}/api/users/{SHIKI_USER}/history?limit=50"

FAVOURITES_URL = f"{SHIKI_BASE_URL}/api/users/{SHIKI_USER}/favourites"

_STAT_STATUSES: frozenset[str] = frozenset({
    "planned", "watching", "rewatching", "completed", "on_hold", "dropped",
})

_ORIGIN_RU: dict[str, str] = {
    "original":         "Оригинал",
    "manga":            "Манга",
    "manhwa":           "Манхва",
    "manhua":           "Маньхуа",
    "light_novel":      "Ранобэ",
    "novel":            "Новелла",
    "visual_novel":     "Визуальная новелла",
    "game":             "Игра",
    "card_game":        "Карточная игра",
    "music":            "Музыка",
    "book":             "Книга",
    "web_manga":        "Веб-манга",
    "web_novel":        "Веб-новелла",
    "four_koma_manga":  "Ёнкома",
    "picture_book":     "Иллюстрированная книга",
    "radio":            "Радио",
    "other":            "Другое",
    "unknown":          "Неизвестно",
}

_RATING_RU: dict[str, str] = {
    "none":   "Без рейтинга",
    "g":      "G",
    "pg":     "PG",
    "pg_13":  "PG-13",
    "r":      "R-17",
    "r_plus": "R+",
    "rx":     "Rx",
}

_GQL_ANIME = """
query($ids: String!) {
  animes(ids: $ids, limit: 50, censored: false) {
    id
    url
    kind
    score
    rating
    origin
    duration
    episodes
    airedOn { year }
    studios { name }
    genres { russian name kind }
  }
}
"""

_GQL_MANGA = """
query($ids: String!) {
  mangas(ids: $ids, limit: 50, censored: false) {
    id
    url
    kind
    score
    chapters
    volumes
    airedOn { year }
    publishers { name }
    genres { russian name kind }
  }
}
"""


# ═══════════════════════════════════════════════════════════════════
#  МЕДИА-КЛАССИФИКАЦИЯ И СЕТЕВЫЕ ЗАПРОСЫ
# ═══════════════════════════════════════════════════════════════════

def get_media_info(entry: dict) -> tuple[str, str]:
    """
    Возвращает (media_type, kind) для записи истории.

    media_type: "anime" | "manga"
    kind:       строка из API, например "tv", "movie", "ova", "manga", "one_shot" и т.д.

    Shikimori кладёт в target.type  → "Anime" или "Manga"
                        в target.kind → "tv" / "movie" / "ova" / "manga" / "one_shot" / ...
    """
    target = entry.get("target") or {}

    raw_type = (target.get("type") or "").lower()   # "anime" / "manga" / ""
    kind     = (target.get("kind") or "").lower()   # "tv", "movie", "ova", "manga", ...

    # Манга — если явно указан тип Manga, либо kind из «мангового» набора
    if raw_type == "manga" or kind in MANGA_KINDS:
        return "manga", kind

    return "anime", kind


def is_relevant(media_type: str, kind: str) -> bool:
    """
    Проверяем, стоит ли вообще уведомлять об этой записи.

    Аниме: разрешаем только tv, movie, ova, ona.
           Спецвыпуски (special, tv_special), клипы (music, pv, cm) — пропускаем.
    Манга: запрещаем only one_shot и doujin, всё остальное разрешено.
           Если kind пустой (API не вернул) — пропускаем на всякий случай.
    """
    if not kind:
        # API не вернул kind — лучше пропустить, чем засорить чат
        log.debug("kind отсутствует для media_type=%s, запись пропущена.", media_type)
        return False

    if media_type == "anime":
        return kind in ANIME_ALLOWED_KINDS

    if media_type == "manga":
        return kind not in MANGA_BLOCKED_KINDS

    return False


def _parse_genres(genres_raw: list, kind_filter: str) -> list[str]:
    """Имена жанров заданного kind (genre|theme|demographic). Предпочитаем русское."""
    out = []
    for g in genres_raw or []:
        if isinstance(g, dict) and g.get("kind") == kind_filter:
            name = (g.get("russian") or g.get("name") or "").strip()
            if name:
                out.append(name)
    return out


async def _gql_request(
    session: aiohttp.ClientSession, query: str, variables: dict,
) -> dict | None:
    """
    Один GraphQL-запрос. Возвращает поле data или None при ошибке.
    Частичные данные (data + errors) возвращаются — пусть caller решает.
    """
    try:
        async with session.post(
            GRAPHQL_URL,
            headers={**HEADERS, "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                log.warning("_gql_request: HTTP %d", resp.status)
                return None
            try:
                payload = await resp.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
                log.warning("_gql_request: не удалось распарсить ответ: %s", e)
                return None
            if "errors" in payload:
                log.warning("_gql_request: GraphQL errors: %s", payload["errors"])
            return payload.get("data")
    except asyncio.TimeoutError:
        log.warning("_gql_request: таймаут (20 с)")
        return None
    except aiohttp.ClientError as e:
        log.error("_gql_request: ошибка клиента: %s", e)
        return None


async def fetch_list_export(session: aiohttp.ClientSession, media: str) -> list[dict] | None:
    """
    Скачиваем публичный экспорт списка пользователя.
    media: "anime" | "manga"
    Возвращает список записей или None при любой ошибке.

    Формат записи:
      {target_title, target_title_ru, target_id, target_type,
       score, status, rewatches, episodes|volumes|chapters, text}
    """
    url = LIST_EXPORT_ANIME if media == "anime" else LIST_EXPORT_MANGA
    try:
        async with session.get(
            url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                log.warning("fetch_list_export(%s): HTTP %d", media, resp.status)
                return None
            data = await resp.json(content_type=None)
            if not isinstance(data, list):
                log.warning("fetch_list_export(%s): ответ не список.", media)
                return None
            return data
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.error("fetch_list_export(%s): ошибка запроса: %s", media, e)
        return None
    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
        log.error("fetch_list_export(%s): не удалось разобрать ответ: %s", media, e)
        return None


async def fetch_meta_batch(media: str, ids: list[str]) -> dict[str, dict]:
    """
    Запрашиваем метаданные тайтлов через GraphQL батчами по 50.
    media: "anime" | "manga"
    Возвращает {str(id): meta_dict}. При сбое отдельного батча — пропускаем его,
    остальные данные сохраняем (частичный результат лучше пустого).
    """
    clean = list({str(i).strip() for i in ids if str(i).strip()})
    if not clean:
        return {}

    query = _GQL_ANIME if media == "anime" else _GQL_MANGA
    result: dict[str, dict] = {}

    async with aiohttp.ClientSession() as session:
        # Батчим по 50 (ограничение limit в GraphQL)
        for i in range(0, len(clean), 50):
            batch = clean[i:i + 50]
            data = await _gql_request(session, query, {"ids": ",".join(batch)})
            key = "animes" if media == "anime" else "mangas"
            for item in ((data or {}).get(key) or []):
                try:
                    item_id = str(item.get("id") or "")
                    if not item_id:
                        continue
                    genres_raw = item.get("genres") or []
                    meta = {
                        "url":         _rel_url(item.get("url")),
                        "kind":        (item.get("kind") or "").lower(),
                        "year":        (item.get("airedOn") or {}).get("year"),
                        "shiki_score": _safe_float(item.get("score")),
                        "genres":      _parse_genres(genres_raw, "genre"),
                        "themes":      _parse_genres(genres_raw, "theme"),
                        "demographic": _parse_genres(genres_raw, "demographic"),
                    }
                    if media == "anime":
                        origin_raw = (item.get("origin") or "").strip()
                        rating_raw = (item.get("rating") or "").strip()
                        meta.update({
                            "duration":       item.get("duration"),   # мин/эп
                            "episodes_total": item.get("episodes"),
                            "rating":         _RATING_RU.get(rating_raw, rating_raw or None),
                            "origin":         _ORIGIN_RU.get(origin_raw, origin_raw or None),
                            "studios":        [s["name"] for s in (item.get("studios") or []) if s.get("name")],
                        })
                    else:
                        meta.update({
                            "chapters_total": item.get("chapters"),
                            "volumes_total":  item.get("volumes"),
                            "publishers":     [p["name"] for p in (item.get("publishers") or []) if p.get("name")],
                        })
                    result[item_id] = meta
                except Exception as e:
                    log.warning("fetch_meta_batch(%s): ошибка парсинга id=%s: %s",
                                media, item.get("id"), e)

            # Пауза между батчами — не триггерим rate limit (5 req/sec)
            if i + 50 < len(clean):
                await asyncio.sleep(0.5)

    log.info("fetch_meta_batch(%s): получено %d/%d тайтлов.", media, len(result), len(clean))
    return result


async def fetch_history(session: aiohttp.ClientSession) -> list[dict] | None:
    """Запрашиваем историю с API Shikimori.
    Возвращает список записей при успехе или None при любой ошибке.
    """
    try:
        async with session.get(
            HISTORY_URL,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                log.warning("fetch_history: API вернул статус %d", resp.status)
                return None
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.error("fetch_history: ошибка запроса: %s", e)
        return None
    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
        log.error("fetch_history: не удалось разобрать ответ: %s", e)
        return None


async def fetch_favourites(session: aiohttp.ClientSession) -> dict | None:
    """
    Запрашиваем избранное с API Shikimori.
    Возвращает словарь вида:
      {"animes": [...], "mangas": [...], "characters": [...], "people": [...], ...}
    Каждый элемент содержит хотя бы "id", "name", "russian", "url".
    Возвращает None при любой ошибке.
    """
    try:
        async with session.get(
            FAVOURITES_URL,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                log.warning("fetch_favourites: API вернул статус %d", resp.status)
                return None
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.error("fetch_favourites: ошибка запроса: %s", e)
        return None
    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
        log.error("fetch_favourites: не удалось разобрать ответ: %s", e)
        return None


async def fetch_current_rates(media: str, statuses: list[str]) -> list[dict] | None:
    """
    Запрашивает тайтлы в указанных статусах.
    media:    "anime" или "manga"
    statuses: ["watching", "rewatching"] — одинаково для аниме и манги
    Возвращает объединённый список записей при успехе или None при любой ошибке.
    """
    results = []
    async with aiohttp.ClientSession() as session:
        for status in statuses:
            url = f"{SHIKI_BASE_URL}/api/users/{SHIKI_USER}/{media}_rates?status={status}&limit=50"
            try:
                async with session.get(
                    url,
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Добавляем поле status в каждую запись, чтобы знать откуда она
                        for item in data:
                            item["_status"] = status
                        results.extend(data)
                    else:
                        log.warning("fetch_current_rates: статус %d для %s/%s", resp.status, media, status)
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.error("fetch_current_rates ошибка (%s/%s): %s", media, status, e)
                return None
            except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
                log.error("fetch_current_rates: не удалось разобрать ответ (%s/%s): %s", media, status, e)
                return None
    return results


