# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Проверка GitHub Release: парсинг, кэш и однократная доставка."""

import asyncio
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import aiohttp
import pytest

import backup
import project_meta
import storage
import updates

PROJECT_META_PATH = Path(__file__).resolve().parents[1] / "project_meta.py"


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status = status
        self.payload = payload
        self.text_payload = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return self.text_payload


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
    monkeypatch.setattr(
        updates,
        "MAIN_VERSION_API",
        "https://api.github.test/repos/example/project/contents/project_meta.py",
    )
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
async def test_fetch_main_version_requests_raw_project_meta_from_main(release_build):
    source = 'PROJECT_VERSION = "v1.4.0"\r\nraise RuntimeError("must not execute")\r\n'
    session = FakeSession(FakeResponse(text=source))

    assert await updates.fetch_main_version(session) == "v1.4.0"
    url, kwargs = session.calls[0]
    assert url.endswith("/contents/project_meta.py")
    assert kwargs["params"] == {"ref": "main"}
    assert kwargs["headers"]["Accept"] == "application/vnd.github.raw+json"


@pytest.mark.asyncio
async def test_fetch_main_version_returns_none_for_missing_file(release_build):
    session = FakeSession(FakeResponse(status=404, text="not found"))

    assert await updates.fetch_main_version(session) is None


@pytest.mark.asyncio
async def test_fetch_main_version_returns_none_on_network_error(release_build):
    session = MagicMock()
    session.get.side_effect = aiohttp.ClientError("network unavailable")

    assert await updates.fetch_main_version(session) is None


@pytest.mark.parametrize(
    "source",
    [
        "",
        'PROJECT_VERSION = "v1.2"',
        'PROJECT_VERSION = "v01.2.3"',
        "PROJECT_VERSION = 'v1.2.3'",
        ' PROJECT_VERSION = "v1.2.3"',
        'PROJECT_VERSION = "v1.2.3"  ',
        'PROJECT_VERSION = "v1.2.3"\nPROJECT_VERSION = "v1.2.4"',
        'PROJECT_VERSION = "v1.2.3"\nPROJECT_VERSION = get_version()',
    ],
)
def test_parse_main_version_rejects_non_strict_or_ambiguous_source(source):
    assert updates.parse_main_version(source) is None


def test_parse_main_version_accepts_current_project_meta_contract():
    source = PROJECT_META_PATH.read_text(encoding="utf-8")

    assert updates.parse_main_version(source) == project_meta.PROJECT_VERSION


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
    monkeypatch.setattr(
        updates,
        "fetch_main_version",
        AsyncMock(return_value="v1.4.0"),
    )

    result = await updates.refresh_update_state(force=True)
    assert result["latest_version"] == "v1.3.0"
    assert result["latest_main_version"] == "v1.4.0"
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
    fetch_main = AsyncMock()
    save = MagicMock()
    monkeypatch.setattr(updates, "_utcnow", lambda: now)
    monkeypatch.setattr(updates, "load_update_state", lambda: state.copy())
    monkeypatch.setattr(updates, "save_update_state", save)
    monkeypatch.setattr(updates, "fetch_latest_release", fetch)
    monkeypatch.setattr(updates, "fetch_main_version", fetch_main)

    assert await updates.refresh_update_state(force=False) == state
    fetch.assert_not_awaited()
    fetch_main.assert_not_awaited()
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
async def test_scheduled_source_check_uses_both_version_sources(
    release_build,
    monkeypatch,
):
    monkeypatch.setattr(updates, "IS_FROZEN", False)
    state = {
        "last_checked_at": None,
        "latest_version": None,
        "release_url": None,
        "last_notified_version": None,
    }
    fetch = AsyncMock(return_value=updates.ReleaseInfo("v1.3.0", "https://release"))
    fetch_main = AsyncMock(return_value="v1.4.0")
    monkeypatch.setattr(updates, "load_update_state", lambda: state.copy())
    monkeypatch.setattr(updates, "save_update_state", lambda value: None)
    monkeypatch.setattr(updates, "fetch_latest_release", fetch)
    monkeypatch.setattr(updates, "fetch_main_version", fetch_main)

    result = await updates.refresh_update_state(force=False)

    fetch.assert_awaited_once()
    fetch_main.assert_awaited_once()
    assert result["latest_version"] == "v1.3.0"
    assert result["latest_main_version"] == "v1.4.0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("main_result", "release_result", "expected_main", "expected_release"),
    [
        ("v1.4.0", None, "v1.4.0", "v1.2.5"),
        (None, updates.ReleaseInfo("v1.3.0", "https://new-release"), "v1.3.5", "v1.3.0"),
    ],
)
async def test_refresh_update_state_merges_independent_results(
    release_build,
    monkeypatch,
    main_result,
    release_result,
    expected_main,
    expected_release,
):
    state = {
        "last_checked_at": None,
        "latest_main_version": "v1.3.5",
        "latest_version": "v1.2.5",
        "release_url": "https://old-release",
        "last_notified_version": "v1.2.5",
    }
    monkeypatch.setattr(updates, "load_update_state", lambda: state.copy())
    monkeypatch.setattr(updates, "save_update_state", lambda value: state.update(value))
    monkeypatch.setattr(
        updates,
        "fetch_main_version",
        AsyncMock(return_value=main_result),
    )
    monkeypatch.setattr(
        updates,
        "fetch_latest_release",
        AsyncMock(return_value=release_result),
    )

    result = await updates.refresh_update_state(force=True)

    assert result["latest_main_version"] == expected_main
    assert result["latest_version"] == expected_release
    assert result["last_notified_version"] == "v1.2.5"


