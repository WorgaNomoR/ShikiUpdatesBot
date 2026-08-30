# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Оркестрация доступа и обработки InlineQuery."""

from types import SimpleNamespace
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.exceptions import TelegramBadRequest

import handlers
from inline_search import (
    InlineSearchLimitExceeded,
    SearchPage,
)


def _inline_query(
    *,
    user_id=777,
    query="anime Berserk",
    offset="",
    query_id="inline-query-1",
):
    return SimpleNamespace(
        id=query_id,
        from_user=SimpleNamespace(
            id=user_id,
            full_name="Morpheus",
            username="neo",
        ),
        query=query,
        offset=offset,
        bot=SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(username="WorgaTestBot")),
        ),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["fact", " FACT ", "факт", "\nФАКТ\t"])
@pytest.mark.parametrize("user_id", [777, handlers.OWNER_ID])
async def test_public_fact_query_is_stable_uncached_and_bypasses_media_flow(
    monkeypatch,
    query,
    user_id,
):
    first = _inline_query(user_id=user_id, query=query, query_id="retry-id")
    retry = _inline_query(user_id=user_id, query=query, query_id="retry-id")
    entitlement = MagicMock(side_effect=AssertionError("прочитана подписка"))
    parser = MagicMock(side_effect=AssertionError("запущен media parser"))
    service = _service()
    monkeypatch.setattr(handlers, "inline_access_status", entitlement)
    monkeypatch.setattr(handlers, "parse_inline_query", parser)
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(first)
    await handlers.cmd_inline_search(retry)

    entitlement.assert_not_called()
    parser.assert_not_called()
    first.bot.me.assert_not_awaited()
    retry.bot.me.assert_not_awaited()
    service.invalidate_debounce.assert_not_called()
    service.debounce.assert_not_awaited()
    service.resolve_continuation.assert_not_called()
    service.get_page.assert_not_awaited()
    service.issue_continuation.assert_not_called()
    first_call = first.answer.await_args
    retry_call = retry.answer.await_args
    assert first_call.kwargs == {"cache_time": 0}
    assert retry_call.kwargs == {"cache_time": 0}
    first_result = first_call.args[0][0]
    retry_result = retry_call.args[0][0]
    assert first_result == retry_result
    assert first_result.title == "💡 Отправить интересный факт"
    assert first_result.description == (
        "При выборе отправит в чат факт об аниме или Японии"
    )
    assert first_result.reply_markup is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "facts",
        "fact anime",
        "fact!",
        "fact:forged",
        "f a c t",
        "факты",
        "факт аниме",
        "факт?",
        "ф а к т",
    ],
)
async def test_fact_like_rejections_do_not_fall_through_to_entitlement_or_search(
    monkeypatch,
    query,
):
    inline_query = _inline_query(query=query)
    entitlement = MagicMock(side_effect=AssertionError("прочитана подписка"))
    parser = MagicMock(side_effect=AssertionError("запущен media parser"))
    service = _service()
    monkeypatch.setattr(handlers, "inline_access_status", entitlement)
    monkeypatch.setattr(handlers, "parse_inline_query", parser)
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(inline_query)

    inline_query.answer.assert_awaited_once_with([], cache_time=0)
    entitlement.assert_not_called()
    parser.assert_not_called()
    service.invalidate_debounce.assert_not_called()
    service.debounce.assert_not_awaited()
    service.resolve_continuation.assert_not_called()
    service.get_page.assert_not_awaited()
    service.issue_continuation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [777, handlers.OWNER_ID])
