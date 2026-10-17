# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Общие определения, независимые checkpoint и замороженные награды бота."""

import re
from collections.abc import (
    Callable,
    Mapping,
)
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from uuid import uuid4

from achievement_plans import (
    ACHIEVEMENTS_MAX_BYTES,
    AchievementStateError,
    _identity,
    _integer,
    decode_owner_state,
    encode_owner_state,
    validate_pending_plans,
)
from catchup_digest import split_html
from notification_outbox import LIFETIME

BOT_ACHIEVEMENTS_FILE_NAME = "bot_achievements.json"
BOT_ACHIEVEMENTS_MAX_BYTES = ACHIEVEMENTS_MAX_BYTES


@dataclass(frozen=True)
class Evaluation:
    """Калькулятор возвращает наблюдение и отдельно молчаливое исправление."""

    checkpoint: dict
    current_value: int | None
    quiet_value: int = 0


@dataclass(frozen=True)
class BotAchievement:
    """Явное определение семьи; метрика и её проверка принадлежат калькулятору."""

    family: str
    title: str
    criterion: Callable[[int], str]
    level_for: Callable[[int], int]
    next_level: Callable[[int], int]
    calculate: Callable[[dict | None, object], Evaluation]
    checkpoint_value: Callable[[dict], int | None]
    award_text: Callable[[int, int], str]


def achievement_catalog(*definitions: BotAchievement) -> Mapping[str, BotAchievement]:
    """Одна точка регистрации с устойчивыми уникальными идентификаторами."""
    result = {}
    for definition in definitions:
        if (
            not isinstance(definition, BotAchievement)
            or not isinstance(definition.family, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", definition.family) is None
            or definition.family in result or not definition.title.strip()
        ):
            raise ValueError("bot_achievement_definition")
        result[definition.family] = definition
    return MappingProxyType(result)


def _checkpoint_value(definition, checkpoint):
    """Ошибки сохранённой метрики входят в общую строгую границу чтения."""
    try:
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint")
        value = definition.checkpoint_value(checkpoint)
        if value is not None and not _integer(value):
            raise ValueError("value")
        return value
    except (ValueError, TypeError, RecursionError, OverflowError) as error:
        raise AchievementStateError("bot_achievement_checkpoint") from error


def capture_bot_achievements(state, inputs, *, catalog, profile, owner_id, now):
    """Публикуемый кандидат: baseline каждой семьи и высший новый порог."""
    if state is not None:
        validate_bot_state(state, catalog=catalog, profile=profile, owner_id=owner_id)
    result = deepcopy(state) if state is not None else {
        "version": 1, "state_id": uuid4().hex, "profile": profile, "owner_id": owner_id,
        "families": {}, "earned": {}, "pending": [],
    }
    if set(inputs) - set(catalog):
        raise AchievementStateError("bot_achievement_input")
    for family, source in inputs.items():
        definition = catalog[family]
        previous = result["families"].get(family)
        observation = definition.calculate(deepcopy(previous["checkpoint"]) if previous else None, source)
        if (
            not isinstance(observation, Evaluation) or not _integer(observation.quiet_value)
            or (observation.current_value is not None and not _integer(observation.current_value))
            or _checkpoint_value(definition, observation.checkpoint) != observation.current_value
            or (observation.current_value is not None and observation.quiet_value > observation.current_value)
        ):
            raise AchievementStateError("bot_achievement_evaluation")
        initialized = bool(previous and previous["initialized"])
        result["families"][family] = {
            "initialized": initialized or observation.current_value is not None,
            "current_value": observation.current_value, "checkpoint": deepcopy(observation.checkpoint),
        }
        if observation.current_value is None:
            continue
        quiet_level = max(result["earned"].get(family, 0), definition.level_for(observation.quiet_value))
        level = max(quiet_level, definition.level_for(observation.current_value))
        if level:
            result["earned"][family] = level
        if initialized and level > quiet_level:
            texts = split_html(definition.award_text(level, observation.current_value))
            result["pending"].append({
                "plan_id": uuid4().hex, "kind": "awards", "awards": {family: level},
                "created_at": now, "expires_at": now + LIFETIME, "total_parts": len(texts),
                "parts": [{"index": index, "text": text, "delivery": {
                    "status": "pending", "attempts": [], "prior_possible": False,
                    "next_attempt_at": now, "terminal_at": None, "reason": None, "duplicate_possible": False,
                }} for index, text in enumerate(texts)],
            })
    validate_bot_state(result, catalog=catalog, profile=profile, owner_id=owner_id)
    return result


def validate_bot_state(state, *, catalog, profile, owner_id):
    """Общая структура строга; семейство проверяет только свою метрику."""
    if (
        not isinstance(state, dict) or set(state) != {
            "version", "state_id", "profile", "owner_id", "families", "earned", "pending",
        } or type(state["version"]) is not int or state["version"] != 1
        or not _identity(state["state_id"]) or not _integer(state["owner_id"], 1)
        or not isinstance(state["families"], dict) or set(state["families"]) - set(catalog)
        or not isinstance(state["earned"], dict) or set(state["earned"]) - set(state["families"])
    ):
        raise AchievementStateError("bot_achievement_structure")
    if state["profile"] != profile or state["owner_id"] != owner_id:
        raise AchievementStateError("bot_achievement_profile")
    for family, saved in state["families"].items():
        definition = catalog[family]
        if (
            not isinstance(saved, dict) or set(saved) != {"initialized", "current_value", "checkpoint"}
            or type(saved["initialized"]) is not bool
            or (saved["current_value"] is not None and not _integer(saved["current_value"]))
            or _checkpoint_value(definition, saved["checkpoint"]) != saved["current_value"]
        ):
            raise AchievementStateError("bot_achievement_checkpoint")
        level = state["earned"].get(family, 0)
        if (
            not _integer(level) or definition.level_for(level) != level
            or (family in state["earned"] and not level)
            or (not saved["initialized"] and (level or saved["current_value"] is not None))
            or (saved["current_value"] is not None and definition.level_for(saved["current_value"]) > level)
            or not _integer(definition.next_level(level), level + 1)
        ):
            raise AchievementStateError("bot_achievement_level")
    validate_pending_plans(state["pending"], state["earned"])
    previous = {}
    for plan in state["pending"]:
        if plan["kind"] != "awards":
            raise AchievementStateError("bot_achievement_plan")
        for family, level in plan["awards"].items():
            if (
                family not in catalog or catalog[family].level_for(level) != level
                or level <= previous.get(family, 0)
            ):
                raise AchievementStateError("bot_achievement_plan")
            previous[family] = level


def bot_state_json(state, *, catalog, profile, owner_id):
    """Один бюджет на все семьи и весь резерв ещё ожидающих попыток."""
    validate_bot_state(state, catalog=catalog, profile=profile, owner_id=owner_id)
    return encode_owner_state(state, max_bytes=BOT_ACHIEVEMENTS_MAX_BYTES)


def parse_bot_member(raw, *, catalog, profile, owner_id):
    """Единый строгий формат для диска, диагностики и восстановления."""
    state = decode_owner_state(raw, max_bytes=BOT_ACHIEVEMENTS_MAX_BYTES)
    bot_state_json(state, catalog=catalog, profile=profile, owner_id=owner_id)
    return state
