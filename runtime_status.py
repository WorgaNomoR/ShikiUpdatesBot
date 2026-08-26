# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Минимальное process-local состояние жизненного цикла приложения."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Безопасный снимок состояния для пользовательского отображения."""

    uptime_seconds: float | None
    last_full_sync_at: float | None
    polling_active: bool


_process_started_monotonic = time.monotonic()
_last_full_sync_at: float | None = None
_polling_active = False


def _nonnegative_clock(value: object) -> float | None:
    """Нормализовать число часов, не падая на чрезмерно больших int."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value < 0:
            return None
        return float(value)
    except (OverflowError, TypeError, ValueError):
        return None


def mark_full_sync_success() -> None:
    """Запомнить wall-clock время успешной полной синхронизации."""
    global _last_full_sync_at
    _last_full_sync_at = time.time()


def set_polling_active(active: bool) -> None:
    """Отметить фактическое состояние фонового notification polling."""
    global _polling_active
    _polling_active = bool(active)


def get_runtime_snapshot() -> RuntimeSnapshot:
    """Вернуть безопасный снимок без исключений из-за повреждённых часов."""
    try:
        uptime = _nonnegative_clock(time.monotonic() - _process_started_monotonic)
    except (OverflowError, TypeError, ValueError):
        uptime = None

    last_sync = _nonnegative_clock(_last_full_sync_at)

    return RuntimeSnapshot(
        uptime_seconds=uptime,
        last_full_sync_at=last_sync,
        polling_active=bool(_polling_active),
    )