async def test_share_query_sends_exact_displayed_fact_without_random_selection(
    monkeypatch,
    user_id,
):
    expected = handlers.select_next_fact("anime-word")
    inline_query = _inline_query(
        user_id=user_id,
        query=f"fact:{expected.id}",
    )
    random_selection = MagicMock(side_effect=AssertionError("выбран другой факт"))
    entitlement = MagicMock(side_effect=AssertionError("прочитана подписка"))
    parser = MagicMock(side_effect=AssertionError("запущен media parser"))
    service = _service()
    monkeypatch.setattr(handlers, "select_fact", random_selection)
    monkeypatch.setattr(handlers, "inline_access_status", entitlement)
    monkeypatch.setattr(handlers, "parse_inline_query", parser)
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(inline_query)

    inline_query.answer.assert_awaited_once()
    results = inline_query.answer.await_args.args[0]
    assert len(results) == 1
    assert expected.text in results[0].input_message_content.message_text
    assert inline_query.answer.await_args.kwargs == {"cache_time": 0}
    random_selection.assert_not_called()
    entitlement.assert_not_called()
    parser.assert_not_called()
    service.debounce.assert_not_awaited()
    service.get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_fact_query_with_offset_is_rejected_before_selection_or_media_state(
    monkeypatch,
):
    inline_query = _inline_query(query="fact", offset="forged")
    selector = MagicMock()
    service = _service()
    monkeypatch.setattr(handlers, "select_fact", selector)
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(inline_query)

    inline_query.answer.assert_awaited_once_with([], cache_time=0)
    selector.assert_not_called()
    service.resolve_continuation.assert_not_called()
    service.get_page.assert_not_awaited()


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
    inline_query.bot.me.assert_not_awaited()
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
    builder = MagicMock(side_effect=lambda media, item, **_kwargs: item)
    finalizer = MagicMock(side_effect=lambda rendered, **_kwargs: rendered)
    monkeypatch.setattr(
        handlers,
        "build_inline_result",
        builder,
    )
    monkeypatch.setattr(handlers, "finalize_inline_results", finalizer)

    await handlers.cmd_inline_search(inline_query)

    service.resolve_continuation.assert_called_once()
    service.debounce.assert_not_awaited()
    assert service.get_page.await_args.args[1] == 2
    inline_query.bot.me.assert_awaited_once_with()
    builder.assert_called_once_with(
        "anime",
        {"id": "1"},
        bot_username="WorgaTestBot",
    )
    finalizer.assert_called_once_with(
        [{"id": "1"}],
        page=2,
        fact_seed="777\0anime\0berserk",
    )
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
async def test_full_49_item_page_adds_promo_and_issues_next_offset(monkeypatch):
    inline_query = _inline_query()
    items = tuple({"id": str(index)} for index in range(49))
    service = _service(page=SearchPage(items=items, expires_at=500.0))
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        lambda _user_id: handlers.INLINE_ACCESS_ALLOWED,
    )
    monkeypatch.setattr(handlers, "_inline_search_service", service)
    monkeypatch.setattr(
        handlers,
        "build_inline_result",
        lambda media, item, **_kwargs: item,
    )

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
    results = inline_query.answer.await_args.args[0]
    assert len(results) == 50
    assert results[-1].id == "project:share"


@pytest.mark.asyncio
async def test_short_first_page_adds_promo_without_next_offset(monkeypatch):
    inline_query = _inline_query()
    items = tuple({"id": str(index)} for index in range(48))
    service = _service(page=SearchPage(items=items, expires_at=500.0))
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        lambda _user_id: handlers.INLINE_ACCESS_ALLOWED,
    )
    monkeypatch.setattr(handlers, "_inline_search_service", service)
    monkeypatch.setattr(
        handlers,
        "build_inline_result",
        lambda media, item, **_kwargs: item,
    )

    await handlers.cmd_inline_search(inline_query)

    service.issue_continuation.assert_not_called()
    results = inline_query.answer.await_args.args[0]
    assert len(results) == 49
    assert [result["id"] for result in results[:-1]] == [
        str(index) for index in range(48)
    ]
    assert results[-1].id == "project:share"
    assert inline_query.answer.await_args.kwargs == {
        "cache_time": 0,
        "next_offset": "",
    }


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
async def test_telegram_rejection_logs_description_before_safe_empty_answer(
    monkeypatch,
    caplog,
):
    inline_query = _inline_query()
    inline_query.answer.side_effect = [
        TelegramBadRequest(
            method=MagicMock(),
            message='Bad Request: can\'t parse entities: Unsupported start tag "br"',
        ),
        None,
    ]
    service = _service(page=SearchPage(items=(), expires_at=100.0))
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        lambda _user_id: handlers.INLINE_ACCESS_ALLOWED,
    )
    monkeypatch.setattr(handlers, "_inline_search_service", service)

    await handlers.cmd_inline_search(inline_query)

    assert inline_query.answer.await_count == 2
    inline_query.answer.assert_awaited_with([], cache_time=0)
    assert "Telegram отклонил результаты" in caplog.text
    assert 'Unsupported start tag "br"' in caplog.text


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
