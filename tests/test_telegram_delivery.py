# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контракты общего ограниченного повтора Telegram-доставки."""

import asyncio
import socket
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import aiohttp
import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage
from aiohttp import web

from telegram_delivery import (
    RetryPolicy,
    SendOutcome,
    TelegramDeliverySession,
    _request_started,
    classify_send_error,
    is_blocked_error,
    send_with_retry,
)

_METHOD = SendMessage(chat_id=1, text="test")


@pytest.mark.asyncio
async def test_send_with_retry_returns_first_success_without_sleep():
    operation = AsyncMock(return_value="sent")
    sleep = AsyncMock()

    result = await send_with_retry(operation, policy=RetryPolicy.AT_LEAST_ONCE, sleep=sleep)
    assert result.delivered and result.value == "sent"
    assert result.outcome is SendOutcome.CONFIRMED_SUCCESS
    assert not result.uncertain and not result.duplicate_possible

    operation.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_with_retry_retries_transient_client_failure():
    operation = AsyncMock(
        side_effect=[aiohttp.ClientOSError(104, "Connection reset by peer"), "sent"]
    )
    sleep = AsyncMock()

    result = await send_with_retry(operation, policy=RetryPolicy.AT_LEAST_ONCE, sleep=sleep)
    assert result.delivered and result.value == "sent"
    assert result.uncertain and result.duplicate_possible
    assert [attempt.outcome for attempt in result.attempts] == [
        SendOutcome.UNCERTAIN, SendOutcome.CONFIRMED_SUCCESS,
    ]

    assert operation.await_count == 2
    sleep.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
async def test_send_with_retry_exhausts_two_retries():
    error = TelegramServerError(method=_METHOD, message="server unavailable")
    operation = AsyncMock(side_effect=error)
    sleep = AsyncMock()

    result = await send_with_retry(operation, policy=RetryPolicy.AT_LEAST_ONCE, sleep=sleep)
    assert result.outcome is SendOutcome.UNCERTAIN
    assert result.error is error
    assert len(result.attempts) == 3

    assert operation.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]


@pytest.mark.asyncio
async def test_send_with_retry_honours_retry_after_delay():
    error = TelegramRetryAfter(
        method=_METHOD,
        message="flood control",
        retry_after=7,
    )
    operation = AsyncMock(side_effect=[error, "sent"])
    sleep = AsyncMock()

    result = await send_with_retry(operation, policy=RetryPolicy.AT_LEAST_ONCE, sleep=sleep)
    assert result.delivered and result.value == "sent"
    assert result.attempts[0].outcome is SendOutcome.CONFIRMED_REJECTION
    assert not result.uncertain and not result.duplicate_possible

    sleep.assert_awaited_once_with(7.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TelegramForbiddenError(method=_METHOD, message="bot was blocked"),
        TelegramEntityTooLarge(method=_METHOD, message="file is too large"),
        RuntimeError("permanent failure"),
    ],
)
async def test_send_with_retry_does_not_retry_permanent_errors(error):
    operation = AsyncMock(side_effect=error)
    sleep = AsyncMock()

    result = await send_with_retry(operation, policy=RetryPolicy.AT_LEAST_ONCE, sleep=sleep)
    assert not result.delivered and result.error is error

    operation.assert_awaited_once()
    sleep.assert_not_awaited()


