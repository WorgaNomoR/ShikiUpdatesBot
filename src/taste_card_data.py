# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Проверка локального среза карточки без изменения сохранённых списков."""

from datetime import (
    datetime,
    timezone,
)
from fractions import Fraction
from math import isfinite
from unicodedata import category as unicode_category
from urllib.parse import urlsplit

from shiki_api import ANIME_ALLOWED_KINDS
from stats import classify_manga_presentation_kind
from storage import (
    STATS_ALL_INVALID,
    STATS_ALL_MISSING,
    StatsAllSnapshot,
)
from taste_card import (
    CATEGORY_LABELS,
    TasteInput,
    TasteTitle,
)
from utils import (
    canonical_title_id,
    personal_score,
)

_STATUSES = {"planned", "watching", "rewatching", "completed", "on_hold", "dropped"}


def _text(value: object, limit: int = 2048) -> str:
    """Оставить обычный отображаемый текст, не интерпретируя разметку."""
    if not isinstance(value, str):
        return ""
    value = "".join(char for char in value[:limit] if unicode_category(char) not in {"Cc", "Cf", "Cs"})
    return " ".join(value.split())


def _labels(value: object) -> tuple[str, ...]:
    """Учитывать каждую пригодную метку один раз; слишком большой список неизвестен."""
    if not isinstance(value, list) or len(value) > 100:
        return ()
    labels = {}
    for item in value:
        if isinstance(item, str) and len(item) <= 128:
            label = _text(item, 128)
            if label:
                labels.setdefault(label.casefold(), label)
    return tuple(labels[key] for key in sorted(labels))


def _community_score(value: object) -> Fraction | None:
    """Личный ноль и отсутствующий общественный рейтинг не образуют пару."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and len(value) > 32:
        return None
    try:
        number = float(value)
        if not isfinite(number) or not 0 < number <= 10:
            return None
        return Fraction(str(value).strip())
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def _timestamp(value: object, now: datetime) -> datetime | None:
    """Только собственная aware-дата успешного экспорта, без подстановок."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        parsed = parsed.astimezone(timezone.utc)
        return parsed if parsed <= now else None
    except (ValueError, OverflowError):
        return None


def build_taste_input(
    snapshot: StatsAllSnapshot,
    category: str,
    *,
    profile: str,
    base_url: str,
    now: datetime,
) -> TasteInput:
    """Создать неизменяемую категорию из одной опубликованной локальной копии."""
    if category not in CATEGORY_LABELS:
        raise ValueError("taste_category")
    now = now.astimezone(timezone.utc)
    initial = dict(profile=_text(profile, 128), category=category, generated_at=now, synced_at=None)
    if snapshot.state == STATS_ALL_MISSING:
        return TasteInput(**initial, state="missing")
    if snapshot.state == STATS_ALL_INVALID or not isinstance(snapshot.data, dict):
        return TasteInput(**initial, state="invalid")
    media = "anime" if category == "anime" else "manga"
    domain = snapshot.data.get(media)
    if not isinstance(domain, dict) or not isinstance(domain.get("titles"), dict):
        return TasteInput(**initial, state="invalid")
    initial["synced_at"] = _timestamp(domain.get("list_synced_at"), now)
    base = urlsplit(base_url)
    if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password:
        raise ValueError("taste_base_url")
    base_url = f"{base.scheme}://{base.netloc}"
    raw_titles = {}
    conflicts = set()
    excluded = unresolved = planned = 0
    for raw_id, record in domain["titles"].items():
        title_id = canonical_title_id(raw_id)
        if title_id is None or not isinstance(record, dict):
            excluded += 1
            continue
        if title_id in raw_titles and raw_titles[title_id] != record:
            conflicts.add(title_id)
        raw_titles.setdefault(title_id, record)
    excluded += len(conflicts)
    titles = []
    for title_id in sorted(raw_titles, key=int):
        if title_id in conflicts:
            continue
        record = raw_titles[title_id]
        kind = record.get("kind")
        kind = kind.strip().lower() if isinstance(kind, str) else ""
        if media == "anime":
            if kind not in ANIME_ALLOWED_KINDS:
                if kind not in {"special", "tv_special", "music", "pv", "cm"}:
                    unresolved += 1
                continue
        else:
            presentation = classify_manga_presentation_kind(kind)
            if presentation == "unknown":
                if kind not in {"one_shot", "doujin"}:
                    unresolved += 1
                continue
            if presentation != category:
                continue
        status = record.get("status")
        status = status.strip().lower() if isinstance(status, str) else ""
        if status not in _STATUSES:
            excluded += 1
            continue
        if status == "planned":
            planned += 1
            continue
        year = record.get("year")
        year = year if type(year) is int and 1 <= year <= now.year else None
        title = _text(record.get("title")) or _text(record.get("title_en")) or f"Тайтл #{title_id}"
        titles.append(TasteTitle(
            id=int(title_id), title=title, url=f"{base_url}/{media}s/{title_id}",
            status=status, kind="light_novel" if kind == "ranobe" else kind,
            score=personal_score(record.get("score")),
            community=_community_score(record.get("shiki_score")), year=year,
            genres=_labels(record.get("genres")), themes=_labels(record.get("themes")),
        ))
    state = "ready"
    if not titles:
        state = "unresolved" if unresolved else "invalid" if excluded else (
            "missing" if not domain["titles"] and initial["synced_at"] is None else "empty"
        )
    return TasteInput(
        **initial, titles=tuple(titles), state=state,
        unresolved=unresolved, excluded=excluded, planned=planned,
    )
