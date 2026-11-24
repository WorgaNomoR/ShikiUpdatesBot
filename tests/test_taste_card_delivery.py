# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Один локальный срез, свежие uploads, отмена и сохранение неопределённости."""

import asyncio
import json
import os
import threading
from hashlib import sha256
from io import BytesIO
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto
from PIL import (
    Image,
    ImageFont,
)

import taste_card_delivery as delivery
from profile_avatar import ProfileAvatar
from storage import (
    STATS_ALL_MISSING,
    STATS_ALL_VALID,
    StatsAllSnapshot,
    read_stats_all_snapshot,
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
    monkeypatch.setattr("taste_card_delivery.read_stats_all_snapshot", loader)
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
    loader.assert_called_once_with()
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
@pytest.mark.parametrize("phase", ["read", "font"])
async def test_cancelled_worker_is_drained_and_busy_requests_never_queue(request_env, monkeypatch, phase):
    bot, loader = request_env
    original_font_reader = ImageFont.truetype
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def read_font(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        result = original_font_reader(*args, **kwargs)
        finished.set()
        return result

    if phase == "font":
        monkeypatch.setattr("PIL.ImageFont.truetype", read_font)
    else:
        def read_snapshot():
            started.set()
            release.wait(timeout=5)
            finished.set()
            return loader.return_value

        loader.side_effect = read_snapshot
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
    loader.side_effect = None
    resumed = await delivery.deliver_taste_card(bot, 42, "anime")
    assert resumed.state == "sent" and loader.call_count == 2
    bot.send_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_snapshot_read_and_conversion_run_off_the_event_loop(request_env):
    bot, loader = request_env
    owner_thread = threading.get_ident()
    snapshot = loader.return_value

    class WorkerData(dict):
        def get(self, *args):
            assert threading.get_ident() != owner_thread
            return super().get(*args)

    def read_snapshot(**kwargs):
        assert threading.get_ident() != owner_thread
        return StatsAllSnapshot(WorkerData(snapshot.data), snapshot.state)

    loader.side_effect = read_snapshot
    result = await delivery.deliver_taste_card(bot, 42, "anime")
    assert result.state == "sent"
    loader.assert_called_once()


@pytest.fixture
def published(monkeypatch, tmp_path):
    path = tmp_path / "stats_all.json"
    monkeypatch.setattr("storage.STATS_ALL_FILE", path)
    reader = MagicMock(wraps=read_stats_all_snapshot)
    monkeypatch.setattr("taste_card_delivery.read_stats_all_snapshot", reader)
    bot = MagicMock(send_photo=AsyncMock(return_value="photo"), send_message=AsyncMock())

    def replace_file(score):
        draft = path.with_suffix(".tmp")
        draft.write_text(json.dumps({
            "anime": {"titles": {"1": {"kind": "tv", "status": "completed", "score": score}}},
            "manga": {"titles": {"1": {"kind": "manga", "status": "completed", "score": score}}},
        }), encoding="utf-8")
        if path.exists():
            stamp = path.stat()
            os.utime(draft, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        draft.replace(path)

    replace_file(7)
    return bot, path, reader, replace_file


def _pause_at_font_read(monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = ImageFont.truetype

    def read_font(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr("PIL.ImageFont.truetype", read_font)
    return started, release


def _avatar(color):
    with BytesIO() as output:
        Image.new("RGB", (160, 160), color).save(output, format="PNG")
        image = output.getvalue()
    return ProfileAvatar(1, image, sha256(image).digest())


def _avatar_pixel(photo):
    with Image.open(BytesIO(photo)) as image:
        return image.getpixel((1063, 120))


@pytest.mark.asyncio
async def test_warm_payload_skips_read_and_render_and_replacement_invalidates(published, monkeypatch):
    bot, path, reader, replace_file = published
    renderer = MagicMock(wraps=delivery.render_taste_card)
    monkeypatch.setattr("taste_card_delivery.render_taste_card", renderer)
    assert (await delivery.deliver_taste_card(bot, 42, "anime")).state == "sent"
    first = bot.send_photo.await_args.kwargs
    assert (await delivery.deliver_taste_card(bot, 42, "anime")).state == "sent"
    second = bot.send_photo.await_args.kwargs
    assert first["photo"] is not second["photo"] and first["photo"].data == second["photo"].data
    assert first["caption"] == second["caption"]
    assert reader.call_count == renderer.call_count == 1
    before = path.stat()
    replace_file(8)
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    await delivery.deliver_taste_card(bot, 42, "anime")
    assert reader.call_count == renderer.call_count == 2
    assert bot.send_photo.await_args.kwargs["photo"].data != first["photo"].data


@pytest.mark.asyncio
async def test_avatar_version_invalidates_only_matching_revision_and_reuses_same_pixels(published, monkeypatch):
    bot, _, reader, _ = published
    selected = ProfileAvatar()
    monkeypatch.setattr("taste_card_delivery.current_profile_avatar", lambda: selected)
    assert (await delivery.deliver_taste_card(bot, 42, "anime")).state == "sent"
    fallback_pixel = _avatar_pixel(bot.send_photo.await_args.kwargs["photo"].data)
    color = (32, 96, 160)
    selected = _avatar(color)
    assert (await delivery.deliver_taste_card(bot, 42, "anime")).state == "sent"
    avatar_photo = bot.send_photo.await_args.kwargs["photo"].data
    assert _avatar_pixel(avatar_photo) == pytest.approx(color, abs=3)
    selected = _avatar(color)
    assert (await delivery.deliver_taste_card(bot, 42, "anime")).state == "sent"
    assert bot.send_photo.await_args.kwargs["photo"].data == avatar_photo
    assert reader.call_count == 2
    selected = ProfileAvatar(2)
    assert (await delivery.deliver_taste_card(bot, 42, "anime")).state == "sent"
    assert _avatar_pixel(bot.send_photo.await_args.kwargs["photo"].data) == fallback_pixel
    assert reader.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["snapshot", "avatar", "identity"])
async def test_refresh_during_render_cannot_cache_stale_payload(published, monkeypatch, changed):
    bot, _, reader, replace_file = published
    started, release = _pause_at_font_read(monkeypatch)
    old_color, new_color = (32, 96, 160), (160, 96, 32)
    avatar = _avatar(old_color)
    monkeypatch.setattr("taste_card_delivery.current_profile_avatar", lambda: avatar)
    renderer = MagicMock(wraps=delivery.render_taste_card)
    monkeypatch.setattr("taste_card_delivery.render_taste_card", renderer)
    first = asyncio.create_task(delivery.deliver_taste_card(bot, 42, "anime"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        if changed == "snapshot":
            replace_file(8)
        elif changed == "avatar":
            avatar = _avatar(new_color)
        else:
            avatar = ProfileAvatar(2)
    finally:
        release.set()
    result = await first
    if changed == "identity":
        assert result.state == "render_error"
        bot.send_photo.assert_not_awaited()
    else:
        assert result.state == "sent"
        assert _avatar_pixel(bot.send_photo.await_args.kwargs["photo"].data) == pytest.approx(old_color, abs=3)
    assert (await delivery.deliver_taste_card(bot, 42, "anime")).state == "sent"
    fresh_photo = bot.send_photo.await_args.kwargs["photo"].data
    fresh_pixel = _avatar_pixel(fresh_photo)
    assert renderer.call_args.args[0].preferences.baseline == (8 if changed == "snapshot" else 7)
    if changed == "identity":
        assert fresh_pixel != pytest.approx(old_color, abs=3)
    else:
        expected_color = new_color if changed == "avatar" else old_color
        assert fresh_pixel == pytest.approx(expected_color, abs=3)
    assert (await delivery.deliver_taste_card(bot, 42, "anime")).state == "sent"
    assert reader.call_count == renderer.call_count == 2
    assert bot.send_photo.await_args.kwargs["photo"].data == fresh_photo


@pytest.mark.asyncio
async def test_cache_does_not_store_errors_and_warm_category_bypasses_busy_worker(published, monkeypatch):
    bot, path, reader, replace_file = published
    path.write_text("broken data", encoding="utf-8")
    for _ in range(2):
        assert (await delivery.deliver_taste_card(bot, 42, "anime")).state == "unavailable"
    assert reader.call_count == 2
    replace_file(7)
    assert (await delivery.deliver_taste_card(bot, 42, "anime")).state == "sent"
    warm_photo = bot.send_photo.await_args.kwargs["photo"].data
    started, release = _pause_at_font_read(monkeypatch)
    worker = asyncio.create_task(delivery.deliver_taste_card(bot, 42, "manga"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        assert (await delivery.deliver_taste_card(bot, 42, "anime")).state == "sent"
        assert bot.send_photo.await_args.kwargs["photo"].data == warm_photo
        assert (await delivery.deliver_taste_card(bot, 42, "ranobe")).state == "busy"
        assert reader.call_count == 4
    finally:
        release.set()
    assert (await worker).state == "sent"
