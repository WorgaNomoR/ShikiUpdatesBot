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
import time

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


# ═══════════════════════════════════════════════════════════════════
#  ЦЕНТРАЛЬНЫЙ ТРОТТЛ + РЕТРАЙ НА 429  (единый choke-point на все запросы)
# ═══════════════════════════════════════════════════════════════════
#
# Firewall-стиль: между любыми двумя исходящими запросами держим фиксированный
# min-gap, БЕЗ джиттера. Один цикл шлёт ~7 запросов за <1 с и без троттла
# пробивает лимит Шики 5 req/сек → поздние падают в 429 (мета манги теряется,
# избранное протухает). Троттл сериализует всплеск в ≤ 1/_MIN_GAP req/сек.
# Плюс мягкий ретрай на 429 (Retry-After) как страховка, если лимит всё же
# задет (напр. чужой трафик с того же IP). 429 перехватываем ДО проверки
# статуса. Один choke-point на все call-sites (вкл. /status и будущий
# много-профильный режим «запросы × N профилей»).

_MIN_GAP: float = 0.25              # сек между выстрелами → ≤4 req/сек (< лимита 5)
_MAX_429_RETRIES: int = 2           # доп. попыток после первого 429
_RETRY_AFTER_DEFAULT: float = 1.0   # если сервер не прислал корректный Retry-After
_RETRY_AFTER_CAP: float = 10.0      # потолок ожидания, чтобы не подвесить цикл

_last_request_at: float = 0.0       # монотонная метка последнего выстрела
_throttle_lock: "asyncio.Lock | None" = None


def _get_throttle_lock() -> asyncio.Lock:
    """Ленивое создание лока — привязка к работающему event loop на первом
    запросе, а не к импортному (которого может не быть / он может смениться)."""
    global _throttle_lock
    if _throttle_lock is None:
        _throttle_lock = asyncio.Lock()
    return _throttle_lock


async def _throttle() -> None:
    """Держит фиксированный min-gap перед выстрелом. Сериализован локом: все
    call-sites проходят через одну точку, всплеск размазывается в ровный ритм."""
    global _last_request_at
    async with _get_throttle_lock():
        gap = _MIN_GAP - (time.monotonic() - _last_request_at)
        if gap > 0:
            await asyncio.sleep(gap)
        _last_request_at = time.monotonic()


def _retry_after(headers) -> float:
    """Секунды ожидания из заголовка Retry-After (форма «число секунд»).
    HTTP-date-форму не поддерживаем — фолбэк на дефолт. Клампим в
    [0, _RETRY_AFTER_CAP]."""
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return _RETRY_AFTER_DEFAULT
    try:
        secs = float(str(raw).strip())
    except (TypeError, ValueError):
        return _RETRY_AFTER_DEFAULT
    if secs < 0:
        return _RETRY_AFTER_DEFAULT
    return min(secs, _RETRY_AFTER_CAP)


