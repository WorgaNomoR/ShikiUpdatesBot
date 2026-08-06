# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Проверка GitHub Release: парсинг, кэш и однократная доставка."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import updates


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.fixture
def release_build(monkeypatch):
    monkeypatch.setattr(updates, "IS_FROZEN", True)
    monkeypatch.setattr(updates, "HAS_RELEASE_INFO", True)
    monkeypatch.setattr(updates, "APP_VERSION", "v1.2.0")
    monkeypatch.setattr(updates, "LATEST_RELEASE_API", "https://api.github.test/releases/latest")
    monkeypatch.setattr(updates, "RELEASES_URL", "https://github.test/releases/latest")


@pytest.mark.asyncio
async def test_fetch_latest_release_accepts_semver_and_windows_asset(release_build):
    session = FakeSession(FakeResponse(payload={
        "tag_name": "v1.3.0",
        "assets": [{"name": "ShikiUpdatesBot-windows-x64.zip"}],
    }))
    release = await updates.fetch_latest_release(session)
    assert release == updates.ReleaseInfo("v1.3.0", "https://github.test/releases/latest")
    assert session.calls[0][0] == "https://api.github.test/releases/latest"


@pytest.mark.asyncio
async def test_fetch_latest_release_rejects_missing_windows_asset(release_build):
    session = FakeSession(FakeResponse(payload={"tag_name": "v1.3.0", "assets": []}))
    assert await updates.fetch_latest_release(session) is None


@pytest.mark.asyncio
async def test_fetch_latest_release_returns_none_on_network_error(release_build):
    session = MagicMock()
    session.get.side_effect = aiohttp.ClientError("network unavailable")

    assert await updates.fetch_latest_release(session) is None


@pytest.mark.asyncio
async def test_refresh_update_state_persists_success(release_build, monkeypatch):
    state = {
        "last_checked_at": None,
        "latest_version": None,
        "release_url": None,
        "last_notified_version": None,
    }
    saved = []
    monkeypatch.setattr(updates, "load_update_state", lambda: state.copy())
    monkeypatch.setattr(updates, "save_update_state", lambda value: saved.append(value.copy()))
    monkeypatch.setattr(
        updates,
        "fetch_latest_release",
        AsyncMock(return_value=updates.ReleaseInfo("v1.3.0", "https://release")),
    )

    result = await updates.refresh_update_state(force=True)
    assert result["latest_version"] == "v1.3.0"
    assert result["last_checked_at"]
    assert saved[-1]["release_url"] == "https://release"


@pytest.mark.asyncio
async def test_refresh_update_state_honors_recent_naive_timestamp(
    release_build,
    monkeypatch,
):
    now = datetime(2026, 8, 6, 12, 0, 0)
    state = {
        "last_checked_at": now.isoformat(),
        "latest_version": "v1.3.0",
        "release_url": "https://release",
        "last_notified_version": None,
    }
    fetch = AsyncMock()
    save = MagicMock()
    monkeypatch.setattr(updates, "_utcnow", lambda: now)
    monkeypatch.setattr(updates, "load_update_state", lambda: state.copy())
    monkeypatch.setattr(updates, "save_update_state", save)
    monkeypatch.setattr(updates, "fetch_latest_release", fetch)

    assert await updates.refresh_update_state(force=False) == state
    fetch.assert_not_awaited()
    save.assert_not_called()


def test_checked_recently_normalizes_aware_timestamp(monkeypatch):
    monkeypatch.setattr(updates, "_utcnow", lambda: datetime(2026, 8, 6, 12, 0, 0))

    assert updates._checked_recently({
        "last_checked_at": "2026-08-06T15:00:00+03:00",
    }) is True


def test_checked_recently_rejects_future_timestamp(monkeypatch):
    monkeypatch.setattr(updates, "_utcnow", lambda: datetime(2026, 8, 6, 12, 0, 0))

    assert updates._checked_recently({
        "last_checked_at": "2026-08-06T12:00:01",
    }) is False


