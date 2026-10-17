# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Утверждённые порядковые уровни с постоянным шагом после первых вех."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AchievementScale:
    """Конечное начало задаёт вехи, постоянный хвост не ограничивает уровень."""

    initial: tuple[int, ...]
    step: int

    def level_for(self, value: int) -> int:
        if value >= self.initial[-1]:
            return len(self.initial) + (value - self.initial[-1]) // self.step
        return sum(value >= threshold for threshold in self.initial)

    def threshold(self, level: int) -> int:
        if level == 0:
            return 0
        if level <= len(self.initial):
            return self.initial[level - 1]
        return self.initial[-1] + (level - len(self.initial)) * self.step

    def next_threshold(self, level: int) -> int:
        return self.threshold(level + 1)


READER_SCALE = AchievementScale((1, 5, 10, 25, 50), 25)
CHAPTER_SCALE = AchievementScale((25, 100, 250, 500, 1000), 1000)
VOLUME_SCALE = AchievementScale((1, 5, 10, 50, 100), 150)
RARE_SCALE = AchievementScale((1, 3, 5, 10, 15), 5)
TAXONOMY_SCALE = AchievementScale((5, 10, 25, 50, 75), 25)
