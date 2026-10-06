# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Одна матрица физического recovery-формата и совместимости старого журнала."""

from copy import deepcopy

import pytest

from event_journal_schema import (
    JOURNAL_MAX_BYTES,
    EventJournalStateError,
    journal_json,
)
from notification_outbox import (
    enqueue,
    migrate_outbox,
)
from notification_progress_schema import (
    compact_json,
    history_budget_size,
    history_document,
    parse_recovery_journal,
    progress_budget_size,
    progress_document,
)


def _pair(factory):
    journal = migrate_outbox(factory(), 0)
    journal = enqueue(journal, journal["events"][0], "frozen", {10: "b" * 32}, 1000)
    return journal, history_document(journal, "c" * 32), progress_document(journal, "c" * 32)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_split_roundtrip_preserves_every_outbox_version(journal_factory, version):
    journal, history, progress = _pair(journal_factory)
    journal["outbox"]["version"] = version
    if version == 3:
        journal["outbox"]["completed_seq"] = 0
    payload = compact_json(progress)
    assert parse_recovery_journal(compact_json(history).encode(), payload.encode()) == journal
    assert history_budget_size(history) + progress_budget_size(progress, payload) == len(journal_json(journal).encode())


@pytest.mark.parametrize("version", [1, 2, 3])
def test_legacy_import_is_read_only_and_never_invents_recipients(journal_factory, version):
    journal = journal_factory(processed=1)
    if version == 2:
        journal.update(version=2, catchup=None)
    elif version == 3:
        journal = migrate_outbox(journal, 1)
    before = deepcopy(journal)
    assert parse_recovery_journal(journal_json(journal).encode(), None) == before
    assert journal == before


@pytest.mark.parametrize("damage", [
    "missing", "orphan", "lineage", "journal", "profile", "version", "extra",
    "cursor_bool", "cursor_gap", "history_id", "duplicate", "encoding", "depth", "oversized",
    "history_extra", "history_cursor", "history_lineage", "history_version",
])
def test_invalid_split_recovery_rejects_entire_pair(journal_factory, damage):
    journal, history, progress = _pair(journal_factory)
    if damage == "lineage":
        progress["progress_id"] = "d" * 32
    elif damage == "journal":
        progress["journal_id"] = "d" * 32
    elif damage == "profile":
        progress["profile"] = "other"
    elif damage == "version":
        progress["version"] = True
    elif damage == "extra":
        progress["extra"] = None
    elif damage == "cursor_bool":
        progress["processed_seq"] = True
    elif damage == "cursor_gap":
        progress["processed_seq"] = 2
    elif damage == "history_id":
        progress["outbox"]["records"][0]["history_id"] += 1
    elif damage == "history_extra":
        history["extra"] = None
    elif damage == "history_cursor":
        history["processed_seq"] = 1
    elif damage == "history_lineage":
        history["progress_id"] = "invalid"
    elif damage == "history_version":
        history["version"] = 4.0
    history_raw = compact_json(history).encode()
    progress_raw = compact_json(progress).encode()
    if damage == "missing":
        progress_raw = None
    elif damage == "orphan":
        history_raw = journal_json(journal).encode()
    elif damage == "duplicate":
        progress_raw = progress_raw.replace(b'"version":1', b'"version":1,"version":1')
    elif damage == "encoding":
        progress_raw = b"\xff"
    elif damage == "depth":
        progress_raw = b"[" * 2000 + b"0" + b"]" * 2000
    elif damage == "oversized":
        progress_raw = b" " * (8 * 1024 * 1024 + 1)
    with pytest.raises(EventJournalStateError):
        parse_recovery_journal(history_raw, progress_raw)


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("split", [False, True])
def test_legacy_and_activated_capacity_share_exact_inclusive_bound(
    outbox_capacity_factory, version, split,
):
    journal = outbox_capacity_factory()
    journal["outbox"]["version"] = version
    if version == 3:
        journal["outbox"]["completed_seq"] = 0
    record = journal["outbox"]["records"][0]
    # Граница рассчитана независимо от helper: 441 байт на fresh recipient.
    actual = len(journal_json(journal).encode())
    record["payload"]["text"] += "x" * (JOURNAL_MAX_BYTES - actual - 441 * 7000)

    def parse():
        if split:
            return parse_recovery_journal(
                compact_json(history_document(journal, "c" * 32)).encode(),
                compact_json(progress_document(journal, "c" * 32)).encode(),
            )
        return parse_recovery_journal(journal_json(journal).encode(), None)

    assert parse() == journal
    record["payload"]["text"] += "x"
    with pytest.raises(EventJournalStateError):
        parse()