@pytest.mark.asyncio
async def test_forced_source_check_uses_release_api(release_build, monkeypatch):
    monkeypatch.setattr(updates, "IS_FROZEN", False)
    state = {
        "last_checked_at": None,
        "latest_version": None,
        "release_url": None,
        "last_notified_version": None,
    }
    fetch = AsyncMock(return_value=updates.ReleaseInfo("v1.3.0", "https://release"))
    monkeypatch.setattr(updates, "load_update_state", lambda: state.copy())
    monkeypatch.setattr(updates, "save_update_state", lambda value: None)
    monkeypatch.setattr(updates, "fetch_latest_release", fetch)

    result = await updates.refresh_update_state(force=True)

    fetch.assert_awaited_once()
    assert result["latest_version"] == "v1.3.0"


@pytest.mark.asyncio
async def test_notification_marks_version_only_after_delivery(release_build, monkeypatch):
    state = {
        "last_checked_at": "2026-08-05T12:00:00+00:00",
        "latest_version": "v1.3.0",
        "release_url": "https://release",
        "last_notified_version": None,
    }
    saved = []
    monkeypatch.setattr(updates, "refresh_update_state", AsyncMock(return_value=state))
    monkeypatch.setattr(updates, "save_update_state", lambda value: saved.append(value.copy()))
    bot = AsyncMock()

    await updates.check_and_notify_update(bot)

    bot.send_message.assert_awaited_once()
    assert saved[-1]["last_notified_version"] == "v1.3.0"


@pytest.mark.asyncio
async def test_notification_delivery_failure_is_retried_later(release_build, monkeypatch):
    state = {
        "last_checked_at": "2026-08-05T12:00:00+00:00",
        "latest_version": "v1.3.0",
        "release_url": "https://release",
        "last_notified_version": None,
    }
    monkeypatch.setattr(updates, "refresh_update_state", AsyncMock(return_value=state))
    save = MagicMock()
    monkeypatch.setattr(updates, "save_update_state", save)
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("owner unavailable")

    await updates.check_and_notify_update(bot)

    save.assert_not_called()
    assert state["last_notified_version"] is None


def test_build_version_text_marks_source_mode(monkeypatch):
    monkeypatch.setattr(updates, "IS_FROZEN", False)
    text = updates.build_version_text({})
    assert "Python/source" in text
    assert "вручную через /version" in text
    assert "Shikimori" in text


def test_version_keyboard_contains_repository_and_release(monkeypatch):
    monkeypatch.setattr(updates, "REPOSITORY_URL", "https://github.test/project")
    monkeypatch.setattr(updates, "RELEASES_URL", "https://github.test/releases/latest")

    keyboard = updates.build_version_keyboard()

    assert [button.url for button in keyboard.inline_keyboard[0]] == [
        "https://github.test/project",
        "https://github.test/releases/latest",
    ]


def test_start_update_loop_is_disabled_outside_release_exe(monkeypatch):
    create_task = MagicMock()
    monkeypatch.setattr(updates, "update_checks_enabled", lambda: False)
    monkeypatch.setattr(updates, "_update_task", None)
    monkeypatch.setattr(updates.asyncio, "create_task", create_task)

    assert updates.start_update_loop(AsyncMock()) is False
    assert updates._update_task is None
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_start_update_loop_is_idempotent(monkeypatch):
    started = asyncio.Event()

    async def fake_update_loop(bot):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(updates, "update_checks_enabled", lambda: True)
    monkeypatch.setattr(updates, "update_loop", fake_update_loop)
    monkeypatch.setattr(updates, "_update_task", None)
    bot = AsyncMock()

    assert updates.start_update_loop(bot) is True
    task = updates._update_task
    assert task is not None
    await started.wait()
    assert updates.start_update_loop(bot) is False
    assert updates._update_task is task

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
