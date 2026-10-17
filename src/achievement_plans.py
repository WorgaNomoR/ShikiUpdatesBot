# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Общая строгая схема замороженных сообщений достижений владельцу."""

import json
import math
import re

from catchup_digest import html_length
from notification_outbox import (
    LIFETIME,
    MAX_ATTEMPTS,
    OUTCOMES,
    recipient_progress_reserve,
)
from report_model import TELEGRAM_TEXT_LIMIT

ACHIEVEMENTS_MAX_BYTES = 8 * 1024 * 1024


class AchievementStateError(ValueError):
    """Состояние сохраняем, небезопасную работу останавливаем."""


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _time(value):
    return type(value) in (int, float) and 0 <= value <= 10**12 and math.isfinite(value)


def _identity(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{32}", value) is not None


def validate_pending_plans(pending, earned, *, version=1):
    """Общая схема замороженных владельческих сообщений, без каталога достижений."""
    if not isinstance(pending, list):
        raise AchievementStateError("achievement_pending")
    identities = set()
    baseline_count = 0
    for plan in pending:
        startup = isinstance(plan, dict) and plan.get("kind") == "startup"
        fields = {"plan_id", "kind", "awards", "created_at", "expires_at", "total_parts", "parts"}
        if startup:
            fields.add("startup_message_id")
        if (
            not isinstance(plan, dict) or set(plan) != fields
            or not _identity(plan["plan_id"]) or plan["plan_id"] in identities
            or plan["kind"] not in ("baseline", "awards", "startup")
            or (startup and (version != 2 or not _integer(plan["startup_message_id"])
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


def encode_owner_state(state, *, max_bytes=ACHIEVEMENTS_MAX_BYTES):
    """Компактный UTF-8 с резервом на все разрешённые попытки и исходы."""
    try:
        payload = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(payload.encode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise AchievementStateError("achievement_encoding") from None
    reserve = sum(recipient_progress_reserve(part["delivery"]) for plan in state["pending"] for part in plan["parts"])
    if size + reserve > max_bytes:
        raise AchievementStateError("achievement_capacity")
    return payload


def decode_owner_state(raw, *, max_bytes=ACHIEVEMENTS_MAX_BYTES):
    """Ограниченный JSON; повторные ключи запрещены и на диске, и в архиве."""
    if len(raw) > max_bytes:
        raise AchievementStateError("achievement_capacity")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AchievementStateError("achievement_duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise AchievementStateError("achievement_json") from None
