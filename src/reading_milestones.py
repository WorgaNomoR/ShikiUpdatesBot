# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Метрика «Читателя»: текущие завершения, пригодность и исправления метаданных."""

import re

from achievement_plans import AchievementStateError
from achievement_scales import READER_SCALE
from bot_achievements import (
    BotAchievement,
    Evaluation,
)


def reading_tier(count: int) -> int:
    """Порядковый уровень по числу завершённых произведений."""
    return READER_SCALE.level_for(count)


def next_reading_tier(tier: int) -> int:
    """Следующий ещё не полученный порог, включая нулевой старт."""
    return READER_SCALE.next_threshold(tier)


def reading_count(completed):
    """Каждый завершённый ID имеет пригодность либо явный пробел меты."""
    if not isinstance(completed, dict) or any(
        not isinstance(key, str) or re.fullmatch(r"[1-9][0-9]*", key) is None
        or (value is not None and type(value) is not bool)
        for key, value in completed.items()
    ):
        raise AchievementStateError("reading_snapshot")
    return None if any(value is None for value in completed.values()) else sum(completed.values())


def calculate_reader(previous, completed):
    """Старые исправленные ID сверяем молча перед настоящими новыми завершениями."""
    count = reading_count(completed)
    quiet = sum(value is True and key in (previous or {}) for key, value in completed.items())
    return Evaluation(dict(completed), count, quiet)


def reading_criterion(tier):
    """Критерий полученного порога, общий для манги и ранобэ."""
    threshold = READER_SCALE.threshold(tier)
    works = "1 завершённое произведение" if threshold == 1 else f"{threshold} завершённых произведений"
    return f"{works} манги и ранобэ"


def reading_award_text(tier, count):
    """Семейство задаёт свою обычную поздравительную формулировку."""
    return (
        f"✨ <b>Читатель — уровень {tier}</b>\n"
        f"Завершённых произведений в списке: <b>{count}</b>. "
        "Манга и ранобэ вместе — книжная полка растёт! 🏆"
    )


READER = BotAchievement(
    family="reader", title="Читатель", criterion=reading_criterion,
    level_for=reading_tier, next_level=next_reading_tier, calculate=calculate_reader,
    checkpoint_value=reading_count, award_text=reading_award_text,
    level_threshold=READER_SCALE.threshold,
)
