# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистая схема обязательств доставки: без I/O и повторной классификации."""

import json
import math
import re
from copy import deepcopy

MAX_ATTEMPTS = 6
LIFETIME = 72 * 60 * 60
BACKOFF = (60, 300, 1800, 7200, 21600)
MAX_DISPATCHES = 20
DISPATCH_SECONDS = 20
REQUEST_SECONDS = 10
OUTCOMES = {"confirmed_success", "confirmed_rejection", "not_dispatched", "uncertain"}
TERMINAL = {"delivered", "cancelled", "expired", "rejected"}


class OutboxStateError(ValueError):
    """Сохранённое обязательство нельзя безопасно использовать."""


def parse_subscriber_payload(raw: bytes, max_bytes: int) -> dict:
    """Ограниченный runtime/import parser, без дубликатов JSON keys."""
    if len(raw) > max_bytes:
        raise OutboxStateError("subscribers_capacity")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise OutboxStateError("subscribers_duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise OutboxStateError("subscribers_invalid") from None


def _time(value):
    return type(value) in {int, float} and 0 <= value <= 10**12 and math.isfinite(value)


def _identity(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _chat(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"-?[1-9][0-9]*", value) is not None
        and -(2**63) <= int(value) <= 2**63 - 1
    )


def validate_memberships(payload: object, subscribers: dict[int, str]) -> dict[int, str] | None:
    """Новые metadata строгие; отсутствие означает совместимую legacy-подписку."""
    if payload is None:
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "tokens"}
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or not isinstance(payload["tokens"], dict)
        or set(payload["tokens"]) != {str(cid) for cid in subscribers}
        or any(not _chat(cid) or not _identity(token) for cid, token in payload["tokens"].items())
    ):
        raise OutboxStateError("notification_memberships")
    return {int(cid): token for cid, token in payload["tokens"].items()}


def notification_event(event: dict) -> bool:
    return event["relevant"] and event["event_type"] not in {"ignored", "score_removed"}


def migrate_outbox(journal: dict, applied_seq: int) -> dict:
    """Старый processed_seq — только тихая граница, никогда не delivered."""
    if journal["version"] == 3:
        return journal
    result = deepcopy(journal)
    baseline = journal["processed_seq"]
    result.update(
        version=3,
        catchup=journal.get("catchup"),
        outbox={
            "version": 1,
            "baseline_seq": baseline,
            "enqueued_seq": baseline,
            "legacy_uncertain_seq": baseline + 1 if applied_seq > baseline else None,
            "records": [],
        },
    )
    return result


