# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Абсолютные seq и точные ID истории после удаления обработанного payload."""

import hashlib
import json


def prefix_seq(journal: dict) -> int:
    """Граница удалённого payload, независимо от времён и source ID."""
    if "source_base" not in journal:
        return 0
    base = journal["source_base"]
    if not isinstance(base, dict) or type(base.get("through_seq")) is not int:
        raise ValueError("source_prefix")
    return base["through_seq"]


def event_count(journal: dict) -> int:
    return prefix_seq(journal) + len(journal["events"])


def event_at_seq(journal: dict, seq: int) -> dict:
    """Удалённая запись не может стать lease или новым событием."""
    if type(seq) is not int:
        raise ValueError("source_seq")
    index = seq - prefix_seq(journal) - 1
    if not 0 <= index < len(journal["events"]):
        raise ValueError("source_seq")
    return journal["events"][index]


def source_suffix(journal: dict, baseline: int, applied: int) -> list[dict]:
    offset = prefix_seq(journal)
    return journal["events"][max(0, baseline - offset):max(0, applied - offset)]


def known_history_ids(journal: dict) -> set[int]:
    return (
        set(journal["baseline_ids"])
        | {item[0] for item in journal.get("source_base", {}).get("ids", [])}
        | {event["history_id"] for event in journal["events"]}
    )


def content_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def semantic_hash(event: dict) -> str:
    """Диагностический отпечаток первой семантики, без seq/времени наблюдения.

    Числа нормализуются согласно прежнему равенству Python (1 == 1.0 == True).
    Отпечаток не заменяет точный ID и не является доказательством подлинности.
    """
    def canonical(value):
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        if isinstance(value, bool) or isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    return content_hash(canonical({
        key: value for key, value in event.items() if key not in {"seq", "observed_at"}
    }))


def same_history_authority(current: dict | None, expected: dict) -> bool:
    """Очистка доказанного префикса не отменяет lease неизменного suffix."""
    if current is None or prefix_seq(current) < prefix_seq(expected):
        return False
    excluded = {"events", "outbox", "source_base"}
    if {k: v for k, v in current.items() if k not in excluded} != {
        k: v for k, v in expected.items() if k not in excluded
    }:
        return False
    removed = prefix_seq(current) - prefix_seq(expected)
    if not removed:
        return (
            current["events"] == expected["events"]
            and current.get("source_base") == expected.get("source_base")
        )
    if current["events"] != expected["events"][removed:]:
        return False
    ids = list(expected.get("source_base", {}).get("ids", []))
    ids.extend([ev["history_id"], semantic_hash(ev)] for ev in expected["events"][:removed])
    return current["source_base"]["ids"] == ids


def rebase_source_candidate(candidate: dict, current: dict) -> dict:
    """Сохранить новый admission suffix, не воскресив очищенный старый prefix."""
    if prefix_seq(current) <= prefix_seq(candidate):
        return candidate
    return {
        **candidate, "source_base": current["source_base"],
        "events": source_suffix(candidate, prefix_seq(current), event_count(candidate)),
    }
