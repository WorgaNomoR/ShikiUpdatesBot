# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Подписи со страницы профиля: оформление без собственного каталога достижений."""

import re
from dataclasses import dataclass
from html.parser import HTMLParser

ACHIEVEMENT_PAGE_MAX_BYTES = 1024 * 1024
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


@dataclass(frozen=True)
class AchievementMetadata:
    """Необязательное оформление точного уровня из одной страницы профиля."""

    title: str
    category: str
    criterion: str = ""
    is_binary: bool = False


class _AchievementLabels(HTMLParser):
    """Карточки и значки связываем с точным уровнем и относительной ссылкой."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.labels = {}
        self.criteria = {}
        self._criterion_conflicts = set()
        self._binary = {}
        self._stack = []
        self._conflicts = set()

    def _add(self, href, title, level, criterion=None, is_binary=False):
        match = re.fullmatch(r"/achievements/([a-z0-9_-]+)/([a-z0-9_-]+)", href or "")
        if match is None or not title or not title.strip() or len(title) > 512 or level is None:
            return
        category, family = match.groups()
        key = (family, level)
        value = AchievementMetadata(title.strip(), category)
        if key in self._conflicts:
            return None
        if key in self.labels and self.labels[key] != value:
            self.labels.pop(key)
            self._conflicts.add(key)
        else:
            self.labels[key] = value
        # Противоречивый вид карточки не позволяет скрыть уровень.
        binary = bool(is_binary and level <= 1)
        self._binary[key] = binary and self._binary.get(key, True)
        # У авторских карточек hint повторяет имя и ничего не объясняет.
        if not isinstance(criterion, str) or len(criterion) > 512:
            return
        criterion = " ".join(criterion.split())
        if not criterion or criterion.casefold() == title.strip().casefold() or key in self._criterion_conflicts:
            return
        if key in self.criteria and self.criteria[key] != criterion:
            self.criteria.pop(key)
            self._criterion_conflicts.add(key)
        else:
            self.criteria[key] = criterion

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        levels = [int(name[6:]) for name in classes if re.fullmatch(r"level-[0-9]{1,6}", name)]
        level = levels[0] if len(levels) == 1 else None
        card = (
            attributes.get("data-title"), level, attributes.get("data-hint"), "is-badge" in classes,
        ) if "b-achievement" in classes else None
        if tag == "a":
            if "b-badge" in classes:
                self._add(attributes.get("href"), attributes.get("title"), level, attributes.get("data-hint"), True)
            else:
                # У авторских значков ссылка бывает только на изображении.
                parent = next((context for _, context in reversed(self._stack) if context is not None), None)
                if parent is not None:
                    self._add(attributes.get("href"), *parent)
        if tag not in _VOID_TAGS:
            self._stack.append((tag, card))

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)


def parse_achievement_metadata(html: str) -> dict[tuple[str, int], AchievementMetadata]:
    """Неизвестная/изменённая разметка оставляет достижения без украшений."""
    if not isinstance(html, str) or len(html) > ACHIEVEMENT_PAGE_MAX_BYTES:
        return {}
    parser = _AchievementLabels()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):
        return {}
    multilevel = {family for family, level in parser.labels if level > 1}
    return {
        key: AchievementMetadata(
            value.title, value.category, parser.criteria.get(key, ""),
            parser._binary.get(key, False) and key[0] not in multilevel,
        )
        for key, value in parser.labels.items()
    }


def parse_achievement_labels(html: str) -> dict[tuple[str, int], tuple[str, str]]:
    """Прежний контракт подписей для новых уведомлений, без условий выдачи."""
    return {
        key: (value.title, value.category)
        for key, value in parse_achievement_metadata(html).items()
    }
