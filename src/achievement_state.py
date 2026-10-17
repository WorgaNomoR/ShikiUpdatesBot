# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Строгое независимое состояние достижений, дедупликации и доставки владельцу."""

from copy import deepcopy
from uuid import uuid4

from achievement_messages import render_achievement_messages
from achievement_plans import ACHIEVEMENTS_MAX_BYTES as ACHIEVEMENTS_MAX_BYTES
from achievement_plans import (
    AchievementStateError,
    _identity,
    _integer,
    _text,
    decode_owner_state,
    encode_owner_state,
    validate_pending_plans,
)
from achievement_view import achievement_summary
from catchup_digest import html_length
from notification_outbox import LIFETIME
from report_model import TELEGRAM_TEXT_LIMIT

ACHIEVEMENTS_FILE_NAME = "achievements.json"
_ROW_FIELDS = {"id", "user_id", "neko_id", "level", "progress", "created_at", "updated_at"}


class AchievementSourceError(ValueError):
    """Ответ не доказывает полный корректный снимок профиля."""


def parse_achievement_source(payload: object, user_id: int) -> list[dict]:
    """Проверить весь ответ; level>0, а не progress, определяет получение."""
    if not _integer(user_id, 1) or not isinstance(payload, list):
        raise AchievementSourceError("source_structure")
    rows = []
    identities = set()
    row_ids = set()
    for row in payload:
        if (
            not isinstance(row, dict) or not _ROW_FIELDS <= row.keys()
            or not _integer(row["id"], 1) or type(row["user_id"]) is not int
            or row["user_id"] != user_id or not _text(row["neko_id"])
            or not _integer(row["level"]) or not _integer(row["progress"])
            or row["progress"] > 100
            or any(row[key] is not None and not isinstance(row[key], str) for key in ("created_at", "updated_at"))
        ):
            raise AchievementSourceError("source_row")
        identity = (row["neko_id"], row["level"])
        if identity in identities or row["id"] in row_ids:
            raise AchievementSourceError("source_duplicate")
        identities.add(identity)
        row_ids.add(row["id"])
        rows.append({key: row[key] for key in sorted(_ROW_FIELDS)})
    return sorted(rows, key=lambda row: (row["neko_id"], row["level"]))


def earned_levels(rows: list[dict]) -> dict[str, int]:
    """Один максимальный полученный уровень на семейство."""
    result = {}
    for row in rows:
        if row["level"] > 0:
            family = row["neko_id"]
            result[family] = max(result.get(family, 0), row["level"])
    return result


def capture_achievements(
    state: dict | None, rows: list[dict], *, profile: str, user_id: int,
    owner_id: int, now: float, base_url: str,
    labels: dict[tuple[str, int], tuple[str, str]] | None = None,
    startup: tuple[int, str] | None = None,
) -> dict:
    """Единый кандидат: снимок, максимумы и замороженный план до отправки."""
    rows = parse_achievement_source(rows, user_id)
    if state is not None:
        validate_achievement_state(state, profile=profile, owner_id=owner_id)
        if state["user_id"] != user_id:
            raise AchievementStateError("achievement_profile")
    baseline = state is None
    result = deepcopy(state) if state is not None else {
        "version": 2 if startup is not None else 1, "state_id": uuid4().hex, "profile": profile,
        "user_id": user_id, "owner_id": owner_id, "initialized": True,
        "snapshot": [], "earned": {}, "pending": [],
    }
    result["snapshot"] = rows
    observed = earned_levels(rows)
    previous = dict(result["earned"])
    changes = {family: level for family, level in observed.items() if level > previous.get(family, 0)}
    result["earned"].update(changes)
    if baseline or changes:
        chunks = render_achievement_messages(
            observed if baseline else changes, previous,
            baseline=baseline, profile=profile, base_url=base_url, labels=labels,
        )
        startup_baseline = baseline and startup is not None
        if startup_baseline:
            summary = achievement_summary(rows)
            text = startup[1] + "\n\n" + summary
            if html_length(text) > TELEGRAM_TEXT_LIMIT:
                text = "🟢 Бот запущен\n\n" + summary
            chunks = [text]
        plan = {
            "plan_id": uuid4().hex, "kind": "startup" if startup_baseline else "baseline" if baseline else "awards",
            "awards": observed if baseline else changes,
            "created_at": now, "expires_at": now + LIFETIME,
            "total_parts": len(chunks),
            "parts": [{
                "index": index, "text": text, "delivery": {
                    "status": "pending", "attempts": [], "prior_possible": False,
                    "next_attempt_at": now, "terminal_at": None,
                    "reason": None, "duplicate_possible": False,
                },
            } for index, text in enumerate(chunks)],
        }
        if startup_baseline:
            plan["startup_message_id"] = startup[0]
        result["pending"].append(plan)
    return result


def validate_achievement_state(state: object, *, profile: str, owner_id: int) -> None:
    """Проверка при чтении/импорте не сбрасывает начало и не перестраивает текст."""
    if (
        not isinstance(state, dict) or set(state) != {
            "version", "state_id", "profile", "user_id", "owner_id", "initialized",
            "snapshot", "earned", "pending",
        } or type(state["version"]) is not int or state["version"] not in (1, 2)
        or not _identity(state["state_id"]) or state["initialized"] is not True
        or not _integer(state["user_id"], 1) or not _integer(state["owner_id"], 1)
    ):
        raise AchievementStateError("achievement_structure")
    if state["profile"] != profile or state["owner_id"] != owner_id:
        raise AchievementStateError("achievement_profile")
    try:
        canonical = parse_achievement_source(state["snapshot"], state["user_id"])
    except AchievementSourceError as exc:
        raise AchievementStateError("achievement_snapshot") from exc
    if state["snapshot"] != canonical:
        raise AchievementStateError("achievement_snapshot_order")
    earned = state["earned"]
    if not isinstance(earned, dict) or any(not _text(k) or not _integer(v, 1) for k, v in earned.items()):
        raise AchievementStateError("achievement_earned")
    if any(earned.get(k, 0) < v for k, v in earned_levels(canonical).items()):
        raise AchievementStateError("achievement_deduplication")
    validate_pending_plans(state["pending"], earned, version=state["version"])


def achievement_state_json(state: dict, *, profile: str, owner_id: int) -> str:
    """Оплачиваем фактический размер UTF-8 плюс весь будущий рост попыток."""
    validate_achievement_state(state, profile=profile, owner_id=owner_id)
    return encode_owner_state(state, max_bytes=ACHIEVEMENTS_MAX_BYTES)


def parse_achievement_member(raw: bytes, *, profile: str, owner_id: int) -> dict:
    """Одинаковый ограниченный строгий разбор для диска и восстановления."""
    state = decode_owner_state(raw, max_bytes=ACHIEVEMENTS_MAX_BYTES)
    achievement_state_json(state, profile=profile, owner_id=owner_id)
    return state