def enqueue(
    journal: dict, event: dict, text: str | None, memberships: dict[int, str], now: float
) -> dict:
    """Состав получателей и payload замораживаются с processing checkpoint."""
    result = deepcopy(journal)
    box = result["outbox"]
    seq = event["seq"]
    if seq != box["enqueued_seq"] + 1 or seq != journal["processed_seq"] + 1:
        raise OutboxStateError("enqueue_order")
    notify = notification_event(event)
    inherited = box["legacy_uncertain_seq"] == seq
    recipients = (
        {
            str(cid): {
                "membership": token,
                "status": "pending",
                "attempts": [],
                "prior_possible": inherited,
                "next_attempt_at": now,
                "terminal_at": None,
                "reason": None,
                "duplicate_possible": False,
            }
            for cid, token in sorted(memberships.items())
        }
        if notify
        else {}
    )
    box["records"].append(
        {
            "seq": seq,
            "history_id": event["history_id"],
            "created_at": now,
            "expires_at": now + LIFETIME,
            "payload": {"text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
            if notify
            else None,
            "recipients": recipients,
        }
    )
    box["enqueued_seq"] = seq
    result["processed_seq"] = seq
    return result


def possible_delivery(recipient: dict) -> bool:
    return recipient["prior_possible"] or any(
        attempt["outcome"] in {"uncertain", "confirmed_success"}
        for attempt in recipient["attempts"]
    )


def finish(recipient: dict, status: str, reason: str, now: float) -> None:
    """Терминальный отказ сохраняет всю предыдущую возможность доставки."""
    recipient.update(status=status, reason=reason, terminal_at=now)


def begin_attempt(recipient: dict, now: float) -> None:
    """Публикуется до dispatch; прерывание расходует попытку консервативно."""
    recipient["attempts"].append({"at": now, "outcome": "uncertain"})
    index = min(len(recipient["attempts"]) - 1, len(BACKOFF) - 1)
    recipient["next_attempt_at"] = now + BACKOFF[index]


def complete_attempt(
    recipient: dict, outcome: str, now: float, *, retry_delay: float | None = None
) -> None:
    """Заменить только свидетельство своей попытки; прежние не стираются."""
    recipient["attempts"][-1]["outcome"] = outcome
    if outcome == "confirmed_success":
        possible = int(recipient["prior_possible"]) + sum(
            attempt["outcome"] in {"uncertain", "confirmed_success"}
            for attempt in recipient["attempts"]
        )
        recipient["duplicate_possible"] = possible > 1
        finish(recipient, "delivered", "confirmed_success", now)
    elif retry_delay is None:
        finish(recipient, "rejected", "permanent_rejection", now)
    elif len(recipient["attempts"]) >= MAX_ATTEMPTS:
        finish(recipient, "expired", "attempt_budget", now)
    else:
        recipient["next_attempt_at"] = now + min(
            max(retry_delay, BACKOFF[len(recipient["attempts"]) - 1]), BACKOFF[-1]
        )


def progress_reserve(journal: dict) -> int:
    """Резерв на все оставшиеся попытки и terminal поля, без увеличения лимита."""
    box = journal.get("outbox")
    if box is None:
        return 0
    return sum(
        512 + (MAX_ATTEMPTS - len(recipient["attempts"])) * 96
        for record in box["records"]
        for recipient in record["recipients"].values()
        if recipient["status"] == "pending"
    )


def validate_outbox(journal: dict) -> None:
    """Одна матрица runtime/import: точные поля, последовательность и budgets."""
    box = journal["outbox"]
    if (
        not isinstance(box, dict)
        or set(box)
        != {"version", "baseline_seq", "enqueued_seq", "legacy_uncertain_seq", "records"}
        or type(box["version"]) is not int
        or box["version"] != 1
        or type(box["baseline_seq"]) is not int
        or not 0 <= box["baseline_seq"] <= journal["processed_seq"]
        or type(box["enqueued_seq"]) is not int
        or box["enqueued_seq"] != journal["processed_seq"]
        or (
            box["legacy_uncertain_seq"] is not None
            and (
                type(box["legacy_uncertain_seq"]) is not int
                or box["legacy_uncertain_seq"] != box["baseline_seq"] + 1
                or box["legacy_uncertain_seq"] > len(journal["events"])
            )
        )
        or not isinstance(box["records"], list)
        or len(box["records"]) != box["enqueued_seq"] - box["baseline_seq"]
    ):
        raise OutboxStateError("outbox_structure")
    for seq, record in enumerate(box["records"], box["baseline_seq"] + 1):
        event = journal["events"][seq - 1]
        if (
            not isinstance(record, dict)
            or set(record)
            != {"seq", "history_id", "created_at", "expires_at", "payload", "recipients"}
            or type(record["seq"]) is not int
            or record["seq"] != seq
            or type(record["history_id"]) is not int
            or record["history_id"] != event["history_id"]
            or not _time(record["created_at"])
            or not _time(record["expires_at"])
            or record["expires_at"] != record["created_at"] + LIFETIME
            or not isinstance(record["recipients"], dict)
        ):
            raise OutboxStateError("outbox_record")
        payload = record["payload"]
        if notification_event(event):
            if (
                not isinstance(payload, dict)
                or set(payload) != {"text", "parse_mode", "disable_web_page_preview"}
                or not isinstance(payload["text"], str)
                or not payload["text"].strip()
                or payload["parse_mode"] != "HTML"
                or payload["disable_web_page_preview"] is not False
            ):
                raise OutboxStateError("outbox_payload")
            payload["text"].encode("utf-8")
        elif payload is not None or record["recipients"]:
            raise OutboxStateError("outbox_silent")
        for cid, recipient in record["recipients"].items():
            if (
                not _chat(cid)
                or not isinstance(recipient, dict)
                or set(recipient)
                != {
                    "membership",
                    "status",
                    "attempts",
                    "prior_possible",
                    "next_attempt_at",
                    "terminal_at",
                    "reason",
                    "duplicate_possible",
                }
                or not _identity(recipient["membership"])
                or not isinstance(recipient["status"], str)
                or recipient["status"] not in TERMINAL | {"pending"}
                or type(recipient["prior_possible"]) is not bool
                or recipient["prior_possible"] != (box["legacy_uncertain_seq"] == seq)
                or type(recipient["duplicate_possible"]) is not bool
                or not _time(recipient["next_attempt_at"])
                or recipient["next_attempt_at"] < record["created_at"]
                or not isinstance(recipient["attempts"], list)
                or len(recipient["attempts"]) > MAX_ATTEMPTS
            ):
                raise OutboxStateError("outbox_recipient")
            previous = record["created_at"]
            for attempt in recipient["attempts"]:
                if (
                    not isinstance(attempt, dict)
                    or set(attempt) != {"at", "outcome"}
                    or not _time(attempt["at"])
                    or not previous <= attempt["at"] < record["expires_at"]
                    or not isinstance(attempt["outcome"], str)
                    or attempt["outcome"] not in OUTCOMES
                ):
                    raise OutboxStateError("outbox_attempt")
                previous = attempt["at"]
            status = recipient["status"]
            if recipient["next_attempt_at"] < previous:
                raise OutboxStateError("outbox_due_time")
            if status == "rejected" and (
                not recipient["attempts"]
                or recipient["attempts"][-1]["outcome"] != "confirmed_rejection"
            ):
                raise OutboxStateError("outbox_rejection")
            reasons = {
                "delivered": {"confirmed_success"},
                "cancelled": {"ineligible"},
                "expired": {"lifetime", "attempt_budget"},
                "rejected": {"permanent_rejection", "forbidden"},
            }
            if status == "pending":
                if recipient["terminal_at"] is not None or recipient["reason"] is not None:
                    raise OutboxStateError("outbox_pending")
            elif (
                not _time(recipient["terminal_at"])
                or recipient["terminal_at"] < previous
                or not isinstance(recipient["reason"], str)
                or recipient["reason"] not in reasons[status]
            ):
                raise OutboxStateError("outbox_terminal")
            if status == "expired" and (
                recipient["reason"] == "attempt_budget"
                and len(recipient["attempts"]) != MAX_ATTEMPTS
                or recipient["reason"] == "lifetime"
                and recipient["terminal_at"] < record["expires_at"]
            ):
                raise OutboxStateError("outbox_expiry")
            success = bool(
                recipient["attempts"]
                and recipient["attempts"][-1]["outcome"] == "confirmed_success"
            )
            if success != (status == "delivered") or any(
                attempt["outcome"] == "confirmed_success" for attempt in recipient["attempts"][:-1]
            ):
                raise OutboxStateError("outbox_success")
            duplicate = (
                status == "delivered"
                and (
                    int(recipient["prior_possible"])
                    + sum(
                        a["outcome"] in {"uncertain", "confirmed_success"}
                        for a in recipient["attempts"]
                    )
                )
                > 1
            )
            if recipient["duplicate_possible"] != duplicate:
                raise OutboxStateError("outbox_duplicate")