@pytest.mark.asyncio
async def test_refresh_update_state_total_failure_preserves_last_valid_cache(
    release_build,
    monkeypatch,
):
    state = {
        "last_checked_at": None,
        "latest_main_version": "v1.3.5",
        "latest_version": "v1.2.5",
        "release_url": "https://old-release",
        "last_notified_version": "v1.2.5",
    }
    monkeypatch.setattr(updates, "load_update_state", lambda: state.copy())
    monkeypatch.setattr(updates, "save_update_state", lambda value: None)
    monkeypatch.setattr(
        updates,
        "fetch_main_version",
        AsyncMock(side_effect=RuntimeError("main unavailable")),
    )
    monkeypatch.setattr(
        updates,
        "fetch_latest_release",
        AsyncMock(side_effect=RuntimeError("release unavailable")),
    )

    result = await updates.refresh_update_state(force=True)

    assert result["latest_main_version"] == "v1.3.5"
    assert result["latest_version"] == "v1.2.5"
    assert result["release_url"] == "https://old-release"
    assert result["last_notified_version"] == "v1.2.5"


@pytest.mark.asyncio
async def test_refresh_update_state_propagates_fetch_cancellation(
    release_build,
    monkeypatch,
):
    monkeypatch.setattr(
        updates,
        "fetch_main_version",
        AsyncMock(side_effect=asyncio.CancelledError),
    )
    monkeypatch.setattr(
        updates,
        "fetch_latest_release",
        AsyncMock(return_value=None),
    )

    with pytest.raises(asyncio.CancelledError):
        await updates.refresh_update_state(force=True)


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
    monkeypatch.setattr(updates, "load_update_state", lambda: state.copy())
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


