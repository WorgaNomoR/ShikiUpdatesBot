# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты shiki_api.py — сетевые фетчи (успех/ошибки/таймауты), get_media_info и is_relevant.

Сетевую границу мокаем одной парой _FakeSession/_FakeResponse (без копипасты
в каждом тесте); is_relevant — чистая функция, не замокана.
"""

import asyncio
import json
import types
from unittest.mock import AsyncMock

import aiohttp
import pytest

import shiki_api
from request_budget import BudgetSnapshot
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

    async def json(self, *args, **kwargs):
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
        self.requests = []

    def get(self, *args, **kwargs):
        self.requests.append((args, kwargs))
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
async def test_fetch_history_returns_none_on_object_payload():
    resp = _FakeResponse(200, json_value={"id": 1})

    assert await fetch_history(_FakeSession(response=resp)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", ["2", True], ids=["string", "bool"])
async def test_fetch_history_returns_none_on_invalid_entry_id(invalid_id):
    payload = [{"id": 1}, {"id": invalid_id}]
    resp = _FakeResponse(200, json_value=payload)

    assert await fetch_history(_FakeSession(response=resp)) is None


@pytest.mark.asyncio
async def test_fetch_history_requests_explicit_page_with_fixed_limit():
    session = _FakeSession(response=_FakeResponse(200, json_value=[]))

    await fetch_history(session, page=3)

    args, _kwargs = session.requests[0]
    assert args[0].endswith("/history?limit=50&page=3")


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
    for kind in ("manga", "manhwa", "light_novel", "novel", "ranobe"):
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


@pytest.mark.parametrize("kind", ["light_novel", "novel", "ranobe"])
def test_get_media_info_ranobe_kind_is_manga(kind):
    """Вид ранобэ определяет домен манги даже без target.type."""
    assert get_media_info({"target": {"kind": kind}}) == ("manga", kind)


def test_get_media_info_fallback_to_anime():
    assert get_media_info({"target": {}}) == ("anime", "")


# ── Регрессии реальных прод-багов (были в test_media, сохранены как регрессии) ──

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
    """Отдаёт заранее заготовленную очередь ответов — по одному на выстрел.
    Пишет json-тела POST (requests) — чтобы проверять границы батчинга."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.requests = []

    def get(self, *args, **kwargs):
        self.calls += 1
        return self._responses.pop(0)

    def post(self, *args, **kwargs):
        self.calls += 1
        self.requests.append(kwargs.get("json"))
        return self._responses.pop(0)


# ── Точный privacy-сигнал: только 403 + точное поле message ──

_EXPECTED_PRIVATE_PROFILE_MESSAGE = "You are not authorized to access this page."


@pytest.mark.asyncio
async def test_fetch_classifies_exact_private_profile_response():
    response = _SeqResponse(
        403,
        json_value={
            "message": _EXPECTED_PRIVATE_PROFILE_MESSAGE,
            "code": 403,
        },
    )

    with pytest.raises(shiki_api.ProfilePrivacyError) as exc_info:
        await fetch_history(_SeqSession([response]))

    assert exc_info.value.endpoint == "fetch_history(page=1)"


@pytest.mark.asyncio
async def test_list_export_propagates_private_profile_response():
    response = _SeqResponse(
        403,
        json_value={
            "message": _EXPECTED_PRIVATE_PROFILE_MESSAGE,
            "code": 403,
        },
    )

    with pytest.raises(shiki_api.ProfilePrivacyError):
        await shiki_api.fetch_list_export(_SeqSession([response]), "anime")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"message": "You are not authorized to access this page"},
        {"message": "You are not authorized to access this page. "},
        {"message": "you are not authorized to access this page."},
        {"error": _EXPECTED_PRIVATE_PROFILE_MESSAGE},
        "You are not authorized to access this page.",
        None,
    ],
    ids=[
        "missing-period",
        "trailing-space",
        "different-case",
        "different-field",
        "plain-string",
        "null",
    ],
)
async def test_fetch_does_not_classify_near_private_profile_responses(payload):
    response = _SeqResponse(403, json_value=payload)

    assert await fetch_history(_SeqSession([response])) is None


