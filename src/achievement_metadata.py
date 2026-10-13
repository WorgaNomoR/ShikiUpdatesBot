# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Подписи со страницы профиля: оформление без собственного каталога достижений."""

import re
from html.parser import HTMLParser

ACHIEVEMENT_PAGE_MAX_BYTES = 1024 * 1024
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _AchievementLabels(HTMLParser):
    """Карточки и значки связываем с точным уровнем и относительной ссылкой."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.labels = {}
        self._stack = []
        self._conflicts = set()

    def _add(self, href, title, level):
        match = re.fullmatch(r"/achievements/([a-z0-9_-]+)/([a-z0-9_-]+)", href or "")
        if match is None or not title or not title.strip() or len(title) > 512 or level is None:
            return
        category, family = match.groups()
        key = (family, level)
        value = (title.strip(), category)
        if key in self._conflicts:
            return
        if key in self.labels and self.labels[key] != value:
            self.labels.pop(key)
            self._conflicts.add(key)
        else:
            self.labels[key] = value

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        levels = [int(name[6:]) for name in classes if re.fullmatch(r"level-[0-9]{1,6}", name)]
        level = levels[0] if len(levels) == 1 and levels[0] > 0 else None
        card = (attributes.get("data-title"), level) if "b-achievement" in classes else None
        if tag == "a":
            if "b-badge" in classes:
                self._add(attributes.get("href"), attributes.get("title"), level)
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


def parse_achievement_labels(html: str) -> dict[tuple[str, int], tuple[str, str]]:
    """Неизвестная/изменённая разметка оставляет достижения без украшений."""
    if not isinstance(html, str) or len(html) > ACHIEVEMENT_PAGE_MAX_BYTES:
        return {}
    parser = _AchievementLabels()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):
        return {}
    return parser.labels
