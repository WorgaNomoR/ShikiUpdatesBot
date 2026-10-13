# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Обычные поздравления: метаданные украшают, но не определяют достижения."""

from html import escape
from urllib.parse import quote

from catchup_digest import split_html


def achievement_link(
    base_url: str, profile: str, family: str | None = None, category: str | None = None,
) -> str:
    """Неизвестное семейство ведёт на список достижений самого профиля."""
    path = f"/{quote(profile, safe='')}/achievements"
    if family is not None and category is not None:
        path = f"/achievements/{quote(category, safe='')}/{quote(family, safe='')}"
    return escape(base_url.rstrip("/") + path, quote=True)


def render_achievement_messages(
    awards: dict[str, int], previous: dict[str, int], *,
    baseline: bool, base_url: str, profile: str,
    labels: dict[tuple[str, int], tuple[str, str]] | None = None,
) -> list[str]:
    """Заморозить одну начальную сводку либо сгруппированные радости."""
    if baseline:
        count = len(awards)
        text = (
            "🏆 <b>Достижения Shikimori на месте!</b>\n\n"
            f"Полученных достижений: <b>{count}</b>. "
            "Существующие достижения запомнил. Следующие победы и новые уровни "
            "отпразднуем вместе! 🎉\n\n"
            f'<a href="{achievement_link(base_url, profile)}">Достижения на Shikimori</a>'
        )
    else:
        lines = ["🎉 <b>Новая порция побед на Shikimori!</b> 🏆", ""]
        for family, level in sorted(awards.items()):
            label, category = (labels or {}).get((family, level), (f"Достижение «{family}»", None))
            link = achievement_link(base_url, profile, family, category)
            before = previous.get(family, 0)
            change = f"уровень {before} → <b>{level}</b>" if before else f"новое достижение, уровень <b>{level}</b>"
            lines.append(f'✨ <a href="{link}">{escape(label)}</a> — {change}')
        text = "\n".join(lines)
    return split_html(text)
