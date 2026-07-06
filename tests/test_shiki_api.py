# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты shiki_api.py — сетевые фетчи (успех/ошибки/таймауты), get_media_info и is_relevant.

Сетевую границу мокаем одной парой _FakeSession/_FakeResponse (без копипасты
в каждом тесте); is_relevant — чистая функция, не замокана.
"""

import asyncio
import json
import types

import aiohttp
import pytest

import shiki_api
from shiki_api import (
    fetch_favourites,
    fetch_history,
    get_media_info,
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
@pytest.mark.parametrize(
    "exc",
    [
        json.JSONDecodeError("bad", "", 0),
        aiohttp.ContentTypeError(types.SimpleNamespace(real_url="http://shikimori.io"), ()),
    ],
    ids=["json_decode_error", "content_type_error"],
)
async def test_fetch_returns_none_on_invalid_json(fetch, exc):
    resp = _FakeResponse(200, json_exc=exc)
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


def test_is_relevant_manga_allows_regular_kinds():
    for kind in ("manga", "manhwa", "ranobe", "novel"):
        assert shiki_api.is_relevant("manga", kind) is True, kind


def test_is_relevant_empty_kind_is_false():
    assert shiki_api.is_relevant("anime", "") is False
    assert shiki_api.is_relevant("manga", "") is False


def test_is_relevant_unknown_media_type_is_false():
    assert shiki_api.is_relevant("person", "tv") is False
# ════════════════════════════════════════════════════════════════
#  get_media_info — media_type/kind из записи истории
# ════════════════════════════════════════════════════════════════

def test_get_media_info_anime_by_type():
    assert get_media_info({"target": {"type": "Anime", "kind": "tv"}}) == ("anime", "tv")


def test_get_media_info_novel_kind_is_manga():
    assert get_media_info({"target": {"kind": "novel"}}) == ("manga", "novel")


def test_get_media_info_fallback_to_anime():
    assert get_media_info({"target": {}}) == ("anime", "")


# ── Регрессии реальных прод-багов (были в test_media, сохранены как регрессии) ──

def test_regression_manga_detected_by_kind():
    """Исторический баг: манга определяется через kind, даже если type
    отсутствует (напр. ранобэ приходит без target.type)."""
    assert get_media_info({"target": {"kind": "ranobe"}}) == ("manga", "ranobe")


def test_regression_manga_status_uses_watching():
    """Исторический баг /status: Shikimori шлёт watching/rewatching и для аниме,
    и для манги — поэтому манга ДОЛЖНА определяться как manga по target.type,
    а не по статусу."""
    assert get_media_info({"target": {"type": "Manga", "kind": "manga"}}) == ("manga", "manga")


# ════════════════════════════════════════════════════════════════
#  Центральный троттл + ретрай на 429  (единый choke-point _fetch)
# ════════════════════════════════════════════════════════════════

class _SeqResponse:
    """Ответ с явными headers (для Retry-After) и json(), принимающим
    content_type= (годится и для gql/list_export)."""
    def __init__(self, status, *, json_value=None, headers=None):
        self.status = status
        self._json_value = json_value
        self.headers = headers or {}

    async def json(self, *args, **kwargs):
        return self._json_value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _SeqSession:
    """Отдаёт заранее заготовленную очередь ответов — по одному на выстрел."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self._responses.pop(0)

    def post(self, *args, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


# ── _throttle: держит min-gap (мутация: без sleep всплеск не тормозится) ──

@pytest.mark.asyncio
async def test_throttle_enforces_min_gap_on_burst(monkeypatch):
    monkeypatch.setattr(shiki_api, "_MIN_GAP", 0.25)
    shiki_api._throttle_lock = None
    shiki_api._last_request_at = 0.0

    clock = {"t": 1000.0}
    slept = []
    monkeypatch.setattr(shiki_api.time, "monotonic", lambda: clock["t"])

    async def fake_sleep(d):
        slept.append(d)
        clock["t"] += d

    monkeypatch.setattr(shiki_api.asyncio, "sleep", fake_sleep)

    # Первый выстрел: last=0, «сейчас» далеко → gap<0, не спим.
    await shiki_api._throttle()
    assert slept == []
    # Второй сразу за ним: часы не двигались → держим полный min-gap.
    await shiki_api._throttle()
    assert slept == [pytest.approx(0.25)]


# ── _fetch реально проходит через троттл (мутация: обход choke-point) ──

@pytest.mark.asyncio
async def test_fetch_goes_through_throttle(monkeypatch):
    calls = []

    async def fake_throttle():
        calls.append(1)

    monkeypatch.setattr(shiki_api, "_throttle", fake_throttle)
    session = _SeqSession([_SeqResponse(200, json_value=[])])
    await fetch_history(session)
    assert calls == [1]


# ── 429 → Retry-After → ретрай восстанавливается И возвращает данные ──

@pytest.mark.asyncio
async def test_fetch_retries_on_429_and_returns_data(monkeypatch):
    slept = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(shiki_api.asyncio, "sleep", fake_sleep)

    payload = [{"id": 1, "description": "оценено на 9"}]
    session = _SeqSession([
        _SeqResponse(429, headers={"Retry-After": "2"}),
        _SeqResponse(200, json_value=payload),
    ])
    result = await fetch_history(session)
    assert result == payload          # данные пришли на успешном ретрае
    assert session.calls == 2         # ровно одна доп. попытка
    assert slept == [pytest.approx(2.0)]  # уважили Retry-After (троттл спит 0)


@pytest.mark.asyncio
async def test_fetch_returns_none_when_429_exhausted(monkeypatch):
    async def fake_sleep(d):
        pass

    monkeypatch.setattr(shiki_api.asyncio, "sleep", fake_sleep)

    attempts = shiki_api._MAX_429_RETRIES + 1
    session = _SeqSession(
        [_SeqResponse(429, headers={"Retry-After": "0"}) for _ in range(attempts)]
    )
    assert await fetch_history(session) is None
    assert session.calls == attempts   # 1 исходный + _MAX_429_RETRIES ретраев


# ── _retry_after: парсинг Retry-After с фолбэками и клампом ──

def test_retry_after_parses_seconds():
    assert shiki_api._retry_after({"Retry-After": "3"}) == 3.0


def test_retry_after_defaults_when_missing_or_none():
    assert shiki_api._retry_after({}) == shiki_api._RETRY_AFTER_DEFAULT
    assert shiki_api._retry_after(None) == shiki_api._RETRY_AFTER_DEFAULT


def test_retry_after_defaults_on_http_date_form():
    # HTTP-date-форму не поддерживаем — фолбэк, а не падение.
    assert shiki_api._retry_after(
        {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    ) == shiki_api._RETRY_AFTER_DEFAULT


def test_retry_after_caps_absurd_values():
    assert shiki_api._retry_after({"Retry-After": "9999"}) == shiki_api._RETRY_AFTER_CAP


# ── Битое тело при 200: parse спотыкается (None/не-список) → None, не исключение ──

class _FakeSessionCM:
    """async-context-manager вокруг готовой сессии — под fetch_current_rates,
    которая открывает собственный aiohttp.ClientSession()."""
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [None, [1, 2]],
    ids=["none", "list"],
)
async def test_gql_request_survives_malformed_payload(payload):
    """200, но тело не той формы: `"errors" in payload` / `payload.get` роняют
    TypeError/AttributeError — _fetch обязан вернуть None, а не пробросить."""
    session = _SeqSession([_SeqResponse(200, json_value=payload)])
    assert await shiki_api._gql_request(session, "q", {}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [None, {"foo": "bar"}],
    ids=["none", "dict"],
)
async def test_fetch_current_rates_survives_malformed_payload(monkeypatch, payload):
    """200, но тело не итерируется как список записей (None) или даёт не те
    элементы (dict → ключи-строки) — обход `item["_status"]=...` роняет
    TypeError; ждём мягкий None."""
    session = _SeqSession([_SeqResponse(200, json_value=payload)])
    monkeypatch.setattr(
        shiki_api.aiohttp, "ClientSession",
        lambda *a, **k: _FakeSessionCM(session),
    )
    assert await shiki_api.fetch_current_rates("anime", ["watching"]) is None


@pytest.mark.asyncio
async def test_fetch_swallows_parse_structure_errors():
    """Прямой контракт _fetch: структурные ошибки parse (AttributeError и пр.)
    на 200 гасятся в None, не всплывают в вызывающий флоу."""
    async def bad_parse(resp):
        raise AttributeError("boom")

    session = _SeqSession([_SeqResponse(200, json_value={})])
    result = await shiki_api._fetch(
        session, "GET", "http://x", parse=bad_parse, label="t", timeout=5,
    )
    assert result is None
