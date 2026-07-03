# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты shiki_api.py — сетевые фетчи (успех/ошибки/таймауты) и is_relevant.

Сетевую границу мокаем одной парой _FakeSession/_FakeResponse (без копипасты
в каждом тесте); is_relevant — чистая функция, не замокана.
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


# ── Мок сетевой границы: одна пара вместо копипасты в каждом тесте ──
class _FakeResponse:
    def __init__(self, status, *, json_value=None, json_exc=None):
        self.status = status
        self._json_value = json_value
        self._json_exc = json_exc

    async def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json_value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, *, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc

    def get(self, *args, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


_FETCHERS = [
    pytest.param(fetch_history, id="history"),
    pytest.param(fetch_favourites, id="favourites"),
]


# ============================================================
# fetch_history / fetch_favourites — сетевая граница
# ============================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("fetch", _FETCHERS)
async def test_fetch_returns_none_on_timeout(fetch):
    assert await fetch(_FakeSession(raise_exc=asyncio.TimeoutError())) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch", _FETCHERS)
async def test_fetch_returns_none_on_client_error(fetch):
    assert await fetch(_FakeSession(raise_exc=aiohttp.ClientError("boom"))) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch", _FETCHERS)
async def test_fetch_returns_none_on_invalid_json(fetch):
    resp = _FakeResponse(200, json_exc=json.JSONDecodeError("bad", "", 0))
    assert await fetch(_FakeSession(response=resp)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch", _FETCHERS)
async def test_fetch_returns_none_on_bad_status(fetch):
    resp = _FakeResponse(500, json_value=[])
    assert await fetch(_FakeSession(response=resp)) is None


@pytest.mark.asyncio
async def test_fetch_history_returns_parsed_list_on_success():
    payload = [{"id": 1, "description": "оценено на 9"}]
    resp = _FakeResponse(200, json_value=payload)
    assert await fetch_history(_FakeSession(response=resp)) == payload


@pytest.mark.asyncio
async def test_fetch_favourites_returns_parsed_dict_on_success():
    payload = {"animes": [{"id": 226, "name": "Elfen Lied"}], "mangas": []}
    resp = _FakeResponse(200, json_value=payload)
    assert await fetch_favourites(_FakeSession(response=resp)) == payload


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


def test_is_relevant_unknown_media_type_is_false():
    assert shiki_api.is_relevant("person", "tv") is False
