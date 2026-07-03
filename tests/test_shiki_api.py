# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты shiki_api.py — сетевые фетчи (ошибки/таймауты) и is_relevant.

Мокаем только сетевую границу; is_relevant — чистая функция, не замокана.
"""

import asyncio
import json

import aiohttp
import pytest

import shiki_api
from shiki_api import (
    fetch_favourites,
    fetch_history,
)

# ============================================================
# fetch_history()
# ============================================================

@pytest.mark.asyncio
async def test_fetch_history_timeout(monkeypatch):
    class FakeSession:
        def get(self, *args, **kwargs):
            raise asyncio.TimeoutError()

    result = await fetch_history(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_history_client_error(monkeypatch):
    class FakeSession:
        def get(self, *args, **kwargs):
            raise aiohttp.ClientError("boom")

    result = await fetch_history(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_history_invalid_json(monkeypatch):
    class FakeResponse:
        status = 200

        async def json(self):
            raise json.JSONDecodeError("bad", "", 0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    result = await fetch_history(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_history_bad_status(monkeypatch):
    class FakeResponse:
        status = 500

        async def json(self):
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    result = await fetch_history(FakeSession())

    assert result is None


# ============================================================
# fetch_favourites()
# ============================================================

@pytest.mark.asyncio
async def test_fetch_favourites_timeout(monkeypatch):
    class FakeSession:
        def get(self, *args, **kwargs):
            raise asyncio.TimeoutError()

    result = await fetch_favourites(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_favourites_client_error(monkeypatch):
    class FakeSession:
        def get(self, *args, **kwargs):
            raise aiohttp.ClientError("boom")

    result = await fetch_favourites(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_favourites_invalid_json(monkeypatch):
    class FakeResponse:
        status = 200

        async def json(self):
            raise json.JSONDecodeError("bad", "", 0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    result = await fetch_favourites(FakeSession())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_favourites_bad_status(monkeypatch):
    class FakeResponse:
        status = 500

        async def json(self):
            return {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    result = await fetch_favourites(FakeSession())

    assert result is None


# ════════════════════════════════════════════════════════════════
#  is_relevant — фильтр значимости (сама функция, не замокана)
# ════════════════════════════════════════════════════════════════

def test_is_relevant_anime_allowed_kinds():
    for kind in ("tv", "movie", "ova", "ona"):
        assert shiki_api.is_relevant("anime", kind) is True, kind

def test_is_relevant_anime_drops_specials_and_clips():
    for kind in ("special", "tv_special", "music", "pv", "cm"):
        assert shiki_api.is_relevant("anime", kind) is False, kind

def test_is_relevant_manga_blocks_oneshot_doujin():
    assert shiki_api.is_relevant("manga", "one_shot") is False
    assert shiki_api.is_relevant("manga", "doujin") is False

def test_is_relevant_manga_allows_regular():
    assert shiki_api.is_relevant("manga", "manga") is True

def test_is_relevant_empty_kind_is_false():
    assert shiki_api.is_relevant("anime", "") is False
