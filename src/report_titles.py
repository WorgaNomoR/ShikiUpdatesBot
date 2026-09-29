# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Общие типизированные названия и ссылки для статистики и избранного."""

from config import SHIKI_BASE_URL
from report_model import (
    Link,
    Poster,
    Text,
    Title,
)
from utils import _rel_url


def _title_inline(record: dict, *, poster: bool = False) -> Link | Text | Title:
    """Подготовить название, ссылку и необязательную подсказку постера."""
    title = str(record.get("title") or "???")
    relative_url = _rel_url(record.get("url"))
    full_url = f"{SHIKI_BASE_URL}{relative_url}" if relative_url else None
    if poster:
        raw_poster = record.get("poster_url")
        poster_url = raw_poster.strip() or None if isinstance(raw_poster, str) else None
        return Title(title, full_url, Poster(poster_url))
    if relative_url:
        return Link(title, full_url)
    return Text(title)
