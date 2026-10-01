# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Неизменяемый формат журнала и единая строгая проверка recovery-набора."""

import json
import re
from datetime import (
    datetime,
    timezone,
)

from event_time_stats import (
    EventTimeStateError,
    validate_event_time,
)

JOURNAL_MAX_BYTES = 8 * 1024 * 1024
JOURNAL_WARN_BYTES = 6 * 1024 * 1024
JOURNAL_CHECKPOINT_RESERVE = 4096
PROJECTION_KEY = "event_projection"
EVENT_TYPES = frozenset({
    "planned", "watching", "rewatching", "on_hold", "dropped", "completed",
    "score_set", "score_changed", "score_removed", "ignored", "unknown",
})


class EventJournalStateError(ValueError):
    """Журнал или его связь с кварталом нельзя безопасно использовать."""


def source_time(value: object) -> tuple[str | None, str]:
    """Не выводить время события из наблюдения или локальной timezone."""
    if value is None or value == "":
        return None, "missing"
    if not isinstance(value, str):
        return None, "invalid"
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None or moment.utcoffset() is None:
            return None, "naive"
        return moment.astimezone(timezone.utc).isoformat(), "aware"
    except (ValueError, OverflowError):
        return None, "invalid"


def _integer(value: object, minimum: int | None = None) -> bool:
    return type(value) is int and (minimum is None or value >= minimum)


def _text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
        return True
    except UnicodeError:
        return False


