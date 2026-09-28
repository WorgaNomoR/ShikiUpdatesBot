# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Сбор и обогащение избранного, построение его типизированного отчёта.

HTTP остаётся в shiki_api, публикация и уведомления — у вызывающих сторон.
"""

import aiohttp

from config import log
from report_model import (
    Bold,
    Italic,
    Line,
    Report,
    Text,
    Unit,
    heading,
    line,
    section,
    unit,
)
from report_titles import _title_inline
from shiki_api import fetch_favourites
from utils import (
    _rel_url,
    _safe_int,
    russian_count_word,
)

# Sentinel «аргумент fav не передан» — отличаем от явного None. None означает
# «избранное уже пытались получить в этом цикле и оно недоступно» → НЕ рефетчим
# (иначе на упавшем цикле бьём эндпоинт повторно — анти-паттерн для rate-limit);
# FAVOURITES_UNSET означает «прямой/standalone-вызов, фетчим сами».
FAVOURITES_UNSET = object()


async def _collect_favourites(
    session: "aiohttp.ClientSession | None",
    stats: dict,
    fav=FAVOURITES_UNSET,
) -> dict:
    """
    Собирает избранное в структуру stats["favourites"].

    fav: готовый ответ API (уже скачанный в цикле) — используем и НЕ ходим в
    сеть повторно. fav=FAVOURITES_UNSET (не передан, standalone-вызов) — фетчим сами через
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
    if fav is FAVOURITES_UNSET:
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


def build_favourites_messages(stats: dict) -> Report:
    """Типизированный отчёт по всем непустым категориям избранного."""
    favourites = stats.get("favourites") or {}
    blocks = [
        ("🎬", "Аниме", favourites.get("anime") or []),
        ("📚", "Манга", favourites.get("manga") or []),
        ("📖", "Ранобэ", favourites.get("ranobe") or []),
        ("👤", "Персонажи", favourites.get("characters") or []),
        ("🎨", "Люди индустрии", favourites.get("people") or []),
    ]
    title = heading("❤️ ", Bold("ИЗБРАННОЕ"), level=1)
    if not any(items for _, _, items in blocks):
        return Report((unit(
            section(title),
            section(line(Italic("Список избранного пока пуст."))),
        ),))

    total_items = sum(len(items) for _, _, items in blocks)
    category_count = sum(bool(items) for _, _, items in blocks)
    header = section(
        title,
        line(Italic(
            f"{total_items} "
            f"{russian_count_word(total_items, 'объект', 'объекта', 'объектов')}  ·  "
            f"{category_count} "
            f"{russian_count_word(category_count, 'категория', 'категории', 'категорий')}"
        )),
    )
    sections = [header]
    for emoji, category_title, items in blocks:
        if not items:
            continue
        item_lines = [heading(
            f"{emoji} ",
            Bold(category_title),
            f" · {len(items)}",
            level=2,
        )]
        for item in items:
            score = item.get("score")
            parts = [Text("  • "), _title_inline(item)]
            if isinstance(score, int) and score > 0:
                parts.append(Text(f" — {score}⭐"))
            item_lines.append(Line(tuple(parts)))
        sections.append(section(*item_lines))
    return Report((Unit(tuple(sections)),))
