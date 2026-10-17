# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Короткая сборка Reader через реальный общий каталог для интеграционных тестов."""

import bot_achievement_catalog as bot_catalog
from bot_achievements import capture_bot_achievements


def capture_reader(state, completed, *, profile, owner_id, now):
    return capture_bot_achievements(
        state, {"reader": completed}, catalog=bot_catalog.BOT_ACHIEVEMENTS,
        profile=profile, owner_id=owner_id, now=now,
    )


def catalog_inputs(count, *, chapters=100, volumes=5):
    """Общий реальный вход каталога для доставки и восстановления."""
    from reading_catalog import TAXONOMY_FAMILIES
    from stats import reading_catalog_observation

    rows = [{"target_id": index, "status": "completed", "chapters": chapters, "volumes": volumes}
            for index in range(1, count + 1)]
    metadata = {str(index): {"kind": "novel", "reading_evidence": {
        "chapters": 100, "year": 1999,
        "taxonomy": [[tag_id, kind] for _, _, kind, tag_id, _ in TAXONOMY_FAMILIES],
    }} for index in range(1, count + 1)}
    return reading_catalog_observation(rows, metadata)
