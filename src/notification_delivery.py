# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Независимый ограниченный consumer долговечных уведомлений журнала."""

import asyncio
import time
import weakref
from copy import deepcopy

import aiohttp
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from config import log
from event_journal_schema import validate_recovery_set
from notification_outbox import (
    BACKOFF,
    DISPATCH_SECONDS,
    MAX_ATTEMPTS,
    MAX_DISPATCHES,
    REQUEST_SECONDS,
    begin_attempt,
    complete_attempt,
    finish,
    possible_delivery,
)
from storage import (
    load_blocked_users,
    load_event_journal,
    load_stats_current,
    load_subscriber_state,
    notification_memberships,
    restorable_restore_generation,
    restorable_state_transaction,
    save_event_journal,
    save_subscriber_state,
)
from telegram_delivery import (
    RetryPolicy,
    is_blocked_error,
    send_with_retry,
)

_locks = weakref.WeakKeyDictionary()


def _recipient(journal, seq, cid):
    box = journal["outbox"]
    return box["records"][seq - box["baseline_seq"] - 1]["recipients"][cid]


def _retry_delay(error):
    """Постоянные ошибки не повторять; server delay ограничен общим backoff."""
    if isinstance(error, TelegramRetryAfter):
        return min(max(0.0, float(error.retry_after)), BACKOFF[-1])
    if isinstance(
        error,
        (
            TelegramNetworkError,
            TelegramServerError,
            ClientDecodeError,
            TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
        ),
    ):
        return 0
    return None


def _maintenance(journal, memberships, blocked, now):
    """Cancellation/expiry — явная публикация, также для неготового head."""
    changed = False
    for record in journal["outbox"]["records"]:
        for cid, recipient in record["recipients"].items():
            if recipient["status"] != "pending":
                continue
            terminal_at = max(
                now, record["created_at"],
                recipient["attempts"][-1]["at"] if recipient["attempts"] else 0,
            )
            if int(cid) in blocked or memberships.get(int(cid)) != recipient["membership"]:
                finish(recipient, "cancelled", "ineligible", terminal_at)
            elif now >= record["expires_at"]:
                finish(recipient, "expired", "lifetime", terminal_at)
            elif len(recipient["attempts"]) >= MAX_ATTEMPTS:
                finish(recipient, "expired", "attempt_budget", terminal_at)
            else:
                continue
            changed = True
            log.warning(
                "Уведомление seq=%d: %s; possible_delivery=%s.",
                record["seq"],
                recipient["status"],
                possible_delivery(recipient),
            )
    return changed


def _next_due(journal, now):
    """Только первый pending для чата; старейший due обеспечивает fairness."""
    heads = {}
    for record in journal["outbox"]["records"]:
        for cid, recipient in record["recipients"].items():
            if recipient["status"] == "pending":
                heads.setdefault(cid, (record, recipient))
    ready = [
        (r["next_attempt_at"], record["seq"], cid, record)
        for cid, (record, r) in heads.items()
        if r["next_attempt_at"] <= now
    ]
    if not ready:
        return None
    _, _, cid, record = min(ready, key=lambda item: item[:3])
    return record, cid


async def dispatch_notifications(bot):
    """Одна очередь consumer на loop; restore не ждёт Telegram или pacing."""
    lock = _locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with lock:
        try:
            return await _dispatch(bot)
        except _DeliveryChanged:
            log.info("Уведомления: restore остановил старую попытку.")


class _DeliveryChanged(RuntimeError):
    """Старая попытка не может менять новую authority."""


async def _dispatch(bot):
    started = time.monotonic()
    async with restorable_state_transaction():
        journal = load_event_journal()
        if journal is None or journal["version"] != 3:
            return
        validate_recovery_set(journal, load_stats_current(strict=True))
        generation = restorable_restore_generation()
        identity = journal["journal_id"]
    for _ in range(MAX_DISPATCHES):
        async with restorable_state_transaction():
            if restorable_restore_generation() != generation:
                raise _DeliveryChanged
            journal = load_event_journal()
            if journal is None or journal["journal_id"] != identity:
                raise _DeliveryChanged
            journal = deepcopy(journal)
            now = time.time()
            memberships = notification_memberships()
            blocked = load_blocked_users()
            if _maintenance(journal, memberships, blocked, now):
                save_event_journal(journal)
            due = _next_due(journal, now)
            remaining = DISPATCH_SECONDS - (time.monotonic() - started)
            if due is None or remaining < REQUEST_SECONDS:
                return
            record, cid = due
            seq = record["seq"]
            recipient = record["recipients"][cid]
            begin_attempt(recipient, now)
            save_event_journal(journal)
            lease = deepcopy(recipient)
            payload = deepcopy(record["payload"])

        async def guard():
            async with restorable_state_transaction():
                if restorable_restore_generation() != generation:
                    raise _DeliveryChanged
                if (
                    notification_memberships().get(int(cid)) != lease["membership"]
                    or int(cid) in load_blocked_users()
                ):
                    raise _DeliveryChanged

        async def send():
            # Deadline относится только к Telegram, а не к ожиданию state lock.
            return await asyncio.wait_for(
                bot.send_message(chat_id=int(cid), **payload), timeout=REQUEST_SECONDS
            )

        result = await send_with_retry(
            send, policy=RetryPolicy.AT_LEAST_ONCE, before_attempt=guard, max_attempts=1
        )
        async with restorable_state_transaction():
            if restorable_restore_generation() != generation:
                raise _DeliveryChanged
            journal = load_event_journal()
            if (
                journal is None
                or journal["journal_id"] != identity
                or _recipient(journal, seq, cid) != lease
            ):
                raise _DeliveryChanged
            journal = deepcopy(journal)
            recipient = _recipient(journal, seq, cid)
            outcome = result.attempts[-1].outcome.value
            delay = _retry_delay(result.error)
            if outcome in {"uncertain", "not_dispatched"} and delay is None:
                delay = 0
            complete_attempt(recipient, outcome, max(time.time(), now), retry_delay=delay)
            if isinstance(result.error, _DeliveryChanged):
                finish(recipient, "cancelled", "ineligible", max(time.time(), now))
            if result.error is not None and is_blocked_error(result.error):
                state = load_subscriber_state(strict_subscribers=True)
                if (state.notification_memberships or {}).get(int(cid)) == lease["membership"]:
                    state.subscribers.pop(int(cid), None)
                    save_subscriber_state(state)
                recipient["reason"] = "forbidden"
            save_event_journal(journal)
            if recipient["status"] != "pending":
                log.info(
                    "Уведомление seq=%d: %s; possible_delivery=%s; duplicate_possible=%s.",
                    seq,
                    recipient["status"],
                    possible_delivery(recipient),
                    recipient["duplicate_possible"],
                )
        remaining = DISPATCH_SECONDS - (time.monotonic() - started)
        if remaining > 0:
            await asyncio.sleep(min(0.3, remaining))