@pytest.mark.asyncio
async def test_fetch_does_not_classify_malformed_403_body():
    response = _FakeResponse(
        403,
        json_exc=json.JSONDecodeError("bad", "", 0),
    )

    assert await fetch_history(_FakeSession(response=response)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 404, 500])
async def test_fetch_does_not_classify_private_message_on_other_status(status):
    response = _SeqResponse(
        status,
        json_value={
            "message": _EXPECTED_PRIVATE_PROFILE_MESSAGE,
            "code": 403,
        },
    )

    assert await fetch_history(_SeqSession([response])) is None


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

    async def fake_throttle(*, reserve, actor_user_id):
        calls.append((reserve, actor_user_id))
        return True

    monkeypatch.setattr(shiki_api, "_throttle", fake_throttle)
    session = _SeqSession([_SeqResponse(200, json_value=[])])
    await fetch_history(session)
    assert calls == [(0, None)]


@pytest.mark.asyncio
async def test_denied_budget_does_not_consume_an_extra_throttle_slot(
    monkeypatch,
):
    monkeypatch.setattr(shiki_api, "_MIN_GAP", 0.25)
    shiki_api._throttle_lock = None
    shiki_api._last_request_at = 1000.0
    clock = {"t": 1000.0}
    slept = []
    reservations = []
    decisions = iter([False, True])

    class Budget:
        def try_acquire(self, *, reserve=0, actor=None):
            reservations.append((reserve, actor, clock["t"]))
            return next(decisions)

    async def fake_sleep(delay):
        slept.append(delay)
        clock["t"] += delay

    monkeypatch.setattr(shiki_api, "_request_attempt_budget", Budget())
    monkeypatch.setattr(shiki_api.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(shiki_api.asyncio, "sleep", fake_sleep)

    assert await shiki_api._throttle(reserve=20, actor_user_id=777) is False
    assert shiki_api._last_request_at == 1000.0
    assert await shiki_api._throttle(reserve=0, actor_user_id=None) is True
    assert slept == [pytest.approx(0.25)]
    assert reservations == [
        (20, 777, pytest.approx(1000.25)),
        (0, None, pytest.approx(1000.25)),
    ]
    assert shiki_api._last_request_at == pytest.approx(1000.25)


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


@pytest.mark.asyncio
async def test_429_private_message_remains_rate_limit_failure(monkeypatch):
    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(shiki_api.asyncio, "sleep", fake_sleep)
    attempts = shiki_api._MAX_429_RETRIES + 1
    responses = [
        _SeqResponse(
            429,
            json_value={
                "message": _EXPECTED_PRIVATE_PROFILE_MESSAGE,
                "code": 403,
            },
            headers={"Retry-After": "0"},
        )
        for _ in range(attempts)
    ]

    session = _SeqSession(responses)

    assert await fetch_history(session) is None
    assert session.calls == attempts


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


# ── fetch_current_rates: мягкая деградация по статусам (watching/rewatching) ──

@pytest.mark.asyncio
async def test_fetch_current_rates_partial_on_single_status_failure(monkeypatch):
    """Один статус упал, другой ок → отдаём частичное (не None): один сбой не
    обнуляет весь /status."""
    watching = [{"id": 1, "anime": {"kind": "tv"}}]
    session = _SeqSession([
        _SeqResponse(200, json_value=watching),   # watching — ок
        _SeqResponse(500, json_value=None),       # rewatching — сбой (не-200, без ретрая)
    ])
    monkeypatch.setattr(
        shiki_api.aiohttp, "ClientSession",
        lambda *a, **k: _FakeSessionCM(session),
    )
    result = await shiki_api.fetch_current_rates("anime", ["watching", "rewatching"])
    assert result is not None
    assert [r["id"] for r in result] == [1]
    assert result[0]["_status"] == "watching"


@pytest.mark.asyncio
async def test_fetch_current_rates_none_only_when_all_statuses_fail(monkeypatch):
    """None — только при полном отказе (ни один статус не пришёл)."""
    session = _SeqSession([
        _SeqResponse(500, json_value=None),
        _SeqResponse(500, json_value=None),
    ])
    monkeypatch.setattr(
        shiki_api.aiohttp, "ClientSession",
        lambda *a, **k: _FakeSessionCM(session),
    )
    assert await shiki_api.fetch_current_rates("anime", ["watching", "rewatching"]) is None


@pytest.mark.asyncio
async def test_fetch_current_rates_private_failure_dominates_partial_success(monkeypatch):
    watching = [{"id": 1, "anime": {"kind": "tv"}}]
    session = _SeqSession([
        _SeqResponse(200, json_value=watching),
        _SeqResponse(
            403,
            json_value={
                "message": _EXPECTED_PRIVATE_PROFILE_MESSAGE,
                "code": 403,
            },
        ),
    ])
    monkeypatch.setattr(
        shiki_api.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSessionCM(session),
    )

    with pytest.raises(shiki_api.ProfilePrivacyError):
        await shiki_api.fetch_current_rates(
            "anime",
            ["watching", "rewatching"],
        )


# ════════════════════════════════════════════════════════════════
#  _parse_genres — фильтр по kind, предпочтение русского (чистая функция)
# ════════════════════════════════════════════════════════════════

def test_parse_genres_filters_by_kind_and_prefers_russian():
    raw = [
        {"russian": "Драма", "name": "Drama", "kind": "genre"},
        {"russian": "Психология", "name": "Psychological", "kind": "theme"},
        {"russian": "", "name": "Seinen", "kind": "demographic"},
    ]
    assert shiki_api._parse_genres(raw, "genre") == ["Драма"]
    assert shiki_api._parse_genres(raw, "theme") == ["Психология"]
    # russian пустой → фолбэк на name
    assert shiki_api._parse_genres(raw, "demographic") == ["Seinen"]


def test_parse_genres_skips_empty_names_and_non_dicts():
    raw = [
        {"russian": "", "name": "", "kind": "genre"},   # оба пустые → пропуск
        "не словарь",                                    # не dict → пропуск
        {"russian": "Экшен", "name": "Action", "kind": "genre"},
    ]
    assert shiki_api._parse_genres(raw, "genre") == ["Экшен"]


def test_parse_genres_empty_input_is_empty_list():
    assert shiki_api._parse_genres(None, "genre") == []
    assert shiki_api._parse_genres([], "genre") == []


# ════════════════════════════════════════════════════════════════
#  _gql_request — успех и частичные данные (data + errors) через HTTP-границу
# ════════════════════════════════════════════════════════════════

def _gql_response(data, *, errors=None):
    payload = {"data": data}
    if errors is not None:
        payload["errors"] = errors
    return _SeqResponse(200, json_value=payload)


@pytest.mark.asyncio
async def test_gql_request_returns_data_field():
    session = _SeqSession([_gql_response({"animes": [{"id": "1"}]})])
    data = await shiki_api._gql_request(session, "q", {"ids": "1"})
    assert data == {"animes": [{"id": "1"}]}


@pytest.mark.asyncio
async def test_gql_request_returns_partial_data_despite_errors():
    """Частичные данные (data + errors) отдаём как есть — решает caller.
    Ветка логирования errors при этом отрабатывает (не роняет)."""
    session = _SeqSession([
        _gql_response({"animes": []}, errors=[{"message": "boom"}]),
    ])
    data = await shiki_api._gql_request(session, "q", {"ids": "1"})
    assert data == {"animes": []}


@pytest.mark.parametrize(
    ("media_type", "field", "kind_filter"),
    [
        ("anime", "animes", "tv,movie,ova,ona,special,tv_special"),
        ("manga", "mangas", "manga,manhwa,manhua,one_shot,doujin"),
        ("ranobe", "mangas", "light_novel,novel"),
    ],
)
@pytest.mark.asyncio
async def test_inline_search_uses_exact_graphql_domain_kind_and_single_page_request(
    monkeypatch,
    media_type,
    field,
    kind_filter,
):
    gql = AsyncMock(return_value={field: [{"id": "1"}]})
    monkeypatch.setattr(shiki_api, "_gql_request", gql)

    result = await shiki_api.fetch_inline_search(
        media_type,
        "Berserk",
        3,
        session=object(),
    )

    assert result == [{"id": "1"}]
    gql.assert_awaited_once()
    query, variables = gql.await_args.args[1:]
    assert f"{field}(" in query
    assert "limit: 49" in query
    assert "censored: false" in query
    assert "poster { originalUrl mainUrl }" in query
    assert "genres { russian name kind }" in query
    if media_type == "anime":
        assert "rating" in query
        assert "origin" in query
    assert variables == {"search": "Berserk", "page": 3}
    assert gql.await_args.kwargs == {
        "traffic_class": "inline",
        "allow_partial": False,
        "actor_user_id": None,
    }
    assert f'kind: "{kind_filter}"' in query


@pytest.mark.asyncio
async def test_inline_anime_translates_origin_rating_and_omits_unknown_values(
    monkeypatch,
):
    gql = AsyncMock(return_value={
        "animes": [
            {"id": "1", "origin": "visual_novel", "rating": "r_plus"},
            {"id": "2", "origin": "unknown", "rating": "none"},
        ]
    })
    monkeypatch.setattr(shiki_api, "_gql_request", gql)

    result = await shiki_api.fetch_inline_search(
        "anime",
        "Fate",
        1,
        session=object(),
    )

    assert result == [
        {"id": "1", "origin": "Визуальная новелла", "rating": "R+"},
        {"id": "2"},
    ]


@pytest.mark.asyncio
async def test_inline_search_rejects_unknown_media_without_network(monkeypatch):
    gql = AsyncMock()
    monkeypatch.setattr(shiki_api, "_gql_request", gql)

    assert await shiki_api.fetch_inline_search(
        "game",
        "Berserk",
        1,
        session=object(),
    ) is None
    gql.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_search_does_not_cache_shape_from_graphql_error():
    session = _SeqSession([
        _gql_response(
            {"animes": [{"id": "partial"}]},
            errors=[{"message": "partial failure"}],
        )
    ])

    assert await shiki_api.fetch_inline_search(
        "anime",
        "Berserk",
        1,
        session=session,
    ) is None
    assert session.calls == 1


@pytest.mark.asyncio
async def test_global_attempt_budget_counts_429_retry_and_reserves_inline_headroom(
    monkeypatch,
):
    reservations = []

    class Budget:
        def try_acquire(self, *, reserve=0, actor=None):
            reservations.append((reserve, actor))
            return True

    monkeypatch.setattr(shiki_api, "_request_attempt_budget", Budget())

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(shiki_api.asyncio, "sleep", no_sleep)
    session = _SeqSession([
        _SeqResponse(429, headers={"Retry-After": "0"}),
        _gql_response({"animes": []}),
    ])

    data = await shiki_api._gql_request(
        session,
        "query { animes { id } }",
        {},
        traffic_class="inline",
        actor_user_id=777,
    )

    assert data == {"animes": []}
    assert session.calls == 2
    assert reservations == [
        (shiki_api._INLINE_ATTEMPT_RESERVE, 777),
        (shiki_api._INLINE_ATTEMPT_RESERVE, 777),
    ]


@pytest.mark.asyncio
async def test_exhausted_global_budget_stops_before_http(monkeypatch):
    class Budget:
        def try_acquire(self, *, reserve=0, actor=None):
            return False

        def snapshot(self, *, reserve=0):
            return BudgetSnapshot(80, 80 - reserve, 12.0, None, ())

    monkeypatch.setattr(shiki_api, "_request_attempt_budget", Budget())
    session = _SeqSession([_SeqResponse(200, json_value=[])])

    assert await fetch_history(session) is None
    assert session.calls == 0
    assert shiki_api._last_request_at == 0.0


@pytest.mark.asyncio
async def test_exhausted_inline_attempt_reserve_raises_attributed_typed_failure(
    monkeypatch,
):
    class Budget:
        def try_acquire(self, *, reserve=0, actor=None):
            assert reserve == shiki_api._INLINE_ATTEMPT_RESERVE
            assert actor == 777
            return False

        def snapshot(self, *, reserve=0):
            return BudgetSnapshot(
                used=60,
                capacity=60,
                retry_after=17.5,
                last_actor=777,
                actor_counts=((777, 12),),
            )

    monkeypatch.setattr(shiki_api, "_request_attempt_budget", Budget())
    session = _SeqSession([_gql_response({"animes": []})])

    with pytest.raises(shiki_api.ShikimoriBudgetExceeded) as exc_info:
        await shiki_api._gql_request(
            session,
            "query { animes { id } }",
            {},
            traffic_class="inline",
            actor_user_id=777,
        )

    assert exc_info.value.snapshot.retry_after == 17.5
    assert exc_info.value.snapshot.actor_counts == ((777, 12),)
    assert session.calls == 0


# ════════════════════════════════════════════════════════════════
#  fetch_list_export — публичный экспорт списка (успех / не-список / URL)
# ════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_fetch_list_export_returns_list_on_success():
    payload = [{"target_id": 1, "score": 9, "status": "completed"}]
    session = _SeqSession([_SeqResponse(200, json_value=payload)])
    assert await shiki_api.fetch_list_export(session, "anime") == payload


@pytest.mark.asyncio
async def test_fetch_list_export_none_when_not_a_list():
    """200, но тело не список (dict) → мягкий None, не исключение."""
    session = _SeqSession([_SeqResponse(200, json_value={"oops": True})])
    assert await shiki_api.fetch_list_export(session, "anime") is None


class _UrlRecordingSession:
    """Пишет URL каждого GET — проверяем выбор anime/manga эндпоинта."""
    def __init__(self, response):
        self._response = response
        self.urls = []

    def get(self, url, *args, **kwargs):
        self.urls.append(url)
        return self._response


@pytest.mark.asyncio
async def test_fetch_list_export_picks_media_specific_url():
    anime_sess = _UrlRecordingSession(_SeqResponse(200, json_value=[]))
    await shiki_api.fetch_list_export(anime_sess, "anime")
    assert anime_sess.urls == [shiki_api.LIST_EXPORT_ANIME]

    manga_sess = _UrlRecordingSession(_SeqResponse(200, json_value=[]))
    await shiki_api.fetch_list_export(manga_sess, "manga")
    assert manga_sess.urls == [shiki_api.LIST_EXPORT_MANGA]


# ════════════════════════════════════════════════════════════════
#  fetch_meta_batch — батчинг + парсинг meta через HTTP-границу
# ════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_fetch_meta_batch_empty_ids_returns_empty_without_network():
    # session=None + пустые ids → возврат до открытия сессии (сети не касаемся)
    assert await shiki_api.fetch_meta_batch("anime", []) == {}
    assert await shiki_api.fetch_meta_batch("anime", ["", "  "]) == {}


@pytest.mark.asyncio
async def test_fetch_meta_batch_parses_full_anime_meta():
    item = {
        "id": 226,
        "url": "https://shikimori.one/animes/226-elfen-lied",
        "kind": "TV",
        "score": "8.5",
        "rating": "r",
        "origin": "manga",
        "duration": 25,
        "episodes": 13,
        "airedOn": {"year": 2004},
        "studios": [{"name": "Arms"}, {"noname": 1}],
        "genres": [
            {"russian": "Драма", "name": "Drama", "kind": "genre"},
            {"russian": "Психология", "name": "Psychological", "kind": "theme"},
            {"russian": "", "name": "Seinen", "kind": "demographic"},
        ],
    }
    session = _SeqSession([_gql_response({"animes": [item]})])
    result = await shiki_api.fetch_meta_batch("anime", ["226"], session=session)

    assert set(result) == {"226"}
    meta = result["226"]
    assert meta["url"] == shiki_api._rel_url(item["url"])   # нормализован к относительному
    assert meta["kind"] == "tv"                              # приведён к нижнему регистру
    assert meta["year"] == 2004
    assert meta["shiki_score"] == 8.5
    assert meta["genres"] == ["Драма"]
    assert meta["themes"] == ["Психология"]
    assert meta["demographic"] == ["Seinen"]
    assert meta["duration"] == 25
    assert meta["episodes_total"] == 13
    assert meta["rating"] == "R-17"                          # RU-маппинг рейтинга
    assert meta["origin"] == "Манга"                         # RU-маппинг origin
    assert meta["studios"] == ["Arms"]                       # студия без name отброшена


@pytest.mark.asyncio
async def test_fetch_meta_batch_parses_manga_meta():
    item = {
        "id": 1,
        "url": "/mangas/1-berserk",
        "kind": "Manga",
        "score": "9.1",
        "chapters": 100,
        "volumes": 12,
        "airedOn": {"year": 1989},
        "publishers": [{"name": "Hakusensha"}, {"nope": 1}],
        "genres": [],
    }
    session = _SeqSession([_gql_response({"mangas": [item]})])
    meta = (await shiki_api.fetch_meta_batch("manga", ["1"], session=session))["1"]

    assert meta["chapters_total"] == 100
    assert meta["volumes_total"] == 12
    assert meta["publishers"] == ["Hakusensha"]
    # манга-ветка НЕ добавляет аниме-специфичные поля
    assert "rating" not in meta and "origin" not in meta and "studios" not in meta


@pytest.mark.asyncio
async def test_fetch_meta_batch_splits_into_batches_of_50():
    """>50 id → несколько GraphQL-запросов; данные всех батчей склеиваются."""
    ids = [str(i) for i in range(60)]
    session = _SeqSession([
        _gql_response({"animes": [{"id": "1000", "genres": []}]}),
        _gql_response({"animes": [{"id": "2000", "genres": []}]}),
    ])
    result = await shiki_api.fetch_meta_batch("anime", ids, session=session)
    assert session.calls == 2                       # ровно два батча (50 + 10)
    # реальные размеры чанков, а не только их число: ловит сдвиг границы
    # (напр. step=30 тоже дал бы 2 вызова, но батчи [30, 30], а не [50, 10])
    sizes = [len(r["variables"]["ids"].split(",")) for r in session.requests]
    assert sizes == [50, 10]
    assert set(result) == {"1000", "2000"}          # данные обоих батчей сохранены


@pytest.mark.asyncio
async def test_fetch_meta_batch_keeps_partial_on_batch_failure():
    """Сбой одного батча (не-200 → _gql_request=None) не обнуляет остальные."""
    ids = [str(i) for i in range(60)]
    session = _SeqSession([
        _SeqResponse(500, json_value=None),                        # первый батч — сбой
        _gql_response({"animes": [{"id": "2000", "genres": []}]}),  # второй — ок
    ])
    result = await shiki_api.fetch_meta_batch("anime", ids, session=session)
    assert session.calls == 2
    assert set(result) == {"2000"}                  # уцелевший батч на месте


@pytest.mark.asyncio
async def test_fetch_meta_batch_skips_items_without_id_and_broken_items():
    """Элемент без id пропускается; элемент, роняющий парсинг, гасится
    per-item (warning) — остальные из того же батча выживают."""
    good = {"id": 226, "genres": [], "studios": [{"name": "OK"}]}
    no_id = {"kind": "tv", "genres": []}                            # нет id → skip
    broken = {"id": 999, "genres": [], "studios": ["не словарь"]}   # .get на строке → AttributeError
    session = _SeqSession([_gql_response({"animes": [good, no_id, broken]})])
    result = await shiki_api.fetch_meta_batch("anime", ["226", "999"], session=session)
    assert set(result) == {"226"}                   # только исправный элемент


@pytest.mark.asyncio
async def test_fetch_meta_batch_opens_own_session_when_none(monkeypatch):
    """session=None → открывает собственную ClientSession (boot-throttle-путь)."""
    session = _SeqSession([_gql_response({"animes": [{"id": "7", "genres": []}]})])
    monkeypatch.setattr(
        shiki_api.aiohttp, "ClientSession",
        lambda *a, **k: _FakeSessionCM(session),
    )
    result = await shiki_api.fetch_meta_batch("anime", ["7"])   # без session
    assert set(result) == {"7"}


def test_retry_after_defaults_on_negative_value():
    # Отрицательный Retry-After бессмыслен -> фолбэк на дефолт, не отрицательный сон
    assert shiki_api._retry_after({"Retry-After": "-5"}) == shiki_api._RETRY_AFTER_DEFAULT
