# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Захват достижений и отдельная ограниченная доставка уведомлений владельцу."""

import asyncio
import time
import weakref
from copy import deepcopy

import aiohttp
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from achievement_state import (
    AchievementStateError,
    capture_achievements,
    earned_levels,
)
from config import (
    ERROR_NOTIFY_INTERVAL,
    OWNER_ID,
    SHIKI_BASE_URL,
    SHIKI_USER,
    log,
)
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
from shiki_api import (
    fetch_achievement_labels,
    fetch_achievements,
)
from storage import (
    load_achievement_state,
    restorable_restore_generation,
    restorable_state_transaction,
    save_achievement_state,
)
from telegram_delivery import (
    RetryPolicy,
    send_with_retry,
)

_locks = weakref.WeakKeyDictionary()
_diagnostics = weakref.WeakKeyDictionary()
_profile_conflicts = weakref.WeakKeyDictionary()
_startup_targets = weakref.WeakKeyDictionary()


def register_achievement_startup(message_id: int, text: str) -> None:
    """Стартовый адрес живёт в процессе; новый baseline заморозит его до edit."""
    _startup_targets[asyncio.get_running_loop()] = (
        restorable_restore_generation(), message_id, text.rsplit("\n\n🏆", 1)[0],
    )


class _AchievementChanged(RuntimeError):
    """Восстановление/замена состояния запрещает старую отправку или подтверждение."""


def ensure_achievement_profile(state):
    """Подтверждённая смена числового профиля блокирует старые сообщения владельцу."""
    if _profile_conflicts.get(asyncio.get_running_loop()) == state["state_id"]:
        raise AchievementStateError("achievement_profile")


async def achievement_diagnostic(bot, error: Exception) -> None:
    """Статическая редкая диагностика; не раскрываем payload и исключения."""
    log.warning("Достижения: работа приостановлена (%s).", type(error).__name__)
    loop = asyncio.get_running_loop()
    now = time.monotonic()
    previous = _diagnostics.get(loop)
    if previous is not None and now - previous < ERROR_NOTIFY_INTERVAL:
        return
    _diagnostics[loop] = now
    text = (
        "⚠️ Достижения Shikimori временно не обрабатываются: сохранённое "
        "состояние не удалось проверить или обновить. Данные оставлены на месте. "
        "Проверь логи и резервную копию."
    )
    await send_with_retry(
        lambda: bot.send_message(OWNER_ID, text), policy=RetryPolicy.SAFE_ONLY, max_attempts=1,
    )


async def synchronize_achievements(*, session=None) -> bool:
    """Без отправок Telegram; сбой источника сохраняет очередь в отдельном файле."""
    if session is None:
        async with aiohttp.ClientSession() as own:
            return await synchronize_achievements(session=own)
    async with restorable_state_transaction():
        # Проверка до HTTP не позволяет скрыть повреждённый файл удачным ответом.
        load_achievement_state()
        generation = restorable_restore_generation()
    source = await fetch_achievements(session)
    if source is None:
        return False
    async with restorable_state_transaction():
        if generation != restorable_restore_generation():
            return False
        expected = load_achievement_state()
    if expected is not None and source["user_id"] != expected["user_id"]:
        _profile_conflicts[asyncio.get_running_loop()] = expected["state_id"]
        raise AchievementStateError("achievement_profile")
    labels = {}
    if expected is not None and any(
        level > expected["earned"].get(family, 0)
        for family, level in earned_levels(source["rows"]).items()
    ):
        labels = await fetch_achievement_labels(session)
    target = _startup_targets.get(asyncio.get_running_loop())
    startup = None
    if target is not None:
        target_generation, message_id, text = target
        startup = (message_id if target_generation == generation else 0, text)
    candidate = capture_achievements(
        expected, source["rows"], profile=SHIKI_USER, user_id=source["user_id"],
        owner_id=OWNER_ID, now=time.time(), base_url=SHIKI_BASE_URL, labels=labels, startup=startup,
    )
    async with restorable_state_transaction():
        if generation != restorable_restore_generation() or load_achievement_state() != expected:
            return False
        save_achievement_state(candidate)
        _profile_conflicts.pop(asyncio.get_running_loop(), None)
    return True


