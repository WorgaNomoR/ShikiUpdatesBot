# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты handlers.cmd_status — команда /status (текущие просмотры/чтение)."""

import asyncio

import pytest

import handlers


class DummyMessage:
    def __init__(self, chat_id=1):
        self.chat_id = chat_id
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))


def _anime_item(name, kind="tv", status="watching"):
    return {"_status": status, "anime": {"name": name, "kind": kind}}


def _manga_item(name, status="watching"):
    return {"_status": status, "manga": {"name": name}}


def _patch_rates(monkeypatch, *, anime=(), manga=()):
    # anime/manga: список текущих rate'ов; [] — пусто, None — сбой API
    calls = []

    async def _fetch(media, statuses):
        calls.append((media, statuses))
        return anime if media == "anime" else manga
    monkeypatch.setattr("handlers.fetch_current_rates", _fetch)
    return calls


@pytest.fixture(autouse=True)
def _reset_status_cache(monkeypatch):
    """Process-local кеш и его лок не должны утекать между event loop тестов."""
    monkeypatch.setattr(handlers, "_status_cache", None)
    monkeypatch.setattr(handlers, "_status_cache_at", 0.0)
    monkeypatch.setattr(handlers, "_status_cache_lock", None)


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


async def _run():
    msg = DummyMessage()
    await handlers.cmd_status(msg)
    return msg.calls[0][0]


@pytest.mark.asyncio
async def test_status_nothing(monkeypatch):
    _patch_rates(monkeypatch, anime=[], manga=[])
    text = await _run()
    assert "ничего не смотрит" in text.lower()


@pytest.mark.asyncio
async def test_status_api_failure_both(monkeypatch):
    _patch_rates(monkeypatch, anime=None, manga=None)
    text = await _run()
    assert "не удалось получить данные" in text.lower()


@pytest.mark.asyncio
async def test_status_anime_only(monkeypatch):
    _patch_rates(monkeypatch, anime=[_anime_item("Ergo Proxy")], manga=[])
    text = await _run()
    assert "Сейчас смотрит" in text
    assert "Ergo Proxy" in text
    assert "Сейчас читает" not in text


@pytest.mark.asyncio
async def test_status_manga_only(monkeypatch):
    _patch_rates(monkeypatch, anime=[], manga=[_manga_item("Berserk")])
    text = await _run()
    assert "Сейчас читает" in text
    assert "Berserk" in text
    assert "Сейчас смотрит" not in text


@pytest.mark.asyncio
async def test_status_anime_and_manga(monkeypatch):
    _patch_rates(monkeypatch, anime=[_anime_item("Ergo Proxy")], manga=[_manga_item("Berserk")])
    text = await _run()
    assert "Сейчас смотрит" in text and "Сейчас читает" in text
    assert "Ergo Proxy" in text and "Berserk" in text


@pytest.mark.asyncio
async def test_status_filters_disallowed_anime_kind(monkeypatch):
    # music — нерелевантный kind → отфильтрован → как будто ничего не смотрит
    _patch_rates(monkeypatch, anime=[_anime_item("Music Clip", kind="music")], manga=[])
    text = await _run()
    assert "ничего не смотрит" in text.lower()
    assert "Music Clip" not in text


@pytest.mark.asyncio
async def test_status_anime_failed_manga_ok(monkeypatch):
    # частичный сбой (аниме упало) → честно об ошибке, не показываем половину
    _patch_rates(monkeypatch, anime=None, manga=[_manga_item("Berserk")])
    text = await _run()
    assert "не удалось получить данные" in text.lower()
    assert "Berserk" not in text


@pytest.mark.asyncio
async def test_status_manga_failed_anime_ok(monkeypatch):
    _patch_rates(monkeypatch, anime=[_anime_item("Ergo Proxy")], manga=None)
    text = await _run()
    assert "не удалось получить данные" in text.lower()
    assert "Ergo Proxy" not in text


@pytest.mark.asyncio
async def test_status_cache_reuses_raw_rates_across_chats_and_renders_each_time(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("handlers.time.monotonic", clock)
    calls = _patch_rates(
        monkeypatch,
        anime=[_anime_item("Ergo Proxy")],
        manga=[_manga_item("Berserk")],
    )
    rendered = []
    real_formatter = handlers.format_rate_entry

    def _format(item, media):
        rendered.append((item, media))
        return real_formatter(item, media)

    monkeypatch.setattr("handlers.format_rate_entry", _format)

    first = DummyMessage(chat_id=1)
    second = DummyMessage(chat_id=2)
    await handlers.cmd_status(first)
    await handlers.cmd_status(second)

    assert [media for media, _statuses in calls] == ["anime", "manga"]
    assert [media for _item, media in rendered] == ["anime", "manga", "anime", "manga"]
    assert first.calls[0][0] == second.calls[0][0]


@pytest.mark.asyncio
async def test_status_cache_expires_at_fixed_ttl(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("handlers.time.monotonic", clock)
    calls = _patch_rates(monkeypatch, anime=[], manga=[])

    await handlers.cmd_status(DummyMessage())
    clock.advance(handlers._STATUS_CACHE_TTL - 0.001)
    await handlers.cmd_status(DummyMessage())
    assert len(calls) == 2

    clock.advance(0.001)
    await handlers.cmd_status(DummyMessage())
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_status_cache_collapses_concurrent_cold_calls(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("handlers.time.monotonic", clock)
    calls = []
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def _fetch(media, statuses):
        calls.append((media, statuses))
        if len(calls) == 2:
            refresh_started.set()
        await release_refresh.wait()
        return [_anime_item("Ergo Proxy")] if media == "anime" else []

    monkeypatch.setattr("handlers.fetch_current_rates", _fetch)
    first = DummyMessage(chat_id=1)
    second = DummyMessage(chat_id=2)

    first_task = asyncio.create_task(handlers.cmd_status(first))
    await refresh_started.wait()
    second_task = asyncio.create_task(handlers.cmd_status(second))
    await asyncio.sleep(0)
    release_refresh.set()
    await asyncio.gather(first_task, second_task)

    assert [media for media, _statuses in calls] == ["anime", "manga"]
    assert first.calls[0][0] == second.calls[0][0]


@pytest.mark.asyncio
async def test_status_full_failure_is_not_cached(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("handlers.time.monotonic", clock)
    responses = {
        "anime": [None, [_anime_item("Ergo Proxy")]],
        "manga": [[], []],
    }
    calls = []

    async def _fetch(media, statuses):
        calls.append((media, statuses))
        return responses[media].pop(0)

    monkeypatch.setattr("handlers.fetch_current_rates", _fetch)

    failed = DummyMessage()
    await handlers.cmd_status(failed)
    assert "не удалось получить данные" in failed.calls[0][0].lower()
    assert handlers._status_cache is None

    recovered = DummyMessage()
    await handlers.cmd_status(recovered)
    assert "Ergo Proxy" in recovered.calls[0][0]
    assert len(calls) == 4
