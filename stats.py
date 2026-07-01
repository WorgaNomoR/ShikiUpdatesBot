# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Статистика ShikiUpdatesBot.

Доменный слой: агрегирование списков, синхронизация stats_all, события
текущего квартала, снапшоты кварталов, построение отчётов (/stats, /favs,
квартальный). Зависит от config/utils/storage/shiki_api/messages; знают о нём
только хендлеры.
"""

import json
from datetime import datetime

import aiohttp

from config import (
    QUARTERS_DIR,
    SHIKI_BASE_URL,
    log,
)
from messages import (
    _avg_score_from_dist,
    _fav_lines,
    _kinds_block,
    _pct_diff,
    _score_dist_block,
    _section_header,
    _status_block_anime,
    _status_block_manga,
    _top_block,
)
from shiki_api import (
    _STAT_STATUSES,
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
    _rel_url,
    _safe_int,
    _utcnow,
    h,
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
    "manga":  "Манга",
    "manhwa": "Манхва",
    "manhua": "Маньхуа",
    "novel":  "Новеллы",
    "ranobe": "Ранобэ",
}


def _merge_title_record(media: str, export_row: dict, meta: dict | None) -> dict:
    """
    Собираем одну запись titles{} из строки экспорта и метаданных GraphQL.
    meta может быть None — тогда метаданные пустые, но пользовательские данные есть.
    """
    meta = meta or {}
    record = {
        "title":    export_row.get("target_title_ru") or export_row.get("target_title") or "???",
        "title_en": export_row.get("target_title") or "",
        "score":    _safe_int(export_row.get("score")),       # 0 = без оценки
        "status":   (export_row.get("status") or "").lower(),
        "rewatches": _safe_int(export_row.get("rewatches")),
        "url":       meta.get("url") or "",
        "kind":      meta.get("kind") or "",
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
    return record


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


async def _collect_favourites(
    session: "aiohttp.ClientSession | None",
    stats: dict,
    fav: dict | None = None,
) -> dict:
    """
    Собирает избранное в структуру stats["favourites"].

    fav: если передан готовый ответ API (например, уже скачанный в
    check_and_notify_favourites) — используем его и НЕ ходим в сеть повторно.
    Если None — фетчим сами через session.

    Для аниме/манги/ранобэ джойнит оценку и название из titles{} (если тайтл
    там есть); если нет — берёт название из ответа API. Персонажи/люди —
    имя+ссылка из API (в titles{} их нет, ссылки/оценки не будет — это ок).

    fetch_favourites возвращает None при сбое — тогда оставляем прежнее
    избранное (не затираем хорошие данные пустотой при ошибке сети).

    Категоризация Shikimori ненадёжна (режиссёры лежат в mangakas, и т.п.),
    поэтому people+mangakas+seyu+producers сливаем в один блок "people"
    («Люди индустрии»). Ранобэ — отдельный блок, но джойнит по namespace манги.
    """
    if fav is None:
        if session is None:
            # Защита от вызова с обоими None: fetch_favourites(None) упал бы
            # внутри на session.get(...). В норме не случается (sync_stats_all
            # передаёт session, check_and_notify_favourites — готовый fav).
            log.error("_collect_favourites: переданы и fav=None, и session=None — оставляем прежнее.")
            return stats
        fav = await fetch_favourites(session)
    if fav is None:
        log.info("_collect_favourites: запрос избранного не удался — оставляем прежнее.")
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
    fav: dict | None = None,
) -> tuple[dict, bool]:
    """
    Главная функция актуализации stats_all.

    1. Скачиваем list_export для аниме и манги.
    2. Сверяем с titles{} в stats_all — находим новые/изменившиеся записи
       (новый id, либо изменился score/status/episodes/chapters).
    3. Для записей, у которых ещё нет метаданных (новый id) — батч GraphQL.
       Для существ, у которых поменялся только пользовательский стейт —
       обновляем поля из экспорта, метаданные не перезапрашиваем.
    4. Пересчитываем агрегаты, сохраняем.

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

        # Ремонт битой меты (Codacy / баг «ваншоты не фильтруются»):
        # записи с пустым kind — это тайтлы, у которых мета не доехала при
        # первом заносе (GraphQL не вернул элемент → ВСЕ мета-поля пусты разом).
        # Они навсегда оставались в titles{} (new_ids их не видит, самоочистка
        # пропускает пустой kind) и, если completed, врали в счётчике.
        # Дозапрашиваем их повторно. Анонсы (вид ещё неизвестен) вернут снова
        # пустой kind — это обрабатывается как no-op ниже, без записи на диск.
        retry_ids = [
            tid for tid in valid_rows
            if tid in titles and not (titles[tid].get("kind") or "")
        ]

        # Подтягиваем метаданные: для новых + для ремонта битых
        need_meta = new_ids + retry_ids
        meta_map: dict[str, dict] = {}
        if need_meta:
            log.info("sync_stats_all(%s): тайтлов для обогащения: %d (новых %d, ремонт %d)",
                     media, len(need_meta), len(new_ids), len(retry_ids))
            try:
                meta_map = await fetch_meta_batch(media, need_meta, session=session)
            except Exception as e:
                log.error("sync_stats_all(%s): fetch_meta_batch упал: %s", media, e)

        skipped_irrelevant = 0
        repaired = 0

        # Обновляем / создаём записи
        for tid, row in valid_rows.items():
            if tid in titles:
                rec = titles[tid]

                # Ремонт битой меты: запись с пустым kind, и сейчас GraphQL
                # вернул непустой kind → пересобираем ЦЕЛИКОМ (url/year/жанры/
                # kind — всё, что побилось вместе с kind). Дальнейшая
                # самоочистка по kind вынесет ставшие нерелевантными (ваншоты).
                # Если мета снова пустая (анонс) — не трогаем, no-op (без
                # changed), чтобы не сохранять файл каждые 6 часов впустую.
                if not (rec.get("kind") or ""):
                    fresh = meta_map.get(tid)
                    if fresh and (fresh.get("kind") or ""):
                        titles[tid] = _merge_title_record(media, row, fresh)
                        repaired += 1
                        changed = True
                        continue

                # Существующая запись — обновляем только пользовательский стейт,
                # метаданные (genres/studios/...) уже есть, не трогаем.
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
                titles[tid] = _merge_title_record(media, row, meta)
                changed = True

        if skipped_irrelevant:
            log.info("sync_stats_all(%s): пропущено нерелевантных по kind: %d",
                     media, skipped_irrelevant)
        if repaired:
            log.info("sync_stats_all(%s): дозапрошена битая мета (kind был пуст): %d",
                     media, repaired)

        # Чистка существующих записей, чей kind не проходит фильтр
        # (самоочистка при изменении критерия или после обновления метаданных).
        stale_kind = [
            tid for tid, rec in titles.items()
            if rec.get("kind") and not is_relevant(media, rec["kind"])
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
    Дедупликация: один (id, event_type) на квартал.
    """
    # Смена оценки внутри квартала: обновляем score уже записанного
    # completed-события того же тайтла. Если completed-события в этом квартале
    # нет (тайтл завершён в прошлом квартале/до старта отслеживания) —
    # игнорируем: в текущем отчёте его всё равно нет.
    if event_type == "score_changed":
        if score is None:
            return cur
        try:
            tid = str((entry.get("target") or {}).get("id") or "")
            if not tid:
                return cur
            for ev in cur.get("events", []):
                if ev.get("id") == tid and ev.get("event") == "completed":
                    if ev.get("score") != score:
                        ev["score"] = score
                        log.info("Обновлена оценка в квартале: id=%s → %s", tid, score)
                    break
        except Exception as e:
            log.error("record_current_event(score_changed): %s", e)
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
            if ev.get("id") == tid and ev.get("event") == event_type:
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


def _quarter_titles(cur: dict, stats_all: dict, media: str, event: str) -> list[dict]:
    """
    Возвращает записи titles{} для тайтлов, у которых в текущем квартале
    было событие event ("completed"|"dropped"), джойня события с stats_all.
    Для completed подставляем score из события (на момент завершения).
    """
    titles = (stats_all.get(media) or {}).get("titles") or {}
    out = []
    seen = set()
    for ev in cur.get("events", []):
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


def _build_quarter_section(records: list[dict], media: str) -> list[str]:
    """Строки секции (аниме/манга) для отчётов на основе titles-записей квартала."""
    lines: list[str] = []
    if not records:
        return lines

    # Оценки
    scores = [r["score"] for r in records if _safe_int(r.get("score")) > 0]
    if scores:
        avg_personal = round(sum(scores) / len(scores), 1)
        # Средний рейтинг Shikimori по тем же тайтлам (у которых есть оценка)
        shiki = [r["shiki_score"] for r in records
                 if _safe_int(r.get("score")) > 0 and isinstance(r.get("shiki_score"), (int, float))]
        score_line = f"⭐ Средняя оценка: <b>{avg_personal}</b>"
        if shiki:
            avg_shiki = round(sum(shiki) / len(shiki), 1)
            diff = round(avg_personal - avg_shiki, 1)
            sign = "+" if diff >= 0 else ""
            score_line += f"  <i>(Shikimori: {avg_shiki}, {sign}{diff})</i>"
        lines.append(score_line)
        dist: dict = {}
        for s in scores:
            _bump(dist, s)
        block = _score_dist_block(dist)
        if block:
            lines.append("")
            lines.extend(block)

    # Топ по оценке
    top = sorted(
        [r for r in records if _safe_int(r.get("score")) > 0],
        key=lambda r: r["score"], reverse=True,
    )[:3]
    if top:
        lines.append("")
        lines.append("🏆 <b>Топ по оценке:</b>")
        for i, r in enumerate(top, 1):
            title = h(r.get("title") or "???")
            url = _rel_url(r.get("url"))
            link = f'<a href="{SHIKI_BASE_URL}{url}">{title}</a>' if url else title
            lines.append(f"  {i}. {link} — ⭐{r['score']}")

    # Хронология по году
    years = [(r["year"], r.get("title") or "???") for r in records
             if isinstance(r.get("year"), int) and r["year"] > 1900]
    if years:
        oldest = min(years, key=lambda x: x[0])
        newest = max(years, key=lambda x: x[0])
        avg_y = round(sum(y for y, _ in years) / len(years))
        lines.append("")
        if oldest[0] == newest[0]:
            lines.append(f"🗓️ Год выпуска: <b>{oldest[0]}</b>")
        else:
            lines.append(
                f"🗓️ Хронология: <b>{oldest[0]}</b> ({h(oldest[1])}) → "
                f"<b>{newest[0]}</b> ({h(newest[1])}),  ср. {avg_y}"
            )

    # Жанры/темы/аудитория из записей квартала
    genres: dict = {}
    themes: dict = {}
    demographic: dict = {}
    for r in records:
        for g in r.get("genres", []):
            _bump(genres, g)
        for t in r.get("themes", []):
            _bump(themes, t)
        for d in r.get("demographic", []):
            _bump(demographic, d)

    n_comp = len(records)  # база для процентов

    if media == "anime":
        studios: dict = {}
        origins: dict = {}
        total_eps = 0
        total_min = 0
        for r in records:
            for s in r.get("studios", []):
                _bump(studios, s)
            _bump(origins, r.get("origin"))
            eps = _safe_int(r.get("episodes_watched"))
            total_eps += eps
            dur = r.get("duration")
            if isinstance(dur, int) and dur > 0 and eps > 0:
                total_min += dur * eps
        if total_eps:
            lines.append(f"📺 Эпизодов: <b>{total_eps}</b>  (~{round(total_min / 60, 1)} ч.)")

        for block in (
            _top_block("🎭", "Жанры",     genres,      8, show_percent=True, total=n_comp),
            _top_block("🏷", "Темы",      themes,      8, show_percent=True, total=n_comp),
            _top_block("👥", "Аудитория", demographic, 99, show_percent=True, total=n_comp),
            _top_block("🎨", "Студии",    studios,     6),
            _top_block("📚", "Источники", origins,     99),
        ):
            if block:
                lines.append("")
                lines.extend(block)
    else:
        publishers: dict = {}
        total_ch = 0
        for r in records:
            for p in r.get("publishers", []):
                _bump(publishers, p)
            total_ch += _safe_int(r.get("chapters_read"))
        if total_ch:
            lines.append(f"📖 Глав прочитано: <b>{total_ch}</b>")

        for block in (
            _top_block("🎭", "Жанры",     genres,      8, show_percent=True, total=n_comp),
            _top_block("🏷", "Темы",      themes,      8, show_percent=True, total=n_comp),
            _top_block("👥", "Аудитория", demographic, 99, show_percent=True, total=n_comp),
            _top_block("🏢", "Издатели",  publishers,  6),
        ):
            if block:
                lines.append("")
                lines.extend(block)

    return lines


def _anime_block(cur: dict, comp: list[dict], drop: list[dict], plan: int, header: str) -> str:
    """Готовый текст блока АНИМЕ (одно сообщение)."""
    lines: list[str] = [header, ""]
    lines.append(f"✅ Завершено: <b>{len(comp)}</b>")
    if drop:
        lines.append(f"🗑 Брошено: {len(drop)}")
    if plan:
        lines.append(f"📋 В планируемое: {plan}")
    section = _build_quarter_section(comp, "anime")
    if section:
        lines.extend(section)
    elif not drop and not plan:
        lines.append("<i>Пока ничего не завершено.</i>")
    return "\n".join(lines)


def _manga_block(cur: dict, comp: list[dict], drop: list[dict], plan: int, header: str) -> str:
    """Готовый текст блока МАНГА (одно сообщение)."""
    lines: list[str] = [header, ""]
    lines.append(f"✅ Прочитано: <b>{len(comp)}</b>")
    if drop:
        lines.append(f"🗑 Брошено: {len(drop)}")
    if plan:
        lines.append(f"📋 В планируемое: {plan}")
    section = _build_quarter_section(comp, "manga")
    if section:
        lines.extend(section)
    elif not drop and not plan:
        lines.append("<i>Пока ничего не завершено.</i>")
    return "\n".join(lines)


def build_favourites_messages(stats: dict) -> list[str]:
    """
    Сообщение со списком любимого: аниме, манга, персонажи, люди.
    Пустые категории не показываются. Если избранного нет совсем —
    короткое сообщение-заглушка.
    """
    fav = stats.get("favourites") or {}
    blocks = [
        ("🎬", "Аниме",          fav.get("anime") or []),
        ("📚", "Манга",          fav.get("manga") or []),
        ("📖", "Ранобэ",         fav.get("ranobe") or []),
        ("👤", "Персонажи",      fav.get("characters") or []),
        ("🎨", "Люди индустрии", fav.get("people") or []),
    ]

    if not any(items for _, _, items in blocks):
        return ["❤️ <b>ЛЮБИМОЕ</b>\n\n<i>Список любимого пока пуст.</i>"]

    out: list[str] = ["❤️ <b>ЛЮБИМОЕ</b>"]
    for emoji, title, items in blocks:
        if not items:
            continue
        out.append("")
        out.append(f"{emoji} <b>{title}</b> ({len(items)})")
        out.extend(_fav_lines(items))

    return ["\n".join(out)]


def build_stats_all_messages(stats: dict) -> list[str]:
    """
    Список сообщений для /stats all, разбитый по темам: [аниме], [манга].
    Каждое самостоятельно проходит лимит Telegram.
    """
    a_agg = (stats.get("anime") or {}).get("aggregates") or {}
    m_agg = (stats.get("manga") or {}).get("aggregates") or {}

    updated = stats.get("updated_at") or ""
    upd_str = ""
    if updated:
        try:
            upd_str = datetime.fromisoformat(updated).strftime("%d.%m.%Y")
        except ValueError:
            pass

    # Пустая статистика — одно короткое сообщение
    if a_agg.get("total_completed", 0) == 0 and m_agg.get("total_completed", 0) == 0:
        return ["📊 <b>СТАТИСТИКА ЗА ВСЁ ВРЕМЯ</b>\n\n"
                "<i>Статистика ещё не собрана. Дай боту немного времени.</i>"]

    a_total = a_agg.get("total_completed", 0)
    m_total = m_agg.get("total_completed", 0)

    # ── Аниме ───────────────────────────────────
    a: list[str] = ["📊 <b>СТАТИСТИКА ЗА ВСЁ ВРЕМЯ</b>"]
    if upd_str:
        a.append(f"<i>актуально на {upd_str}</i>")
    a.append("")
    a.append(_section_header("🎬", "АНИМЕ"))
    a.append("")

    # Акцент сверху: сколько посмотрено · эпизоды/время, средняя оценка
    eps = a_agg.get("total_episodes_watched", 0)
    hrs = a_agg.get("total_hours_watched", 0)
    top_line = f"✅ Завершено: <b>{a_total}</b>"
    if eps:
        top_line += f"   ·   📺 {eps} эп (~{hrs} ч)"
    a.append(top_line)
    avg_a = _avg_score_from_dist(a_agg.get("score_dist", {}))
    if avg_a is not None:
        line = f"⭐ Средняя: <b>{avg_a}</b>"
        avg_shiki_a = a_agg.get("avg_shiki_completed")
        if isinstance(avg_shiki_a, (int, float)):
            diff = round(avg_a - avg_shiki_a, 1)
            sign = "+" if diff >= 0 else ""
            line += f"   <i>Shikimori: {round(avg_shiki_a, 1)} ({sign}{diff})</i>"
        a.append(line)

    # Детализация блоками
    for block in (
        _status_block_anime(a_agg),
        _kinds_block(a_agg.get("kinds", {}), _KIND_RU_ANIME),
        _score_dist_block(a_agg.get("score_dist", {})),
        _top_block("🎭", "Жанры",      a_agg.get("genres", {}),      8, show_percent=True, total=a_total),
        _top_block("🏷", "Темы",       a_agg.get("themes", {}),      8, show_percent=True, total=a_total),
        _top_block("👥", "Аудитория",  a_agg.get("demographic", {}), 99, show_percent=True, total=a_total),
        _top_block("🎨", "Студии",     a_agg.get("studios", {}),     6),
        _top_block("📚", "Источники",  a_agg.get("origins", {}),     99),
        _top_block("🔞", "Рейтинги",   a_agg.get("ratings", {}),     99),
    ):
        if block:
            a.append("")
            a.extend(block)

    # ── Манга ───────────────────────────────────
    m: list[str] = [_section_header("📚", "МАНГА"), ""]

    ch = m_agg.get("total_chapters_read", 0)
    vol = m_agg.get("total_volumes_read", 0)
    top_line = f"✅ Прочитано: <b>{m_total}</b>"
    if ch:
        top_line += f"   ·   📖 {ch} гл · {vol} томов"
    m.append(top_line)
    avg_m = _avg_score_from_dist(m_agg.get("score_dist", {}))
    if avg_m is not None:
        line = f"⭐ Средняя: <b>{avg_m}</b>"
        avg_shiki_m = m_agg.get("avg_shiki_completed")
        if isinstance(avg_shiki_m, (int, float)):
            diff = round(avg_m - avg_shiki_m, 1)
            sign = "+" if diff >= 0 else ""
            line += f"   <i>Shikimori: {round(avg_shiki_m, 1)} ({sign}{diff})</i>"
        m.append(line)

    for block in (
        _status_block_manga(m_agg),
        _kinds_block(m_agg.get("kinds", {}), _KIND_RU_MANGA),
        _score_dist_block(m_agg.get("score_dist", {})),
        _top_block("🎭", "Жанры",     m_agg.get("genres", {}),      8, show_percent=True, total=m_total),
        _top_block("🏷", "Темы",      m_agg.get("themes", {}),      8, show_percent=True, total=m_total),
        _top_block("👥", "Аудитория", m_agg.get("demographic", {}), 99, show_percent=True, total=m_total),
        _top_block("🏢", "Издатели",  m_agg.get("publishers", {}),  6),
    ):
        if block:
            m.append("")
            m.extend(block)

    return ["\n".join(a), "\n".join(m)]


def build_current_stats_messages(cur: dict, stats_all: dict) -> list[str]:
    """
    Список сообщений для /stats (текущий квартал), разбитый по темам:
      [0] аниме, [1] манга.
    Каждое сообщение самостоятельное и проходит лимит Telegram отдельно.
    """
    title_label = tracking_period_label(cur)

    comp_a = _quarter_titles(cur, stats_all, "anime", "completed")
    drop_a = _quarter_titles(cur, stats_all, "anime", "dropped")
    comp_m = _quarter_titles(cur, stats_all, "manga", "completed")
    drop_m = _quarter_titles(cur, stats_all, "manga", "dropped")

    plan_a = sum(1 for e in cur.get("events", []) if e["media"] == "anime" and e["event"] == "planned")
    plan_m = sum(1 for e in cur.get("events", []) if e["media"] == "manga" and e["event"] == "planned")

    header = f"📊 <b>Статистика {h(title_label)}</b>"
    if _is_partial_quarter(cur):
        header += "\n<i>⚠️ Квартал отслеживается не с самого начала — данные неполные.</i>"

    msgs: list[str] = []
    msgs.append(header + "\n\n" + _anime_block(cur, comp_a, drop_a, plan_a, _section_header("🎬", "АНИМЕ")))
    msgs.append(_manga_block(cur, comp_m, drop_m, plan_m, _section_header("📚", "МАНГА")))
    return msgs


def build_quarterly_report_messages(cur: dict, stats_all: dict, prev_quarter: dict | None) -> list[str]:
    """
    Список сообщений квартального отчёта для владельца, по темам:
      [0] заголовок + аниме, [1] манга, [2] сравнение + достижения.
    """
    title_label = tracking_period_label(cur)

    comp_a = _quarter_titles(cur, stats_all, "anime", "completed")
    drop_a = _quarter_titles(cur, stats_all, "anime", "dropped")
    comp_m = _quarter_titles(cur, stats_all, "manga", "completed")
    drop_m = _quarter_titles(cur, stats_all, "manga", "dropped")

    plan_a = sum(1 for e in cur.get("events", []) if e["media"] == "anime" and e["event"] == "planned")
    plan_m = sum(1 for e in cur.get("events", []) if e["media"] == "manga" and e["event"] == "planned")

    header = f"📊 <b>КВАРТАЛЬНЫЙ ОТЧЁТ</b>\n<b>{h(title_label)}</b>"
    if _is_partial_quarter(cur):
        header += "\n<i>⚠️ Квартал отслеживался не с самого начала — данные неполные.</i>"

    msgs: list[str] = []

    # Сообщение 1: заголовок + аниме
    msgs.append(header + "\n\n" + _anime_block(cur, comp_a, drop_a, plan_a, _section_header("🎬", "АНИМЕ")))

    # Сообщение 2: манга
    msgs.append(_manga_block(cur, comp_m, drop_m, plan_m, _section_header("📚", "МАНГА")))

    # Сообщение 3: сравнение + достижения
    extra: list[str] = []
    if prev_quarter:
        prev_a = prev_quarter.get("anime_completed", 0)
        prev_m = prev_quarter.get("manga_completed", 0)
        prev_label = quarter_label(prev_quarter.get("period") or "прошлый квартал")
        extra.append(f"📈 <b>Сравнение с {h(prev_label)}:</b>")
        extra.append(f"🎬 Аниме: {_pct_diff(len(comp_a), prev_a)}")
        extra.append(f"📚 Манга: {_pct_diff(len(comp_m), prev_m)}")

    all_comp = comp_a + comp_m
    ach: list[str] = []
    tens = [r for r in all_comp if r.get("score") == 10]
    if len(tens) >= 3:
        ach.append(f"💎 Десятку поставил {len(tens)} раза — строгий критик!")
    elif len(tens) == 1:
        ach.append("💎 Один безоговорочный шедевр за квартал.")
    total_drops = len(drop_a) + len(drop_m)
    if total_drops == 0 and all_comp:
        ach.append("🎯 Ни одного дропа — железная воля или идеальный вкус!")
    elif total_drops >= 5:
        ach.append(f"🗑️ {total_drops} дропов — знает, чего не хочет.")
    low = [r for r in all_comp if 0 < _safe_int(r.get("score")) <= 3]
    if low:
        n = len(low)
        ach.append(f"🧟 Домучил {n} тайтл{'а' if n < 5 else 'ов'} с оценкой ≤3 — стойкость.")

    if ach:
        if extra:
            extra.append("")
        extra.append("🏆 <b>Достижения:</b>")
        extra.extend(f"• {a}" for a in ach)

    if extra:
        msgs.append("\n".join(extra))

    return msgs


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