def test_is_blocked_error_accepts_aiogram_forbidden_error():
    error = TelegramForbiddenError(method=_METHOD, message="forbidden")

    assert is_blocked_error(error) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("later", [
    TelegramBadRequest(method=_METHOD, message="rejected"),
    TelegramNotFound(method=_METHOD, message="Not Found"),
    TelegramForbiddenError(method=_METHOD, message="forbidden"),
])
async def test_later_rejection_never_erases_uncertain_attempt(later):
    lost = TelegramNetworkError(method=_METHOD, message="lost response")
    operation = AsyncMock(side_effect=[lost, later])
    result = await send_with_retry(operation, policy=RetryPolicy.AT_LEAST_ONCE, sleep=AsyncMock())
    assert result.outcome is SendOutcome.UNCERTAIN
    assert result.error is later and result.attempts[0].error is lost
    assert result.attempts[1].outcome is SendOutcome.CONFIRMED_REJECTION
    assert not result.delivered
    assert operation.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    TimeoutError(),
    aiohttp.SocketTimeoutError("read"),
    aiohttp.ClientPayloadError("truncated"),
    ClientDecodeError("decode", ValueError(), "bad JSON"),
])
async def test_uncertain_exhaustion_preserves_all_attempts(error):
    operation = AsyncMock(side_effect=error)
    result = await send_with_retry(operation, policy=RetryPolicy.AT_LEAST_ONCE, sleep=AsyncMock())
    assert result.outcome is SendOutcome.UNCERTAIN
    assert not result.delivered and result.duplicate_possible
    assert len(result.attempts) == operation.await_count == 3
    assert all(attempt.error is error for attempt in result.attempts)


@pytest.mark.asyncio
async def test_safe_policy_stops_uncertain_with_unobserved_connection_failure():
    operation = AsyncMock(side_effect=TimeoutError())
    sleeper = AsyncMock()
    result = await send_with_retry(operation, policy=RetryPolicy.SAFE_ONLY, sleep=sleeper)
    assert result.uncertain and not result.delivered
    operation.assert_awaited_once()
    sleeper.assert_not_awaited()
    operation = AsyncMock(side_effect=[aiohttp.ConnectionTimeoutError(), "sent"])
    result = await send_with_retry(operation, policy=RetryPolicy.SAFE_ONLY, sleep=sleeper)
    assert not result.delivered and result.uncertain
    sleeper.assert_not_awaited()


def test_text_and_implicit_exception_context_are_not_dispatch_evidence():
    error = TelegramNetworkError(method=_METHOD, message="ClientConnectorError: chat not found")
    error.__context__ = aiohttp.ConnectionTimeoutError()
    assert classify_send_error(error) is SendOutcome.UNCERTAIN
    assert not is_blocked_error(error)


@pytest.mark.asyncio
async def test_prepare_failure_preserves_previous_uncertainty():
    operation = AsyncMock(side_effect=TimeoutError())
    failure = RuntimeError("changed generation")
    prepare = AsyncMock(side_effect=[None, failure])
    result = await send_with_retry(
        operation, policy=RetryPolicy.AT_LEAST_ONCE,
        before_attempt=prepare, sleep=AsyncMock(),
    )
    assert result.uncertain and result.error is failure
    assert [a.outcome for a in result.attempts] == [SendOutcome.UNCERTAIN, SendOutcome.NOT_DISPATCHED]
    operation.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["send", "sleep", "prepare"])
