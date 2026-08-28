# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Оркестрация доступа и обработки InlineQuery."""

from types import SimpleNamespace
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest

import handlers
from inline_search import (
    InlineSearchLimitExceeded,
    SearchPage,
)


def _inline_query(*, user_id=777, query="anime Berserk", offset=""):
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=user_id,
            full_name="Morpheus",
            username="neo",
        ),
        query=query,
        offset=offset,
        answer=AsyncMock(),
    )


def _service(*, page=None, continuation_page=2):
    return SimpleNamespace(
        invalidate_debounce=MagicMock(),
        debounce=AsyncMock(return_value=1),
        is_current=MagicMock(return_value=True),
        resolve_continuation=MagicMock(return_value=continuation_page),
        get_page=AsyncMock(return_value=page),
        issue_continuation=MagicMock(return_value="next-token"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "has_button"),
    [
        (handlers.INLINE_ACCESS_BLOCKED, False),
        ("unsubscribed", True),
    ],
)
async def test_rejected_users_stop_before_parser_debounce_cache_and_network(
    monkeypatch,
    status,
    has_button,
):
    inline_query = _inline_query()
    service = _service()
    parser = MagicMock()
    monkeypatch.setattr(handlers, "inline_access_status", lambda _user_id: status)
    monkeypatch.setattr(handlers, "parse_inline_query", parser)
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(inline_query)

    parser.assert_not_called()
    service.debounce.assert_not_awaited()
    service.get_page.assert_not_awaited()
    kwargs = inline_query.answer.await_args.kwargs
    assert kwargs["cache_time"] == 0
    assert ("button" in kwargs) is has_button
    if has_button:
        assert kwargs["button"].start_parameter == "inline_search"


@pytest.mark.asyncio
async def test_invalid_or_too_short_query_invalidates_old_timer_without_new_timer(
    monkeypatch,
):
    inline_query = _inline_query(query="anime x")
    service = _service()
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        lambda _user_id: handlers.INLINE_ACCESS_ALLOWED,
    )
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(inline_query)

    service.invalidate_debounce.assert_called_once_with(777)
    service.debounce.assert_not_awaited()
    service.get_page.assert_not_awaited()
    inline_query.answer.assert_awaited_once_with([], cache_time=0)


@pytest.mark.asyncio
async def test_valid_offset_skips_debounce_and_fetches_issued_page(monkeypatch):
    inline_query = _inline_query(offset="issued")
    page = SearchPage(items=({"id": "1"},), expires_at=100.0)
    service = _service(page=page, continuation_page=2)
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        lambda _user_id: handlers.INLINE_ACCESS_ALLOWED,
    )
    monkeypatch.setattr(handlers, "_inline_search_service", service)
    monkeypatch.setattr(handlers, "build_inline_result", lambda media, item: item)

    await handlers.cmd_inline_search(inline_query)

    service.resolve_continuation.assert_called_once()
    service.debounce.assert_not_awaited()
    assert service.get_page.await_args.args[1] == 2
    inline_query.answer.assert_awaited_once_with(
        [{"id": "1"}],
        cache_time=0,
        next_offset="",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", ["malformed", "stale", "cross", "skipped", "unissued"])
async def test_invalid_offsets_are_immediate_and_touch_no_search_state(
    monkeypatch,
    offset,
):
    inline_query = _inline_query(offset=offset)
    service = _service(continuation_page=None)
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        lambda _user_id: handlers.INLINE_ACCESS_ALLOWED,
    )
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(inline_query)

    service.resolve_continuation.assert_called_once()
    assert service.resolve_continuation.call_args.args[1] == offset
    service.invalidate_debounce.assert_not_called()
    service.debounce.assert_not_awaited()
    service.get_page.assert_not_awaited()
    service.issue_continuation.assert_not_called()
    inline_query.answer.assert_awaited_once_with([], cache_time=0)


@pytest.mark.asyncio
async def test_offset_with_invalid_query_does_not_even_read_or_invalidate_state(
    monkeypatch,
):
    inline_query = _inline_query(query="anime x", offset="malformed")
    service = _service()
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        lambda _user_id: handlers.INLINE_ACCESS_ALLOWED,
    )
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(inline_query)

    service.resolve_continuation.assert_not_called()
    service.invalidate_debounce.assert_not_called()
    service.debounce.assert_not_awaited()
    service.get_page.assert_not_awaited()
    inline_query.answer.assert_awaited_once_with([], cache_time=0)


@pytest.mark.asyncio
async def test_full_page_issues_next_offset_but_short_page_does_not(monkeypatch):
    inline_query = _inline_query()
    items = tuple({"id": str(index)} for index in range(50))
    service = _service(page=SearchPage(items=items, expires_at=500.0))
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        lambda _user_id: handlers.INLINE_ACCESS_ALLOWED,
    )
    monkeypatch.setattr(handlers, "_inline_search_service", service)
    monkeypatch.setattr(handlers, "build_inline_result", lambda media, item: item)

    await handlers.cmd_inline_search(inline_query)

    service.issue_continuation.assert_called_once()
    assert service.issue_continuation.call_args.kwargs == {
        "page": 2,
        "preceding_expires_at": 500.0,
    }
    assert inline_query.answer.await_args.kwargs == {
        "cache_time": 0,
        "next_offset": "next-token",
    }
    assert len(inline_query.answer.await_args.args[0]) == 50


@pytest.mark.asyncio
async def test_unsubscribe_during_debounce_stops_before_cache_and_network(monkeypatch):
    inline_query = _inline_query()
    service = _service(page=SearchPage(items=(), expires_at=100.0))
    statuses = iter([
        handlers.INLINE_ACCESS_ALLOWED,
        "unsubscribed",
    ])
    monkeypatch.setattr(handlers, "inline_access_status", lambda _user_id: next(statuses))
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(inline_query)

    service.debounce.assert_awaited_once_with(777)
    service.get_page.assert_not_awaited()
    assert inline_query.answer.await_args.kwargs["button"].start_parameter == (
        "inline_search"
    )


@pytest.mark.asyncio
async def test_api_failure_returns_empty_and_no_continuation(monkeypatch):
    inline_query = _inline_query()
    service = _service(page=None)
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        lambda _user_id: handlers.INLINE_ACCESS_ALLOWED,
    )
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(inline_query)

    service.issue_continuation.assert_not_called()
    inline_query.answer.assert_awaited_once_with([], cache_time=0)


@pytest.mark.asyncio
async def test_budget_failure_shows_timed_shikimori_button(monkeypatch):
    inline_query = _inline_query()
    service = _service()
    service.get_page.side_effect = InlineSearchLimitExceeded(23.2)
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        lambda _user_id: handlers.INLINE_ACCESS_ALLOWED,
    )
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(inline_query)

    actor = service.get_page.await_args.kwargs["actor"]
    assert (actor.user_id, actor.full_name, actor.username) == (
        777,
        "Morpheus",
        "neo",
    )
    service.issue_continuation.assert_not_called()
    kwargs = inline_query.answer.await_args.kwargs
    assert kwargs["cache_time"] == 0
    assert kwargs["button"].text == (
        "⏳ Лимит Shikimori: повторите через 24 с"
    )
    assert kwargs["button"].start_parameter == "inline_search_limit"
