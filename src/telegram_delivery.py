# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Ограниченные повторы отдельных операций доставки через Telegram."""

import asyncio
from collections.abc import (
    Awaitable,
    Callable,
)
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import (
    Generic,
    TypeVar,
)

import aiohttp
from aiogram import __version__ as aiogram_version
from aiogram.client.session.aiohttp import (
    SERVER_SOFTWARE,
    AiohttpSession,
)
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

_ResultT = TypeVar("_ResultT")

_MAX_RETRIES = 2
_TRANSIENT_BACKOFF = (0.5, 1.0)
# Старые допустимые aiohttp не выделяют connection timeout отдельным типом.
_CONNECT_FAILURES = (aiohttp.ClientConnectorError,)
if hasattr(aiohttp, "ConnectionTimeoutError"):
    _CONNECT_FAILURES += (aiohttp.ConnectionTimeoutError,)


@dataclass
class _RequestEvidence:
    """Redirect означает, что до нового соединения запрос уже отправлялся."""

    requests: int = 0
    redirected: bool = False


_request_evidence: ContextVar[_RequestEvidence | None] = ContextVar(
    "telegram_request_evidence", default=None,
)


async def _request_started(_session, _context, _params) -> None:
    evidence = _request_evidence.get()
    if evidence is not None:
        evidence.requests += 1


async def _request_redirected(_session, _context, _params) -> None:
    evidence = _request_evidence.get()
    if evidence is not None:
        evidence.redirected = True


class TelegramDeliverySession(AiohttpSession):
    """Штатный aiogram transport с наблюдением redirect, без копии dispatch."""

    async def create_session(self) -> aiohttp.ClientSession:
        # Оба поля заранее создаёт AiohttpSession.__init__.
        if self._should_reset_connector:  # pylint: disable=access-member-before-definition
            await self.close()
        if self._session is None or self._session.closed:  # pylint: disable=access-member-before-definition
            trace = aiohttp.TraceConfig()
            trace.on_request_start.append(_request_started)
            trace.on_request_redirect.append(_request_redirected)
            self._session = aiohttp.ClientSession(
                connector=self._connector_type(**self._connector_init),
                headers={"User-Agent": f"{SERVER_SOFTWARE} aiogram/{aiogram_version}"},
                trace_configs=[trace],
            )
            self._should_reset_connector = False
        return self._session


class SendOutcome(str, Enum):
    """Доказанный исход попытки, без вывода стадии по тексту ошибки."""

    CONFIRMED_SUCCESS = "confirmed_success"
    CONFIRMED_REJECTION = "confirmed_rejection"
    NOT_DISPATCHED = "not_dispatched"
    UNCERTAIN = "uncertain"


class RetryPolicy(str, Enum):
    """Caller явно выбирает допустимость повторной возможной доставки."""

    SAFE_ONLY = "safe_only"
    AT_LEAST_ONCE = "at_least_once"


@dataclass(frozen=True)
class SendAttempt:
    """Свидетельство одной попытки; последующий отказ его не заменяет."""

    outcome: SendOutcome
    error: Exception | None = None


@dataclass(frozen=True)
class SendResult(Generic[_ResultT]):
    """Итог с историей, пригодной для будущего учёта получателей в outbox."""

    attempts: tuple[SendAttempt, ...]
    value: _ResultT | None = None

    @property
    def delivered(self) -> bool:
        return self.attempts[-1].outcome is SendOutcome.CONFIRMED_SUCCESS

    @property
    def uncertain(self) -> bool:
        return any(attempt.outcome is SendOutcome.UNCERTAIN for attempt in self.attempts)

    @property
    def outcome(self) -> SendOutcome:
        if self.delivered:
            return SendOutcome.CONFIRMED_SUCCESS
        if self.uncertain:
            return SendOutcome.UNCERTAIN
        return self.attempts[-1].outcome

    @property
    def duplicate_possible(self) -> bool:
        return sum(
            attempt.outcome in {SendOutcome.CONFIRMED_SUCCESS, SendOutcome.UNCERTAIN}
            for attempt in self.attempts
        ) > 1

    @property
    def error(self) -> Exception | None:
        return self.attempts[-1].error


