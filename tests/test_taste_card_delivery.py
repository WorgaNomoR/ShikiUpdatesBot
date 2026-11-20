# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Один локальный срез, свежие uploads, отмена и сохранение неопределённости."""

import asyncio
import threading
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto
from PIL import ImageFont

import taste_card_delivery as delivery
from storage import (
    STATS_ALL_MISSING,
    STATS_ALL_VALID,
    StatsAllSnapshot,
)
from telegram_delivery import (
    RetryPolicy,
    SendAttempt,
    SendOutcome,
    SendResult,
)


@pytest.fixture
def request_env(monkeypatch):
    snapshot = StatsAllSnapshot({
        "anime": {"titles": {"1": {"kind": "tv", "status": "completed", "score": 8}}},
        "manga": {"titles": {}},
    }, STATS_ALL_VALID)
    loader = MagicMock(return_value=snapshot)
    monkeypatch.setattr("taste_card_delivery.load_stats_all_snapshot", loader)
    bot = MagicMock(send_photo=AsyncMock(return_value="photo"), send_message=AsyncMock())
    return bot, loader


@pytest.mark.asyncio
async def test_one_snapshot_and_frozen_payload_with_fresh_uploads(request_env, monkeypatch):
    bot, loader = request_env
    uploads = []
    captions = []

    async def send(factory, *, policy):
        assert policy is RetryPolicy.AT_LEAST_ONCE
        await factory()
        uploads.append(bot.send_photo.await_args.kwargs["photo"])
        captions.append(bot.send_photo.await_args.kwargs["caption"])
        await factory()
        uploads.append(bot.send_photo.await_args.kwargs["photo"])
        captions.append(bot.send_photo.await_args.kwargs["caption"])
        return SendResult((SendAttempt(SendOutcome.CONFIRMED_SUCCESS),), value="photo")

    monkeypatch.setattr("taste_card_delivery.send_with_retry", send)
    result = await delivery.deliver_taste_card(bot, 42, "anime")
    assert result.state == "sent"
    loader.assert_called_once_with(use_cache=False)
    assert uploads[0] is not uploads[1] and uploads[0].data == uploads[1].data
    assert uploads[0].data.startswith(b"\xff\xd8")
    assert captions[0] == captions[1]
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertainty_then_rejection_cannot_trigger_alternative_format(request_env, monkeypatch):
    bot, _ = request_env
    calls = []
    rejection = TelegramBadRequest(SendPhoto(chat_id=42, photo="x"), "bad photo")
    uncertain = SendResult((
        SendAttempt(SendOutcome.UNCERTAIN, TimeoutError()),
        SendAttempt(SendOutcome.CONFIRMED_REJECTION, rejection),
    ))

    async def send(factory, *, policy):
        calls.append(policy)
        if policy is RetryPolicy.AT_LEAST_ONCE:
            await factory()
            return uncertain
        await factory()
        return SendResult((SendAttempt(SendOutcome.CONFIRMED_SUCCESS),), value="notice")

    monkeypatch.setattr("taste_card_delivery.send_with_retry", send)
    result = await delivery.deliver_taste_card(bot, 42, "anime")
    assert result.state == "uncertain" and result.send_result is uncertain
    assert calls == [RetryPolicy.AT_LEAST_ONCE, RetryPolicy.SAFE_ONLY]
    assert "могла прийти" in bot.send_message.await_args.kwargs["text"]
    bot.send_photo.assert_awaited_once()
    bot.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_missing_source_is_an_explanation_without_rendering(request_env, monkeypatch):
    bot, loader = request_env
    loader.return_value = StatsAllSnapshot({}, STATS_ALL_MISSING)
    font_reader = MagicMock()
    monkeypatch.setattr("PIL.ImageFont.truetype", font_reader)
    result = await delivery.deliver_taste_card(bot, 42, "anime")
    assert result.state == "unavailable"
    font_reader.assert_not_called()
    bot.send_photo.assert_not_awaited()
    assert "ещё не готов" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_render_failure_is_short_safe_notice(request_env, monkeypatch):
    bot, _ = request_env
    monkeypatch.setattr("PIL.ImageFont.truetype", MagicMock(side_effect=OSError("private path")))
    result = await delivery.deliver_taste_card(bot, 42, "anime")
    assert result.state == "render_error"
    bot.send_photo.assert_not_awaited()
    assert "private path" not in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_cancelled_worker_is_drained_and_busy_requests_never_queue(request_env, monkeypatch):
    bot, loader = request_env
    original_font_reader = ImageFont.truetype
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def read_font(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        result = original_font_reader(*args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr("PIL.ImageFont.truetype", read_font)
    first = asyncio.create_task(delivery.deliver_taste_card(bot, 42, "anime"))
    assert await asyncio.to_thread(started.wait, 3)
    try:
        busy = await delivery.deliver_taste_card(bot, 43, "anime")
        assert busy.state == "busy" and loader.call_count == 1
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert finished.is_set()
    bot.send_photo.assert_not_awaited()
    monkeypatch.setattr("PIL.ImageFont.truetype", original_font_reader)
    resumed = await delivery.deliver_taste_card(bot, 42, "anime")
    assert resumed.state == "sent" and loader.call_count == 2
    bot.send_photo.assert_awaited_once()
