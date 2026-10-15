# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Публичное представление текущего снимка без истории доставки и максимумов."""

from html import unescape

from achievement_messages import achievement_link
from report_asset_ids import REPORT_ACHIEVEMENTS_MEDIA
from report_model import (
    Bold,
    Gallery,
    Italic,
    Link,
    Poster,
    Report,
    heading,
    line,
    section,
    unit,
)

# Только порядок оформления; неизвестные категории сохраняются после известных.
_EARNED_CATEGORY_ORDER = {"common": 0, "genre": 1, "franchise": 2, "author": 3}


def _earned_sort_key(row: dict, metadata: dict | None) -> tuple:
    """Сортировать по категории точного уровня, названию и стабильному идентификатору."""
    family = row["neko_id"]
    decoration = (metadata or {}).get((family, row["level"]))
    if decoration is None:
        return (5, "", family.casefold(), family)
    return (
        _EARNED_CATEGORY_ORDER.get(decoration.category, 4), decoration.category,
        decoration.title.casefold(), family,
    )


def current_families(rows: list[dict]) -> list[dict]:
    """Выбрать высший присутствующий уровень, включая явно неполученный нулевой."""
    families = {}
    for row in rows:
        family = row["neko_id"]
        if family not in families or row["level"] > families[family]["level"]:
            families[family] = row
    return [families[key] for key in sorted(families)]


def achievement_summary(rows: list[dict] | None, *, invalid: bool = False) -> str:
    """Короткий счётчик источника; недоступность не превращаем в ноль."""
    if invalid:
        return "🏆 Достижения временно недоступны."
    if rows is None:
        return "🏆 Достижения пока не загружены."
    count = sum(row["level"] > 0 for row in current_families(rows))
    return f"🏆 Получено достижений: <b>{count}</b>."


def build_achievement_report(
    rows: list[dict] | None, *, base_url: str, profile: str,
    metadata: dict | None = None, invalid: bool = False,
) -> Report:
    """Одна строка на семейство без повторов; безопасные поля и полные continuations."""
    title = heading("🏆 ", Bold("ДОСТИЖЕНИЯ"), level=1)
    sections = [section(Gallery((Poster(REPORT_ACHIEVEMENTS_MEDIA),)), title)]
    if invalid:
        sections.append(section(line(Italic("Достижения временно недоступны. Попробуй позже."))))
    elif rows is None:
        sections.append(section(line(Italic("Достижения ещё не загружены."))))
    else:
        families = current_families(rows)
        earned = [row for row in families if row["level"] > 0]
        earned.sort(key=lambda row: _earned_sort_key(row, metadata))
        progressing = [row for row in families if row["level"] == 0 and row["progress"] > 0]
        progressing.sort(key=lambda row: (-row["progress"], row["neko_id"]))
        if not families:
            sections.append(section(line(Italic("Список достижений пуст."))))
        elif not earned and not progressing:
            sections.append(section(line(Italic("Полученных достижений пока нет."))))
        groups = [("🏆", "Полученные", earned, False), ("📈", "В процессе получения", progressing, True)]
        for emoji, name, group, is_progress in groups:
            if not group:
                continue
            items = [heading(f"{emoji} ", Bold(name), f" · {len(group)}", level=2, collapsible=True)]
            for row in group:
                family, level = row["neko_id"], row["level"]
                decoration = (metadata or {}).get((family, level))
                if decoration:
                    label = Link(decoration.title, unescape(achievement_link(base_url, profile, family, decoration.category)))
                else:
                    # Неограниченный неизвестный идентификатор продолжается как обычный текст.
                    label = f"Достижение «{family}»"
                if is_progress:
                    status = f" — {row['progress']}%" if row["progress"] < 100 else ""
                else:
                    binary = bool(decoration and decoration.is_binary and level == 1)
                    status = "" if binary else f" — уровень {level}"
                    if 0 < row["progress"] < 100:
                        status += f" — {row['progress']}%" if binary else f" · {row['progress']}% до следующего уровня"
                criterion = decoration.criterion if decoration else ""
                parts = ("\n", Italic(criterion)) if criterion else ()
                items.append(line("  • ", label, status, *parts))
            sections.append(section(*items))
    return Report((unit(*sections),))
