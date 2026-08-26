# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Process-local runtime status: fake-clock и безопасная деградация."""

import runtime_status


def test_runtime_snapshot_uses_monotonic_uptime_and_wall_sync_clock(monkeypatch):
    monkeypatch.setattr(runtime_status, "_process_started_monotonic", 100.0)
    monkeypatch.setattr(runtime_status.time, "monotonic", lambda: 466.0)
    monkeypatch.setattr(runtime_status.time, "time", lambda: 1_750_000_000.0)
    monkeypatch.setattr(runtime_status, "_last_full_sync_at", None)
    monkeypatch.setattr(runtime_status, "_polling_active", False)

    runtime_status.mark_full_sync_success()
    runtime_status.set_polling_active(True)
    snapshot = runtime_status.get_runtime_snapshot()

    assert snapshot.uptime_seconds == 366.0
    assert snapshot.last_full_sync_at == 1_750_000_000.0
    assert snapshot.polling_active is True


def test_runtime_snapshot_degrades_corrupt_clock_values(monkeypatch):
    monkeypatch.setattr(runtime_status, "_process_started_monotonic", float("nan"))
    monkeypatch.setattr(runtime_status, "_last_full_sync_at", 10**1000)
    monkeypatch.setattr(runtime_status, "_polling_active", 0)

    snapshot = runtime_status.get_runtime_snapshot()

    assert snapshot.uptime_seconds is None
    assert snapshot.last_full_sync_at is None
    assert snapshot.polling_active is False