async def _fetch(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    parse,
    label: str,
    timeout: float,
    headers: dict = HEADERS,
    json_body: dict | None = None,
):
    """
    Единый выстрел HTTP через центральный троттл + ретрай на 429.

    Перед КАЖДОЙ попыткой await-им _throttle() (min-gap). При 429 читаем
    Retry-After, спим, ретраим (до _MAX_429_RETRIES). На не-200 → None. На
    успехе отдаём await parse(resp). parse разбирает тело под конкретный вызов
    (list / dict / GraphQL); его исключения парсинга ловим здесь → None.
    """
    for attempt in range(_MAX_429_RETRIES + 1):
        await _throttle()
        try:
            if method == "POST":
                cm = session.post(
                    url, headers=headers, json=json_body,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                )
            else:
                cm = session.get(
                    url, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                )
            async with cm as resp:
                if resp.status == 429:
                    delay = _retry_after(getattr(resp, "headers", None))
                    if attempt < _MAX_429_RETRIES:
                        log.warning(
                            "%s: 429 rate limit, ретрай через %.2f с (попытка %d/%d)",
                            label, delay, attempt + 1, _MAX_429_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        continue
                    log.warning("%s: 429 rate limit, ретраи исчерпаны", label)
                    return None
                if resp.status != 200:
                    log.warning("%s: HTTP %d", label, resp.status)
                    return None
                try:
                    return await parse(resp)
                except (json.JSONDecodeError, aiohttp.ContentTypeError,
                        AttributeError, TypeError, KeyError) as e:
                    # Статус 200, но тело битое / неожиданной структуры (None,
                    # не список, нет ожидаемых полей) — parse спотыкается на
                    # .get()/итерации/`in`. Деградируем в None+warning, а не
                    # рвём итерацию наверх в общий except polling_loop.
                    log.warning("%s: не удалось разобрать ответ: %s", label, e)
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.error("%s: ошибка запроса: %s", label, e)
            return None
    return None


async def _gql_request(
    session: aiohttp.ClientSession, query: str, variables: dict,
) -> dict | None:
    """
    Один GraphQL-запрос через центральный троттл/ретрай. Возвращает поле data
    или None при ошибке. Частичные данные (data + errors) возвращаются — пусть
    caller решает.
    """
    async def _parse(resp):
        payload = await resp.json(content_type=None)
        if "errors" in payload:
            log.warning("_gql_request: GraphQL errors: %s", payload["errors"])
        return payload.get("data")

    return await _fetch(
        session, "POST", GRAPHQL_URL, parse=_parse, label="_gql_request",
        headers={**HEADERS, "Content-Type": "application/json"}, timeout=20,
        json_body={"query": query, "variables": variables},
    )


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

    async def _parse(resp):
        data = await resp.json(content_type=None)
        if not isinstance(data, list):
            log.warning("fetch_list_export(%s): ответ не список.", media)
            return None
        return data

    return await _fetch(
        session, "GET", url, parse=_parse,
        label=f"fetch_list_export({media})", timeout=30,
    )


async def fetch_meta_batch(media: str, ids: list[str],
                           session: "aiohttp.ClientSession | None" = None) -> dict[str, dict]:
    """
    Запрашиваем метаданные тайтлов через GraphQL батчами по 50.
    media: "anime" | "manga"
    Возвращает {str(id): meta_dict}. При сбое отдельного батча — пропускаем его,
    остальные данные сохраняем (частичный результат лучше пустого).
    """
    clean = list({str(i).strip() for i in ids if str(i).strip()})
    if not clean:
        return {}
    if session is None:
        # boot-throttle: своя короткоживущая сессия, если не передали общую.
        async with aiohttp.ClientSession() as own:
            return await fetch_meta_batch(media, ids, session=own)

    query = _GQL_ANIME if media == "anime" else _GQL_MANGA
    result: dict[str, dict] = {}

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

        # Троттл между батчами обеспечивает центральный _throttle в _gql_request
        # (фиксированный min-gap) — отдельная пауза здесь больше не нужна.

    log.info("fetch_meta_batch(%s): получено %d/%d тайтлов.", media, len(result), len(clean))
    return result


async def fetch_history(session: aiohttp.ClientSession) -> list[dict] | None:
    """Запрашиваем историю с API Shikimori.
    Возвращает список записей при успехе или None при любой ошибке.
    """
    return await _fetch(
        session, "GET", HISTORY_URL,
        parse=lambda resp: resp.json(), label="fetch_history", timeout=15,
    )


async def fetch_favourites(session: aiohttp.ClientSession) -> dict | None:
    """
    Запрашиваем избранное с API Shikimori.
    Возвращает словарь вида:
      {"animes": [...], "mangas": [...], "characters": [...], "people": [...], ...}
    Каждый элемент содержит хотя бы "id", "name", "russian", "url".
    Возвращает None при любой ошибке.
    """
    return await _fetch(
        session, "GET", FAVOURITES_URL,
        parse=lambda resp: resp.json(), label="fetch_favourites", timeout=15,
    )


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

            async def _parse(resp, _status=status):
                data = await resp.json()
                # Помечаем каждую запись статусом, чтобы знать её происхождение.
                for item in data:
                    item["_status"] = _status
                return data

            data = await _fetch(
                session, "GET", url, parse=_parse,
                label=f"fetch_current_rates({media}/{status})", timeout=15,
            )
            if data is None:
                return None
            results.extend(data)
    return results


