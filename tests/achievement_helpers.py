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