def _identity(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def validate_projection(projection: object) -> None:
    """Проверить локальную форму checkpoint без чтения журнала."""
    if (
        not isinstance(projection, dict)
        or set(projection) != {"journal_id", "baseline_seq", "applied_seq"}
        or not _identity(projection["journal_id"])
        or not _integer(projection["baseline_seq"], 0)
        or not _integer(projection["applied_seq"], projection["baseline_seq"])
    ):
        raise EventJournalStateError("projection_structure")


def validate_event_journal(journal: object, *, profile: str | None = None) -> dict:
    """Проверить v1/v2 без повторной классификации опубликованных событий."""
    if (
        not isinstance(journal, dict)
        or set(journal) != {
            "version", "journal_id", "profile", "normalization_version",
            "baseline_initialized", "baseline_ids", "events", "processed_seq",
        } | ({"catchup"} if journal.get("version") == 2 else set())
        or type(journal["version"]) is not int or journal["version"] not in {1, 2}
        or type(journal["normalization_version"]) is not int
        or journal["normalization_version"] != 1
        or not _identity(journal["journal_id"])
        or not _text(journal["profile"]) or not journal["profile"].strip()
        or (profile is not None and journal["profile"].casefold() != profile.casefold())
        or type(journal["baseline_initialized"]) is not bool
        or not isinstance(journal["baseline_ids"], list)
        or not isinstance(journal["events"], list)
    ):
        raise EventJournalStateError("journal_structure")
    ids = journal["baseline_ids"]
    if any(not _integer(value) for value in ids) or len(set(ids)) != len(ids):
        raise EventJournalStateError("baseline_ids")
    known = set(ids)
    for seq, event in enumerate(journal["events"], 1):
        if (
            not isinstance(event, dict)
            or set(event) != {
                "seq", "normalization_version", "history_id", "created_at",
                "event_at", "observed_at", "time_quality", "event_type", "media",
                "target_id", "kind", "relevant", "score", "score_change",
                "description", "title",
            }
            or not _integer(event["seq"], 1) or event["seq"] != seq
            or type(event["normalization_version"]) is not int
            or event["normalization_version"] != 1
            or not _integer(event["history_id"]) or event["history_id"] in known
            or not isinstance(event["event_type"], str) or event["event_type"] not in EVENT_TYPES
            or not isinstance(event["media"], str) or event["media"] not in {"anime", "manga"}
            or not _text(event["target_id"])
            or not _text(event["kind"])
            or type(event["relevant"]) is not bool
            or (event["score"] is not None and not _integer(event["score"]))
            or not _text(event["description"])
            or not isinstance(event["title"], dict)
            or set(event["title"]) != {"name", "russian", "url"}
            or any(not _text(value) for value in event["title"].values())
        ):
            raise EventJournalStateError("event_structure")
        change = event["score_change"]
        if change is not None and (
            not isinstance(change, list) or len(change) != 2
            or any(not _integer(value) for value in change)
        ):
            raise EventJournalStateError("event_score_change")
        if (event["event_at"], event["time_quality"]) != source_time(event["created_at"]):
            raise EventJournalStateError("event_time")
        observed, quality = source_time(event["observed_at"])
        if quality != "aware" or observed != event["observed_at"]:
            raise EventJournalStateError("observation_time")
        known.add(event["history_id"])
    if (
        not _integer(journal["processed_seq"], 0)
        or journal["processed_seq"] > len(journal["events"])
        or (not journal["baseline_initialized"] and (ids or journal["events"]))
    ):
        raise EventJournalStateError("journal_cursor")
    acquisition = journal.get("catchup")
    if acquisition is not None:
        validate_acquisition(acquisition, journal, known)
    # Проверка кодируемости исходного created_at и запрет NaN/Infinity.
    try:
        json.dumps(journal, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise EventJournalStateError("journal_encoding") from None
    return journal


def validate_acquisition(state: object, journal: dict, known: set[int]) -> None:
    """Staged payload имеет отдельную последовательность и не двигает authority."""
    if (
        not isinstance(state, dict)
        or set(state) != {"phase", "page", "frontier", "head_ids", "staged", "spanning"}
        or not isinstance(state["phase"], str) or state["phase"] not in {"tail", "head"}
        or not _integer(state["page"], 1)
        or type(state["spanning"]) is not bool
        or not isinstance(state["staged"], list)
        or not journal["baseline_initialized"]
        or journal["processed_seq"] != len(journal["events"])
    ):
        raise EventJournalStateError("acquisition_structure")
    # Повторно используем ту же матрицу нормализованных событий без рекурсии v2.
    validate_event_journal({
        **{key: value for key, value in journal.items() if key != "catchup"},
        "version": 1, "baseline_ids": [], "events": state["staged"], "processed_seq": 0,
    })
    staged_ids = {event["history_id"] for event in state["staged"]}
    if staged_ids & known:
        raise EventJournalStateError("acquisition_already_admitted")
    retained = known | staged_ids
    for field in ("frontier", "head_ids"):
        values = state[field]
        if (
            not isinstance(values, list) or len(values) > 51
            or any(not _integer(value) or value not in retained for value in values)
            or len(set(values)) != len(values)
        ):
            raise EventJournalStateError("acquisition_ids")
    if (
        (state["page"] > 1 and not state["frontier"])
        or (state["phase"] == "head" and not state["spanning"])
    ):
        raise EventJournalStateError("acquisition_cursor")


def validate_recovery_set(journal: dict, cur: dict, *, full_recovery: bool = True) -> None:
    """Курсоры и структура всегда строгие; source-сверка нужна на границе drain."""
    projection = cur.get(PROJECTION_KEY)
    validate_projection(projection)
    completed = journal["processed_seq"]
    if (
        projection["journal_id"] != journal["journal_id"]
        or projection["baseline_seq"] > completed
        or not completed <= projection["applied_seq"] <= min(completed + 1, len(journal["events"]))
    ):
        raise EventJournalStateError("recovery_mismatch")
    try:
        validate_event_time(cur, journal if full_recovery else None)
    except EventTimeStateError:
        raise EventJournalStateError("event_time_recovery") from None


def journal_json(journal: dict) -> str:
    """Компактная UTF-8 публикация с точным размером без перевода строк."""
    validate_event_journal(journal)
    return json.dumps(journal, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EventJournalStateError("duplicate_key")
        result[key] = value
    return result


def parse_event_journal(raw: bytes, *, profile: str | None = None) -> dict:
    """Один bounded parser для диска и импортируемого архива."""
    if len(raw) > JOURNAL_MAX_BYTES:
        raise EventJournalStateError("journal_size")
    try:
        journal = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        return validate_event_journal(journal, profile=profile)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise EventJournalStateError("journal_invalid") from None
