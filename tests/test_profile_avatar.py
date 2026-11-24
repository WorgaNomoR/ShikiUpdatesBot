# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Границы аватара и привязка сетевого ответа к проверенному ID."""

import asyncio
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import profile_avatar as avatars


@pytest.fixture(autouse=True)
def origin(monkeypatch):
    monkeypatch.setattr("profile_avatar.SHIKI_BASE_URL", "https://shikimori.io")


@pytest.mark.parametrize("url", [
    "https://evil.example/system/users/x160/42.png",
    "https://shikimori.io@evil.example/system/users/x160/42.png",
    "https://evil.example@shikimori.io/system/users/x160/42.png",
    "https://shikimori.io:8443/system/users/x160/42.png",
    "https://shikimori.io/system/users/x160/41.png",
    "http://shikimori.io/system/users/x160/42.png",
    "//shikimori.io/system/users/x160/42.png",
    "https://shikimori.io\\evil.example/system/users/x160/42.png",
    "/system/users/x160/42.png#fragment",
    "/system/users/x160/%34%32.png",
    "data:image/png;base64,abc",
    None,
])
def test_avatar_url_binds_https_origin_and_exact_id(url):
    assert avatars._avatar_url(url, 42) is None


def test_avatar_url_accepts_relative_path_and_cache_busting_query():
    assert avatars._avatar_url("/system/users/x160/42.png?1758013685", 42) == (
        "https://shikimori.io/system/users/x160/42.png?1758013685"
    )


def _png(color=(50, 80, 140, 190), size=(48, 64)):
    output = BytesIO()
    Image.new("RGBA", size, color).save(output, format="PNG", dpi=(96, 96))
    return output.getvalue()


def test_decode_keeps_alpha_and_rejects_empty_oversized_or_invalid_images():
    data = avatars._normalize(_png())
    with Image.open(BytesIO(data)) as img:
        assert img.format == "PNG" and img.size == (160, 160)
        assert img.info == {} and img.getpixel((80, 80))[3] == 190
    assert avatars._normalize(_png(size=(513, 8))) is None
    assert avatars._normalize(_png(color=(50, 80, 140, 0))) is None
    assert avatars._normalize(b"not an image") is None


@pytest.mark.asyncio
async def test_avatar_refresh_is_bounded_and_keeps_equal_decoded_versions(monkeypatch):
    fetched = AsyncMock(return_value=_png())
    monkeypatch.setattr("profile_avatar.fetch_profile_avatar", fetched)
    now = 0.0
    monkeypatch.setattr("profile_avatar.time", type("Clock", (), {"monotonic": staticmethod(lambda: now)}))
    url = "/system/users/x160/42.png"
    avatars.observe_profile_avatar(42, url)
    state = avatars._states[asyncio.get_running_loop()]
    assert avatars.current_profile_avatar().image is None
    task = state.task
    avatars.observe_profile_avatar(42, url)
    assert state.task is task
    await task
    first = avatars.current_profile_avatar()
    assert first.image and first.user_id == 42 and first.version[0] == 42
    fetched.assert_awaited_once()
    avatars.observe_profile_avatar(42, url)
    assert state.task is None
    now += avatars._REFRESH_AFTER + 1
    avatars.observe_profile_avatar(42, url)
    await state.task
    assert avatars.current_profile_avatar().version == first.version
    fetched.return_value = None
    now += avatars._REFRESH_AFTER + 1
    avatars.observe_profile_avatar(42, url)
    await state.task
    assert avatars.current_profile_avatar() == first
    avatars.observe_profile_avatar(42, url)
    assert state.task is None


@pytest.mark.asyncio
async def test_in_flight_response_cannot_restore_avatar_after_identity_conflict(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*args):
        entered.set()
        await release.wait()
        return _png()

    monkeypatch.setattr("profile_avatar.fetch_profile_avatar", fetch)
    avatars.observe_profile_avatar(42, "/system/users/x160/42.png")
    state = avatars._states[asyncio.get_running_loop()]
    task = state.task
    await entered.wait()
    avatars.observe_profile_avatar(99, "/system/users/x160/99.png", matches=False)
    release.set()
    await task
    current = avatars.current_profile_avatar()
    assert current.user_id == 99 and current.version == (None, None)
