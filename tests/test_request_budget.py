# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Скользящие бюджеты общих HTTP-попыток и inline-страниц."""

from request_budget import RollingBudget


def test_rolling_budget_expires_old_events_at_window_boundary():
    clock = {"now": 10.0}
    budget = RollingBudget(2, 60.0, clock=lambda: clock["now"])

    assert budget.try_acquire()
    assert budget.try_acquire()
    assert not budget.try_acquire()

    clock["now"] = 70.0
    assert budget.try_acquire()
    assert budget.used == 1


def test_reserve_keeps_capacity_for_other_traffic():
    budget = RollingBudget(5, 60.0, clock=lambda: 1.0)

    assert budget.try_acquire(reserve=2)
    assert budget.try_acquire(reserve=2)
    assert budget.try_acquire(reserve=2)
    assert not budget.try_acquire(reserve=2)
    assert budget.try_acquire()
    assert budget.try_acquire()
    assert not budget.try_acquire()


def test_snapshot_reports_retry_actor_counts_and_reserve_boundary():
    clock = {"now": 10.0}
    budget = RollingBudget(5, 60.0, clock=lambda: clock["now"])

    assert budget.try_acquire(actor=101)
    clock["now"] = 20.0
    assert budget.try_acquire(actor=101)
    clock["now"] = 30.0
    assert budget.try_acquire(actor=202)

    snapshot = budget.snapshot(reserve=2)

    assert snapshot.used == 3
    assert snapshot.capacity == 3
    assert snapshot.retry_after == 40.0
    assert snapshot.last_actor == 202
    assert dict(snapshot.actor_counts) == {101: 2, 202: 1}


def test_snapshot_waits_for_enough_events_when_general_traffic_uses_reserve():
    clock = {"now": 0.0}
    budget = RollingBudget(5, 60.0, clock=lambda: clock["now"])
    for moment in range(5):
        clock["now"] = float(moment)
        assert budget.try_acquire(actor=moment)

    clock["now"] = 10.0
    snapshot = budget.snapshot(reserve=2)

    assert snapshot.used == 5
    assert snapshot.capacity == 3
    assert snapshot.retry_after == 52.0
