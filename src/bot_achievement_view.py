# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Общее публичное представление семей бота без очереди и личности владельца."""

from dataclasses import dataclass

from report_model import (
    Bold,
    Italic,
    heading,
    line,
    section,
)


@dataclass(frozen=True)
class PublicBotAchievement:
    """Минимальный локальный срез любой семьи для обоих форматов отчёта."""

    family: str
    title: str
    initialized: bool
    level: int
    value: int | None
    target: int
    criterion: str


def bot_achievement_snapshot(state, *, catalog):
    """Определение предоставляет название, условие и следующий порог."""
    rows = []
    for family, definition in catalog.items():
        saved = state["families"].get(family) if state else None
        level = state["earned"].get(family, 0) if state else 0
        target = definition.next_level(level)
        rows.append(PublicBotAchievement(
            family, definition.title, bool(saved and saved["initialized"]), level,
            saved["current_value"] if saved else None, target, definition.criterion(level or target),
        ))
    return tuple(rows)


def bot_achievement_sections(snapshot=None, *, invalid=False):
    """Полученные и первые положительные продвижения без дублирования семей."""
    sections = [section(heading("🤖 ", Bold("Бота"), level=2))]
    if invalid:
        return (*sections, section(line(Italic("Достижения бота временно недоступны. Попробуй позже."))))
    if snapshot is None:
        return (*sections, section(line(Italic("Достижения бота ещё не рассчитаны."))))
    earned = sorted((row for row in snapshot if row.level > 0), key=lambda row: (row.title.casefold(), row.family))
    progressing = sorted(
        (row for row in snapshot if row.initialized and row.level == 0 and row.value is not None and row.value > 0),
        key=lambda row: (-row.value / row.target, row.family),
    )
    for emoji, name, group in [("🏆", "Полученные", earned), ("📈", "В процессе получения", progressing)]:
        if not group:
            continue
        items = [heading(f"{emoji} ", Bold(name), f" · {len(group)}", level=3, collapsible=True)]
        for row in group:
            status = f" — уровень {row.level}" if row.level else ""
            if row.value is None:
                status += " · прогресс пока неизвестен"
            else:
                percentage = row.value * 100 // row.target
                progress = f"{percentage}%" if percentage or row.value == 0 else "менее 1%"
                status += f" · {progress} до следующего уровня" if row.level else f" — {progress}"
            items.append(line("  • ", Bold(row.title), status, "\n", Italic(row.criterion)))
            if row.value is not None and row.value < row.level:
                items.append(line(Italic("Полученный уровень сохранён, хотя текущий прогресс уменьшился.")))
        sections.append(section(*items))
    notices = [
        line(Italic(f"«{row.title}»: ещё не рассчитан."))
        for row in snapshot if not row.initialized
    ]
    notices.extend(
        line(Italic(f"«{row.title}»: прогресс пока неизвестен."))
        for row in snapshot if row.initialized and not row.level and row.value is None
    )
    if not earned and not progressing and not notices:
        notices.append(line(Italic("Полученных достижений бота пока нет.")))
    if notices:
        sections.append(section(*notices))
    return tuple(sections)