def classify_send_error(exc: Exception) -> SendOutcome:
    """Классифицировать только доступные типизированные свидетельства."""
    # AiohttpSession сохраняет прямую причину при обёртке ClientError/timeout.
    cause = exc.__cause__ if isinstance(exc, TelegramNetworkError) else exc
    evidence = _request_evidence.get()
    if (
        isinstance(cause, _CONNECT_FAILURES)
        and evidence is not None and evidence.requests == 1 and not evidence.redirected
    ):
        return SendOutcome.NOT_DISPATCHED
    if isinstance(exc, (TelegramNetworkError, TelegramServerError)):
        return SendOutcome.UNCERTAIN
    if isinstance(exc, TelegramAPIError):
        return SendOutcome.CONFIRMED_REJECTION
    # Неизвестная ошибка внутри dispatch не доказывает отсутствие доставки.
    return SendOutcome.UNCERTAIN


async def _sleep(delay: float) -> None:
    """Тестовый шов для ожидания между попытками."""
    await asyncio.sleep(delay)


def is_blocked_error(exc: Exception) -> bool:
    """Означает ли ошибка, что подписчик больше недоступен для бота."""
    if isinstance(exc, TelegramForbiddenError):
        return True
    if not isinstance(exc, TelegramAPIError) or isinstance(exc, (TelegramNetworkError, TelegramServerError)):
        return False
    error = str(exc).lower()
    return (
        "bot was blocked" in error
        or "user is deactivated" in error
        or "chat not found" in error
    )


def _retry_delay(
    exc: Exception, outcome: SendOutcome, policy: RetryPolicy, retry_index: int,
) -> float | None:
    """Задержка повтора для временной ошибки либо None для постоянной."""
    if isinstance(exc, TelegramRetryAfter):
        return max(0.0, float(exc.retry_after))
    if is_blocked_error(exc) or isinstance(exc, TelegramEntityTooLarge):
        return None
    if outcome is SendOutcome.UNCERTAIN and policy is RetryPolicy.SAFE_ONLY:
        return None
    if isinstance(
        exc,
        (
            TelegramNetworkError,
            TelegramServerError,
            ClientDecodeError,
            TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
        ),
    ):
        return _TRANSIENT_BACKOFF[retry_index]
    return None


async def send_with_retry(
    operation: Callable[[], Awaitable[_ResultT]],
    *,
    policy: RetryPolicy,
    before_attempt: Callable[[], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> SendResult[_ResultT]:
    """Ограниченно повторить свежую операцию и сохранить исходы всех попыток.

    Подготовка и guards отделены от dispatch. Отмена распространяется сразу:
    caller сохраняет незавершённое обязательство, а не получает ложный успех.
    """
    sleeper = sleep or _sleep
    retries = 0
    attempts = []

    while True:
        if before_attempt is not None:
            try:
                await before_attempt()
            except Exception as exc:
                attempts.append(SendAttempt(SendOutcome.NOT_DISPATCHED, exc))
                return SendResult(tuple(attempts))
        try:
            token = _request_evidence.set(_RequestEvidence())
            try:
                value = await operation()
            except Exception as exc:
                outcome = classify_send_error(exc)
                raise
            finally:
                _request_evidence.reset(token)
        except Exception as exc:
            # outcome присвоен во внутреннем except перед повторным raise.
            attempts.append(SendAttempt(outcome, exc))  # pylint: disable=used-before-assignment
            if retries >= _MAX_RETRIES:
                return SendResult(tuple(attempts))
            delay = _retry_delay(exc, outcome, policy, retries)
            if delay is None:
                return SendResult(tuple(attempts))
            retries += 1
            try:
                await sleeper(delay)
            except Exception as wait_error:
                attempts.append(SendAttempt(SendOutcome.NOT_DISPATCHED, wait_error))
                return SendResult(tuple(attempts))
        else:
            attempts.append(SendAttempt(SendOutcome.CONFIRMED_SUCCESS))
            return SendResult(tuple(attempts), value)
