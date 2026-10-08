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
    progress_reserve,
)
from notification_progress_schema import (
    compact_json,
    history_budget_size,
    history_document,
    parse_recovery_journal,
    progress_budget_size,
    progress_document,
)
from source_history import content_hash


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


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("prefix", [4095, 4096, 4097, 4224])
def test_source_index_old_and_new_formats_roundtrip_without_mutation(source_index_factory, prefix, version):
    journal, _ = source_index_factory(prefix=prefix, version=version)
    original = deepcopy(journal)
    history = history_document(journal, "c" * 32)
    assert history["version"] == (5 if version == 1 else 6)
    assert parse_recovery_journal(
        compact_json(history).encode(), compact_json(progress_document(journal, "c" * 32)).encode(),
    ) == original
    assert journal == original
    assert len(journal_json(journal).encode()) == (
        history_budget_size(history)
        + progress_budget_size(progress_document(journal, "c" * 32), compact_json(progress_document(journal, "c" * 32)))
    )


@pytest.mark.parametrize("damage", [
    "old_digest", "old_invalid", "window_null", "window_gap", "window_invalid", "bool_id", "row_null",
    "row_extra", "missing_id", "duplicate_id", "checksum", "base_unknown", "base_bool", "physical_unknown",
    "v5_new_base", "v6_old_base", "v1_null",
])
def test_source_index_window_runtime_and_import_reject_same_corruption(
    backup_env, source_index_factory, damage,
):
    import storage

    journal, _ = source_index_factory(prefix=4100, version=2)
    history = history_document(journal, "c" * 32)
    base = history["source_base"]
    ids = base["ids"]
    if damage == "old_digest":
        ids[3][1] = "a" * 64
    elif damage == "old_invalid":
        ids[0][1] = False
    elif damage == "window_null":
        ids[4][1] = None
    elif damage == "window_gap":
        ids[100][1] = None
    elif damage == "window_invalid":
        ids[-1][1] = "A" * 64
    elif damage == "bool_id":
        ids[0][0] = True
    elif damage == "row_null":
        ids[0] = None
    elif damage == "row_extra":
        ids[0].append(0)
    elif damage == "missing_id":
        ids.pop(0)
    elif damage == "duplicate_id":
        ids[-1][0] = ids[-2][0]
    elif damage == "base_unknown":
        base["version"] = 3
    elif damage == "base_bool":
        base["version"] = True
    elif damage == "physical_unknown":
        history["version"] = 7
    elif damage == "v5_new_base":
        history["version"] = 5
    elif damage == "v6_old_base":
        base["version"] = 1
    elif damage == "v1_null":
        base["version"] = 1
        history["version"] = 5
    base["checksum"] = content_hash({k: v for k, v in base.items() if k != "checksum"})
    if damage == "checksum":
        base["checksum"] = "f" * 64
    raw = compact_json(history).encode()
    progress = compact_json(progress_document(journal, "c" * 32)).encode()
    storage.EVENT_JOURNAL_FILE.write_bytes(raw)
    storage.notification_progress_file().write_bytes(progress)
    with pytest.raises(EventJournalStateError):
        parse_recovery_journal(raw, progress)
    with pytest.raises(EventJournalStateError):
        storage.load_event_journal()
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == raw
    assert storage.notification_progress_file().read_bytes() == progress


def test_v6_logical_budget_keeps_exact_inclusive_pending_reserve(
    backup_env, source_index_factory, monkeypatch,
):
    import storage

    journal, _ = source_index_factory(version=2)
    history = compact_json(history_document(journal, "c" * 32)).encode()
    progress = compact_json(progress_document(journal, "c" * 32)).encode()
    storage.EVENT_JOURNAL_FILE.write_bytes(history)
    storage.notification_progress_file().write_bytes(progress)
    boundary = len(journal_json(journal).encode()) + progress_reserve(journal)
    for limit in [boundary, boundary - 1]:
        monkeypatch.setattr("notification_progress_schema.JOURNAL_MAX_BYTES", limit)
        monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", limit)
        if limit == boundary:
            assert parse_recovery_journal(history, progress) == journal
            assert storage.load_event_journal() == journal
        else:
            with pytest.raises(EventJournalStateError, match="journal_capacity"):
                parse_recovery_journal(history, progress)
            with pytest.raises(EventJournalStateError, match="journal_capacity"):
                storage.load_event_journal()
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == history
    assert storage.notification_progress_file().read_bytes() == progress


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("offset", [0, -1])
def test_digest_runtime_import_share_inclusive_future_reserve(digest_factory, monkeypatch, ready, split, offset):
    from event_journal_schema import (
        EventJournalStateError,
        journal_json,
        parse_event_journal,
    )
    from notification_outbox import progress_reserve

    journal = digest_factory(ready=ready)
    budget = len(journal_json(journal).encode()) + progress_reserve(journal)
    monkeypatch.setattr("event_journal_schema.JOURNAL_MAX_BYTES", budget + offset)
    monkeypatch.setattr("notification_progress_schema.JOURNAL_MAX_BYTES", budget + offset)
    if split:
        history = compact_json(history_document(journal, "c" * 32)).encode()
        progress = compact_json(progress_document(journal, "c" * 32)).encode()
        def parse():
            return parse_recovery_journal(history, progress)
    else:
        raw = journal_json(journal).encode()
        def parse():
            return parse_event_journal(raw)
    if offset:
        with pytest.raises(EventJournalStateError):
            parse()
    else:
        assert parse() == journal
