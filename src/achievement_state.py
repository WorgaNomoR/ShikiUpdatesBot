# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Строгое независимое состояние достижений, дедупликации и доставки владельцу."""

import json
import math
import re
from copy import deepcopy
from uuid import uuid4

from achievement_messages import render_achievement_messages
from achievement_view import achievement_summary
from catchup_digest import html_length
from notification_outbox import (
    LIFETIME,
    MAX_ATTEMPTS,
    OUTCOMES,
    recipient_progress_reserve,
)
from report_model import TELEGRAM_TEXT_LIMIT

ACHIEVEMENTS_FILE_NAME = "achievements.json"
ACHIEVEMENTS_MAX_BYTES = 8 * 1024 * 1024
_ROW_FIELDS = {"id", "user_id", "neko_id", "level", "progress", "created_at", "updated_at"}


class AchievementStateError(ValueError):
    """Состояние сохраняем, небезопасную работу останавливаем."""


class AchievementSourceError(ValueError):
    """Ответ не доказывает полный корректный снимок профиля."""


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _time(value):
    return type(value) in (int, float) and 0 <= value <= 10**12 and math.isfinite(value)


def _identity(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{32}", value) is not None


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
            chunks = [startup[1] + "\n\n" + achievement_summary(rows)]
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
    if not isinstance(state["pending"], list):
        raise AchievementStateError("achievement_pending")
    identities = set()
    baseline_count = 0
    for plan in state["pending"]:
        startup = isinstance(plan, dict) and plan.get("kind") == "startup"
        fields = {"plan_id", "kind", "awards", "created_at", "expires_at", "total_parts", "parts"}
        if startup:
            fields.add("startup_message_id")
        if (
            not isinstance(plan, dict) or set(plan) != fields
            or not _identity(plan["plan_id"]) or plan["plan_id"] in identities
            or plan["kind"] not in ("baseline", "awards", "startup")
            or (startup and (state["version"] != 2 or not _integer(plan["startup_message_id"])
                            or plan["total_parts"] != 1))
            or not _time(plan["created_at"]) or not _time(plan["expires_at"])
            or plan["expires_at"] != plan["created_at"] + LIFETIME
            or not _integer(plan["total_parts"], 1)
            or not isinstance(plan["parts"], list) or not plan["parts"]
            or not isinstance(plan["awards"], dict)
            or any(not _text(k) or not _integer(v, 1) or earned.get(k, 0) < v for k, v in plan["awards"].items())
            or (plan["kind"] == "awards" and not plan["awards"])
        ):
            raise AchievementStateError("achievement_plan")
        identities.add(plan["plan_id"])
        baseline_count += plan["kind"] in {"baseline", "startup"}
        indices = []
        for part in plan["parts"]:
            if (
                not isinstance(part, dict) or set(part) != {"index", "text", "delivery"}
                or not _integer(part["index"]) or not _text(part["text"])
            ):
                raise AchievementStateError("achievement_part")
            try:
                length = html_length(part["text"])
            except (ValueError, UnicodeError, RecursionError):
                raise AchievementStateError("achievement_html") from None
            if not 0 < length <= TELEGRAM_TEXT_LIMIT:
                raise AchievementStateError("achievement_html")
            indices.append(part["index"])
            _validate_delivery(part["delivery"], plan)
        if (
            len(indices) != plan["total_parts"] - indices[0]
            or any(index != indices[0] + offset for offset, index in enumerate(indices))
        ):
            raise AchievementStateError("achievement_parts")
    if baseline_count > 1:
        raise AchievementStateError("achievement_baseline")


def _validate_delivery(delivery, plan):
    """Храним только незавершённые сообщения; завершённые удаляем при подтверждении."""
    if (
        not isinstance(delivery, dict) or set(delivery) != {
            "status", "attempts", "prior_possible", "next_attempt_at", "terminal_at", "reason", "duplicate_possible",
        } or delivery["status"] != "pending" or delivery["prior_possible"] is not False
        or delivery["duplicate_possible"] is not False
        or delivery["reason"] is not None or delivery["terminal_at"] is not None
        or not _time(delivery["next_attempt_at"]) or delivery["next_attempt_at"] < plan["created_at"]
        or not isinstance(delivery["attempts"], list) or len(delivery["attempts"]) > MAX_ATTEMPTS
    ):
        raise AchievementStateError("achievement_delivery")
    previous = plan["created_at"]
    for attempt in delivery["attempts"]:
        if (
            not isinstance(attempt, dict) or set(attempt) != {"at", "outcome"}
            or not _time(attempt["at"]) or not previous <= attempt["at"] < plan["expires_at"]
            or not isinstance(attempt["outcome"], str) or attempt["outcome"] not in OUTCOMES - {"confirmed_success"}
        ):
            raise AchievementStateError("achievement_attempt")
        previous = attempt["at"]
    if delivery["next_attempt_at"] < previous:
        raise AchievementStateError("achievement_due")


def achievement_state_json(state: dict, *, profile: str, owner_id: int) -> str:
    """Оплачиваем фактический размер UTF-8 плюс весь будущий рост попыток."""
    validate_achievement_state(state, profile=profile, owner_id=owner_id)
    try:
        payload = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(payload.encode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise AchievementStateError("achievement_encoding") from None
    reserve = sum(recipient_progress_reserve(part["delivery"]) for plan in state["pending"] for part in plan["parts"])
    if size + reserve > ACHIEVEMENTS_MAX_BYTES:
        raise AchievementStateError("achievement_capacity")
    return payload


def parse_achievement_member(raw: bytes, *, profile: str, owner_id: int) -> dict:
    """Одинаковый ограниченный строгий разбор для диска и восстановления."""
    if len(raw) > ACHIEVEMENTS_MAX_BYTES:
        raise AchievementStateError("achievement_capacity")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AchievementStateError("achievement_duplicate_key")
            result[key] = value
        return result

    try:
        state = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise AchievementStateError("achievement_json") from None
    achievement_state_json(state, profile=profile, owner_id=owner_id)
    return state
