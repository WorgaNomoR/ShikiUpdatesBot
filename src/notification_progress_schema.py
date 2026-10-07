# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Физическое разделение истории и прогресса с общей runtime/import проверкой."""

import json
import re

from event_journal_schema import (
    JOURNAL_MAX_BYTES,
    EventJournalStateError,
    _unique_object,
    parse_event_journal,
    validate_event_journal,
)
from notification_outbox import (
    OutboxStateError,
    progress_reserve,
    validate_outbox,
)
from source_history import event_count

PROGRESS_FILE_NAME = "notification_progress.json"
_PROGRESS_FIELDS = {"version", "progress_id", "journal_id", "profile", "processed_seq", "outbox"}


def compact_json(value: dict) -> str:
    """Точные байты штатного формата; никакого форматирования при публикации."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _parse_member(raw: bytes) -> dict:
    if len(raw) > JOURNAL_MAX_BYTES:
        raise EventJournalStateError("journal_size")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise EventJournalStateError("journal_invalid") from None


def history_document(journal: dict, progress_id: str) -> dict:
    """Activation member не содержит изменяемых checkpoint и outbox."""
    return {
        **{key: value for key, value in journal.items() if key not in {"processed_seq", "outbox"}},
        "version": 5 if "source_base" in journal else 4,
        "progress_id": progress_id,
    }


def progress_document(journal: dict, progress_id: str) -> dict:
    return {
        "version": 1, "progress_id": progress_id,
        "journal_id": journal["journal_id"], "profile": journal["profile"],
        "processed_seq": journal["processed_seq"], "outbox": journal["outbox"],
    }


def history_budget_size(history: dict) -> int:
    """Размер неизменной части прежнего logical v3 без двойного envelope."""
    return len(compact_json({
        **{key: value for key, value in history.items() if key != "progress_id"},
        "version": 3,
    }).encode("utf-8"))


def progress_budget_size(progress: dict, payload: str) -> int:
    """Добавленные logical поля; размер совпадает с прежним journal_json."""
    header = {key: value for key, value in progress.items() if key not in {"processed_seq", "outbox"}}
    return len(payload.encode("utf-8")) - len(compact_json(header).encode("utf-8"))


def parse_history_member(raw: bytes, *, profile: str | None = None) -> dict:
    """Историю проверяем полностью на чтении новой физической ревизии."""
    value = _parse_member(raw)
    if value.get("version") not in (4, 5):
        return parse_event_journal(raw, profile=profile)
    identity = value.get("progress_id")
    if (
        type(value["version"]) is not int
        or not isinstance(identity, str)
        or re.fullmatch(r"[0-9a-f]{32}", identity) is None
        or "processed_seq" in value or "outbox" in value
        or ("source_base" in value) != (value["version"] == 5)
    ):
        raise EventJournalStateError("history_structure")
    try:
        validate_event_journal({
            **{key: field for key, field in value.items() if key != "progress_id"},
            "version": 2, "processed_seq": event_count(value),
        }, profile=profile)
    except (ValueError, TypeError, KeyError, RecursionError):
        raise EventJournalStateError("history_invalid") from None
    return value


def parse_progress_member(raw: bytes) -> dict:
    """Структура progress обязательна; связь и recipients проверяет join."""
    value = _parse_member(raw)
    if (
        set(value) != _PROGRESS_FIELDS
        or type(value["version"]) is not int or value["version"] != 1
        or type(value["processed_seq"]) is not int or value["processed_seq"] < 0
    ):
        raise EventJournalStateError("progress_structure")
    return value


def join_history_progress(history: dict, progress: dict) -> dict:
    """Свежий progress поверх проверенной истории, без её повторной обработки."""
    if (
        set(progress) != _PROGRESS_FIELDS
        or type(progress["version"]) is not int or progress["version"] != 1
        or any(progress[key] != history[key] for key in ("progress_id", "journal_id", "profile"))
        or type(progress["processed_seq"]) is not int
        or not 0 <= progress["processed_seq"] <= event_count(history)
        or (history["catchup"] is not None and progress["processed_seq"] != event_count(history))
    ):
        raise EventJournalStateError("progress_mismatch")
    journal = {
        **{key: value for key, value in history.items() if key != "progress_id"},
        "version": 3, "processed_seq": progress["processed_seq"], "outbox": progress["outbox"],
    }
    try:
        validate_outbox(journal)
    except (OutboxStateError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise EventJournalStateError("progress_invalid") from None
    return journal


def parse_recovery_journal(history_raw: bytes, progress_raw: bytes | None, *, profile: str | None = None) -> dict:
    """Runtime и импорт разделяют границы, lineage и прежний общий бюджет."""
    history = parse_history_member(history_raw, profile=profile)
    if history["version"] not in {4, 5}:
        if progress_raw is not None:
            raise EventJournalStateError("progress_orphan")
        return history
    if progress_raw is None:
        raise EventJournalStateError("progress_missing")
    progress = parse_progress_member(progress_raw)
    journal = join_history_progress(history, progress)
    size = history_budget_size(history) + progress_budget_size(progress, compact_json(progress))
    if size + progress_reserve(journal) > JOURNAL_MAX_BYTES:
        raise EventJournalStateError("journal_capacity")
    return journal