def _retry_delay(error):
    """Та же ограниченная политика повторов, что у уведомлений истории."""
    if isinstance(error, TelegramRetryAfter):
        return min(max(0.0, float(error.retry_after)), BACKOFF[-1])
    if isinstance(error, (
        TelegramNetworkError, TelegramServerError, ClientDecodeError, TimeoutError,
        aiohttp.ClientConnectionError, aiohttp.ClientPayloadError,
    )):
        return 0
    return None


def _log_delivery(plan, part, *, status=None, reason=None):
    """Русский результат операции без текста сообщения и приватных адресов."""
    delivery = part["delivery"]
    status = status or delivery["status"]
    reason = reason or delivery["reason"]
    operation = {
        "startup": "дополнение стартового сообщения",
        "baseline": "отправка сводки полученных достижений владельцу",
        "awards": "отправка поздравления владельцу",
    }[plan["kind"]]
    if status == "delivered":
        outcome = "успешно"
    elif reason == "startup_unavailable":
        outcome = "отменено: стартовое сообщение недоступно"
    elif status == "expired":
        outcome = "прекращено: истёк срок доставки" if reason == "lifetime" else "прекращено: исчерпаны попытки"
    elif status == "rejected":
        outcome = "отклонено Telegram; повтора не будет"
    else:
        latest = delivery["attempts"][-1]["outcome"]
        outcome = {
            "uncertain": "результат не подтверждён",
            "not_dispatched": "запрос не отправлен",
            "confirmed_rejection": "запрос отклонён Telegram",
        }[latest]
        delay = max(0, int(delivery["next_attempt_at"] - time.time()))
        outcome += f"; повтор не раньше чем через {delay} с"
    evidence = ""
    if delivery["duplicate_possible"]:
        evidence = " Возможно повторное выполнение."
    elif status != "delivered" and possible_delivery(delivery):
        evidence = " Предыдущая попытка могла выполниться."
    logger = log.info if status == "delivered" else log.warning
    logger(
        "Достижения: %s — %s; достижений: %d; часть %d/%d; попытка %d/%d.%s",
        operation, outcome, len(plan["awards"]), part["index"] + 1, plan["total_parts"],
        len(delivery["attempts"]), MAX_ATTEMPTS, evidence,
    )


def _retire(state, now):
    """Срок/исчерпание бюджета попыток убирает долг, сохраняя earned максимум."""
    result = deepcopy(state)
    retained = []
    for plan in result["pending"]:
        if plan["kind"] == "startup" and plan["startup_message_id"] == 0:
            _log_delivery(plan, plan["parts"][0], status="rejected", reason="startup_unavailable")
            continue
        if now >= plan["expires_at"]:
            for part in plan["parts"]:
                _log_delivery(plan, part, status="expired", reason="lifetime")
            continue
        while plan["parts"] and len(plan["parts"][0]["delivery"]["attempts"]) >= MAX_ATTEMPTS:
            first = plan["parts"].pop(0)
            _log_delivery(plan, first, status="expired", reason="attempt_budget")
        if plan["parts"]:
            retained.append(plan)
    result["pending"] = retained
    return result


async def dispatch_achievements(bot) -> bool:
    """Порция в существующей паузе поллинга; одна доставка на event loop."""
    lock = _locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with lock:
        return await dispatch_owner_achievements(
            bot, load_state=load_achievement_state, save_state=save_achievement_state,
            ensure_profile=ensure_achievement_profile,
        )


async def dispatch_owner_achievements(bot, *, load_state, save_state, ensure_profile=None):
    """Общие попытки и lease для двух независимых владельческих очередей."""
    try:
        return await _dispatch(bot, load_state, save_state, ensure_profile)
    except _AchievementChanged:
        log.info("Достижения: изменение сохранённого состояния остановило прежнюю попытку доставки.")
        return True


