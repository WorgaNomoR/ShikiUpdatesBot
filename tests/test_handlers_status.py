# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты handlers.cmd_status — команда /status (текущие просмотры/чтение)."""

import pytest

import handlers


class DummyMessage:
    def __init__(self):
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))


def _anime_item(name, kind="tv", status="watching"):
    return {"_status": status, "anime": {"name": name, "kind": kind}}


def _manga_item(name, status="watching"):
    return {"_status": status, "manga": {"name": name}}


def _patch_rates(monkeypatch, *, anime=(), manga=()):
    # anime/manga: список текущих rate'ов; [] — пусто, None — сбой API
    async def _fetch(media, statuses):
        return anime if media == "anime" else manga
    monkeypatch.setattr("handlers.fetch_current_rates", _fetch)


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


@pytest.mark.asyncio
async def test_status_manga_failed_anime_ok(monkeypatch):
    _patch_rates(monkeypatch, anime=[_anime_item("Ergo Proxy")], manga=None)
    text = await _run()
    assert "не удалось получить данные" in text.lower()
