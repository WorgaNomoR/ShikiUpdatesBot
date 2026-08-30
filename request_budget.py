# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Процессный бюджет скользящего окна без сетевых зависимостей."""

import time
from collections import (
    Counter,
    deque,
)
from collections.abc import (
    Callable,
    Hashable,
)
from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetSnapshot:
    """Снимок занятости окна для диагностики и ожидания.

    ``actor_counts`` не включает системные резервирования без actor.
    """

    used: int
    capacity: int
    retry_after: float
    last_actor: Hashable | None
    actor_counts: tuple[tuple[Hashable, int], ...]


@dataclass(frozen=True)
class _Reservation:
    created_at: float
    actor: Hashable | None


class RollingBudget:
    """Считать успешные резервирования в скользящем временном окне."""

    def __init__(
        self,
        limit: int,
        period: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit <= 0 or period <= 0:
            raise ValueError("limit и period должны быть положительными")
        self.limit = limit
        self.period = period
        self._clock = clock
        self._events: deque[_Reservation] = deque()

    def _validate_reserve(self, reserve: int) -> None:
        if reserve < 0 or reserve >= self.limit:
            raise ValueError("reserve должен быть от 0 до limit - 1")

    def _prune(self, now: float) -> None:
        cutoff = now - self.period
        while self._events and self._events[0].created_at <= cutoff:
            self._events.popleft()

    def try_acquire(
        self,
        *,
        reserve: int = 0,
        actor: Hashable | None = None,
    ) -> bool:
        """Зарезервировать единицу, оставив ``reserve`` мест другим потокам."""
        self._validate_reserve(reserve)
        now = self._clock()
        self._prune(now)
        if len(self._events) >= self.limit - reserve:
            return False
        self._events.append(_Reservation(now, actor))
        return True

    def snapshot(self, *, reserve: int = 0) -> BudgetSnapshot:
        """Вернуть цельный снимок окна и ожидание до нового места."""
        self._validate_reserve(reserve)
        now = self._clock()
        self._prune(now)
        used = len(self._events)
        capacity = self.limit - reserve
        retry_after = 0.0
        if used >= capacity:
            expiry_index = used - capacity
            retry_after = max(
                0.0,
                self._events[expiry_index].created_at + self.period - now,
            )
        counts = Counter(
            event.actor for event in self._events if event.actor is not None
        )
        return BudgetSnapshot(
            used=used,
            capacity=capacity,
            retry_after=retry_after,
            last_actor=self._events[-1].actor if self._events else None,
            actor_counts=tuple(counts.items()),
        )

    @property
    def used(self) -> int:
        """Вернуть число актуальных резервирований после ленивой очистки."""
        return self.snapshot().used