@pytest.mark.asyncio
async def test_stale_update_refresh_preserves_imported_notification_state(
    backup_env,
    release_build,
    monkeypatch,
):
    storage.save_update_state(storage._empty_update_state())
    started = asyncio.Event()
    resume = asyncio.Event()

    async def paused_fetch():
        started.set()
        await resume.wait()
        return updates.ReleaseInfo("v1.3.0", "https://new-release")

    monkeypatch.setattr(updates, "fetch_latest_release", paused_fetch)
    monkeypatch.setattr(updates, "fetch_main_version", AsyncMock(return_value=None))
    writer = asyncio.create_task(updates.refresh_update_state(force=True))
    await started.wait()

    imported = {
        "last_checked_at": "2026-08-01T00:00:00+00:00",
        "latest_version": "v1.2.5",
        "release_url": "https://imported-release",
        "last_notified_version": "v1.2.5",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("update_state.json", json.dumps(imported))
    await backup.restore_backup_zip(buf.getvalue())
    resume.set()
    await writer

    state = storage.load_update_state()
    assert state["latest_version"] == "v1.3.0"
    assert state["release_url"] == "https://new-release"
    assert state["last_notified_version"] == "v1.2.5"


@pytest.mark.asyncio
async def test_overlapping_update_refresh_keeps_newer_completed_check(
    release_build,
    monkeypatch,
):
    state = storage._empty_update_state()
    started = asyncio.Event()
    resume = asyncio.Event()
    checked_times = iter(
        [
            datetime(2026, 8, 21, 12, 0, 0),
            datetime(2026, 8, 21, 12, 1, 0),
        ]
    )
    fetch_count = 0

    async def fetch_release():
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 1:
            started.set()
            await resume.wait()
            return updates.ReleaseInfo("v1.3.0", "https://older-check")
        return updates.ReleaseInfo("v1.4.0", "https://newer-check")

    def load_state():
        return state.copy()

    def save_state(value):
        state.clear()
        state.update(value)

    monkeypatch.setattr(updates, "_utcnow", lambda: next(checked_times))
    monkeypatch.setattr(updates, "fetch_latest_release", fetch_release)
    monkeypatch.setattr(updates, "fetch_main_version", AsyncMock(return_value=None))
    monkeypatch.setattr(updates, "load_update_state", load_state)
    monkeypatch.setattr(updates, "save_update_state", save_state)

    older_writer = asyncio.create_task(updates.refresh_update_state(force=True))
    await started.wait()
    newer_result = await updates.refresh_update_state(force=True)
    resume.set()
    older_result = await older_writer

    assert newer_result["latest_version"] == "v1.4.0"
    assert older_result["latest_version"] == "v1.4.0"
    assert state["latest_version"] == "v1.4.0"
    assert state["release_url"] == "https://newer-check"
    assert state["last_checked_at"] == "2026-08-21T12:01:00"


def test_build_version_text_marks_source_mode(monkeypatch):
    monkeypatch.setattr(updates, "IS_FROZEN", False)
    text = updates.build_version_text({})
    assert "Python/source или Docker" in text
    assert "Shikimori" in text
    assert "GNU General Public License версии 3 или более поздней" in text


@pytest.mark.parametrize(
    ("stored", "rendered"),
    [
        ("2026-08-06T12:12:30.388825", "06.08.2026, 12:12 UTC"),
        ("2026-08-06T15:12:30+03:00", "06.08.2026, 12:12 UTC"),
        (None, "ещё не выполнялось"),
        ("", "ещё не выполнялось"),
        (123, "ещё не выполнялось"),
        ("not-an-iso-timestamp", "время неизвестно"),
    ],
)
def test_build_version_text_formats_checked_at_safely(monkeypatch, stored, rendered):
    monkeypatch.setattr(updates, "IS_FROZEN", False)

    text = updates.build_version_text({"last_checked_at": stored})

    assert "Версия этого бота:" in text
    assert "Актуальная версия проекта:" in text
    assert "Последняя версия для Windows:" in text
    assert f"Последнее обновление сведений: {rendered}" in text
    if isinstance(stored, str) and stored:
        assert stored not in text


def test_version_keyboard_contains_repository_and_release(monkeypatch):
    monkeypatch.setattr(updates, "REPOSITORY_URL", "https://github.test/project")
    monkeypatch.setattr(updates, "RELEASES_URL", "https://github.test/releases/latest")

    keyboard = updates.build_version_keyboard()

    assert [button.url for button in keyboard.inline_keyboard[0]] == [
        "https://github.test/project",
        "https://github.test/releases/latest",
    ]


def test_version_renderer_escapes_content_and_rejects_unsafe_links(monkeypatch):
    monkeypatch.setattr(updates, "PROJECT_SUMMARY", '<script>alert("x")</script>')
    monkeypatch.setattr(updates, "APP_VERSION", 'v1.2.3<unsafe>')
    monkeypatch.setattr(updates, "REPOSITORY_URL", "javascript:alert(1)")
    monkeypatch.setattr(updates, "RELEASES_URL", "https://github.test/releases/latest")
    runtime = updates.RuntimeSnapshot(3661, 1_750_000_000, True)

    text = updates.build_version_text(
        {
            "latest_main_version": '<b>v9.9.9</b>',
            "latest_version": "v1.2.3&next",
        },
        runtime=runtime,
        last_backup_at=10**1000,
    )
    keyboard = updates.build_version_keyboard(
        "https://user:secret@github.test/release",
    )

    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "v1.2.3&lt;unsafe&gt;" in text
    assert "Актуальная версия проекта: <code>неизвестна</code>" in text
    assert "Последняя версия для Windows: <code>неизвестна</code>" in text
    assert "javascript:" not in text
    assert "Последняя плановая резервная копия: неизвестно" in text
    assert keyboard.inline_keyboard[0][0].url == "https://github.test/releases/latest"

    monkeypatch.setattr(
        updates,
        "REPOSITORY_URL",
        'https://github.test/project?quote="x"&page=1',
    )
    linked_text = updates.build_version_text({}, runtime=runtime)
    assert 'quote=&quot;x&quot;&amp;page=1' in linked_text


@pytest.mark.asyncio
async def test_newer_main_without_windows_release_does_not_notify(
    release_build,
    monkeypatch,
):
    state = {
        "latest_main_version": "v9.0.0",
        "latest_version": None,
        "last_notified_version": None,
    }
    monkeypatch.setattr(updates, "refresh_update_state", AsyncMock(return_value=state))
    bot = AsyncMock()

    await updates.check_and_notify_update(bot)

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_background_refresh_never_sends_windows_notification(monkeypatch):
    state = {
        "latest_main_version": "v1.4.0",
        "latest_version": "v1.3.0",
        "last_notified_version": None,
    }
    refresh = AsyncMock(return_value=state)
    monkeypatch.setattr(updates, "IS_FROZEN", False)
    monkeypatch.setattr(updates, "refresh_update_state", refresh)
    bot = AsyncMock()

    await updates.check_and_notify_update(bot)

    refresh.assert_awaited_once_with()
    bot.send_message.assert_not_awaited()


def test_start_update_loop_is_disabled_without_release_identity(monkeypatch):
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