async def _dispatch(bot, load_state, save_state, ensure_profile):
    started = time.monotonic()
    async with restorable_state_transaction():
        state = load_state()
        if state is None:
            return False
        if ensure_profile:
            ensure_profile(state)
        generation = restorable_restore_generation()
        identity = state["state_id"]
    for _ in range(MAX_DISPATCHES):
        async with restorable_state_transaction():
            if restorable_restore_generation() != generation:
                raise _AchievementChanged
            state = load_state()
            if state is None or state["state_id"] != identity:
                raise _AchievementChanged
            if ensure_profile:
                ensure_profile(state)
            now = time.time()
            maintained = _retire(state, now)
            if maintained != state:
                save_state(maintained)
                state = maintained
            if not state["pending"]:
                return False
            plan = state["pending"][0]
            part = plan["parts"][0]
            delivery = part["delivery"]
            if (
                delivery["next_attempt_at"] > now
                or DISPATCH_SECONDS - (time.monotonic() - started) < REQUEST_SECONDS
            ):
                return True
            begin_attempt(delivery, max(now, plan["created_at"]))
            save_state(state)
            lease = deepcopy(part)
            plan_id = plan["plan_id"]
            operation = (plan["kind"], plan.get("startup_message_id"))

        async def guard():
            async with restorable_state_transaction():
                current = load_state()
                if current is not None and ensure_profile:
                    ensure_profile(current)
                if (
                    restorable_restore_generation() != generation or current is None
                    or current["state_id"] != identity or not current["pending"]
                    or current["pending"][0]["plan_id"] != plan_id
                    or (current["pending"][0]["kind"], current["pending"][0].get("startup_message_id")) != operation
                    or current["pending"][0]["parts"][0] != lease
                ):
                    raise _AchievementChanged

        async def send():
            if operation[0] == "startup":
                try:
                    return await asyncio.wait_for(bot.edit_message_text(
                        chat_id=OWNER_ID, message_id=operation[1], text=lease["text"],
                        parse_mode="HTML", disable_web_page_preview=True,
                    ), timeout=REQUEST_SECONDS)
                except TelegramBadRequest as error:
                    if "message is not modified" in error.message.casefold():
                        # Telegram подтверждает, что замороженный текст уже установлен.
                        return True
                    raise
            return await asyncio.wait_for(bot.send_message(
                chat_id=OWNER_ID, text=lease["text"], parse_mode="HTML",
                disable_web_page_preview=True,
            ), timeout=REQUEST_SECONDS)

        result = await send_with_retry(
            send, policy=RetryPolicy.AT_LEAST_ONCE, before_attempt=guard, max_attempts=1,
        )
        async with restorable_state_transaction():
            if restorable_restore_generation() != generation:
                raise _AchievementChanged
            current = load_state()
            if current is not None and ensure_profile:
                ensure_profile(current)
            if (
                current is None or current["state_id"] != identity or not current["pending"]
                or current["pending"][0]["plan_id"] != plan_id
                or (current["pending"][0]["kind"], current["pending"][0].get("startup_message_id")) != operation
                or current["pending"][0]["parts"][0] != lease
            ):
                raise _AchievementChanged
            part = current["pending"][0]["parts"][0]
            delivery = part["delivery"]
            outcome = result.attempts[-1].outcome.value
            delay = _retry_delay(result.error)
            if outcome in {"uncertain", "not_dispatched"} and delay is None:
                delay = 0
            complete_attempt(delivery, outcome, max(time.time(), now), retry_delay=delay)
            if time.time() >= plan["expires_at"] and delivery["status"] == "pending":
                finish(delivery, "expired", "lifetime", max(time.time(), now))
            _log_delivery(current["pending"][0], part)
            if delivery["status"] != "pending":
                current["pending"][0]["parts"].pop(0)
                if not current["pending"][0]["parts"]:
                    current["pending"].pop(0)
            save_state(current)
        remaining = DISPATCH_SECONDS - (time.monotonic() - started)
        if remaining > 0:
            await asyncio.sleep(min(0.3, remaining))
    return bool(current["pending"])