async def test_cancellation_never_becomes_a_result_or_another_attempt(phase):
    cancelled = asyncio.CancelledError()
    operation = AsyncMock(side_effect=cancelled if phase == "send" else TimeoutError())
    prepare = AsyncMock(side_effect=cancelled if phase == "prepare" else None)
    sleeper = AsyncMock(side_effect=cancelled if phase == "sleep" else None)
    with pytest.raises(asyncio.CancelledError):
        await send_with_retry(
            operation, policy=RetryPolicy.AT_LEAST_ONCE,
            before_attempt=prepare, sleep=sleeper,
        )
    assert operation.await_count == (0 if phase == "prepare" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,expected", [
    ("connect", SendOutcome.NOT_DISPATCHED),
    ("connection_timeout", SendOutcome.NOT_DISPATCHED),
    ("total_timeout", SendOutcome.UNCERTAIN),
    ("read", SendOutcome.UNCERTAIN),
    ("decode", SendOutcome.UNCERTAIN),
    ("rejection", SendOutcome.CONFIRMED_REJECTION),
])
async def test_installed_aiogram_transport_retains_typed_evidence(monkeypatch, phase, expected):
    session = AiohttpSession()
    bot = Bot("123456:TEST", session=session)
    response = MagicMock(status=400 if phase == "rejection" else 200)
    response.text = AsyncMock(return_value='{"ok":false,"description":"rejected"}')
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=False)
    if phase == "connect":
        request.__aenter__.side_effect = aiohttp.ClientConnectorError(
            MagicMock(), OSError(111, "refused"),
        )
    elif phase == "connection_timeout":
        request.__aenter__.side_effect = aiohttp.ConnectionTimeoutError()
    elif phase == "total_timeout":
        request.__aenter__.side_effect = TimeoutError()
    elif phase == "read":
        # Запрос принят сервером, но тело подтверждения потеряно.
        response.text.side_effect = aiohttp.ClientPayloadError("lost body")
    elif phase == "decode":
        response.text.return_value = "broken JSON"
    client = MagicMock()
    client.post.return_value = request
    monkeypatch.setattr(session, "create_session", AsyncMock(return_value=client))
    enter = request.__aenter__

    async def observed_enter(*_args):
        await _request_started(None, None, None)
        return await enter()

    request.__aenter__ = observed_enter
    result = await send_with_retry(
        lambda: session.make_request(bot, _METHOD),
        policy=RetryPolicy.SAFE_ONLY, sleep=AsyncMock(),
    )
    assert result.outcome is expected
    assert not result.delivered
    if phase in {"connect", "connection_timeout"}:
        assert len(result.attempts) == 3
        response.text.assert_not_awaited()
    else:
        assert len(result.attempts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", [False, True])
async def test_real_aiohttp_connection_failure_after_redirect_is_not_predispatch(monkeypatch, redirect):
    # Свободный loopback-порт: никаких запросов к Telegram или внешней сети.
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        closed_port = unused.getsockname()[1]
    target = f"http://127.0.0.1:{closed_port}"
    real_connect = aiohttp.TCPConnector._create_connection

    async def connect(connector, request, traces, timeout):
        if request.url.port == closed_port:
            raise aiohttp.ClientConnectorError(request.connection_key, OSError(111, "refused"))
        return await real_connect(connector, request, traces, timeout)

    monkeypatch.setattr(aiohttp.TCPConnector, "_create_connection", connect)
    runner = None
    accepted = []
    if redirect:
        async def handler(request):
            accepted.append(await request.read())
            raise web.HTTPFound(location=target)

        app = web.Application()
        app.router.add_post("/bot{token}/{method}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, "127.0.0.1", 0)
        await server.start()
        target = f"http://127.0.0.1:{server._server.sockets[0].getsockname()[1]}"
    session = TelegramDeliverySession(api=TelegramAPIServer.from_base(target), timeout=10)
    bot = Bot("123456:TEST", session=session)
    try:
        result = await send_with_retry(
            lambda: bot.send_message(chat_id=1, text="test"),
            policy=RetryPolicy.SAFE_ONLY, sleep=AsyncMock(),
        )
        if redirect:
            assert result.outcome is SendOutcome.UNCERTAIN
            assert len(result.attempts) == len(accepted) == 1
        else:
            assert result.outcome is SendOutcome.NOT_DISPATCHED
            assert len(result.attempts) == 3
        assert not result.delivered
    finally:
        await session.close()
        if runner is not None:
            await runner.cleanup()


@pytest.mark.parametrize("error,blocked", [
    (TelegramForbiddenError(method=_METHOD, message="bot was blocked by the user"), True),
    (TelegramForbiddenError(method=_METHOD, message="user is deactivated"), True),
    (TelegramBadRequest(method=_METHOD, message="chat not found"), True),
    (TelegramNetworkError(method=_METHOD, message="chat not found"), False),
    (RuntimeError("bot was blocked"), False),
    (TelegramServerError(method=_METHOD, message="Internal Server Error"), False),
    (TelegramServerError(method=_METHOD, message="chat not found"), False),
    (RuntimeError(""), False),
])
def test_blocked_detection_requires_a_confirmed_api_refusal(error, blocked):
    assert is_blocked_error(error) is blocked
