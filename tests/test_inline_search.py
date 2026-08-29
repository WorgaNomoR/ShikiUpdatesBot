# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Разбор, задержка ввода, кеш, схлопывание, бюджет и продолжения."""

import asyncio
import logging

import pytest

from inline_search import (
    CACHE_TTL_SECONDS,
    DEBOUNCE_SECONDS,
    INLINE_PAGE_LIMIT,
    InlineActor,
    InlineSearchLimitExceeded,
    InlineSearchService,
    parse_inline_query,
)
from request_budget import (
    BudgetSnapshot,
    RollingBudget,
)
from shiki_api import ShikimoriBudgetExceeded

_ACTOR = InlineActor(user_id=101, full_name="Alice", username="alice")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" anime   Steins;Gate ", ("anime", "Steins;Gate", "steins;gate")),
        ("АНИМЕ Берсерк", ("anime", "Берсерк", "берсерк")),
        ("a Steins;Gate", ("anime", "Steins;Gate", "steins;gate")),
        ("А   Берсерк", ("anime", "Берсерк", "берсерк")),
        ("manga  Higurashi no Naku Koro ni", ("manga", "Higurashi no Naku Koro ni", "higurashi no naku koro ni")),
        ("МАНГА   Когда плачут цикады", ("manga", "Когда плачут цикады", "когда плачут цикады")),
        ("M Higurashi", ("manga", "Higurashi", "higurashi")),
        ("м Higurashi", ("manga", "Higurashi", "higurashi")),
        ("ranobe Re:Zero", ("ranobe", "Re:Zero", "re:zero")),
        ("РАНОБЭ  Меланхолия Харухи", ("ranobe", "Меланхолия Харухи", "меланхолия харухи")),
        ("r Re:Zero", ("ranobe", "Re:Zero", "re:zero")),
        ("Р Re:Zero", ("ranobe", "Re:Zero", "re:zero")),
    ],
)
def test_parser_normalizes_prefix_case_and_whitespace(raw, expected):
    parsed = parse_inline_query(raw)

    assert parsed is not None
    assert (parsed.media_type, parsed.title, parsed.normalized_title) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "anime",
        "anime   ",
        "anime a",
        "a x",
        "а я",
        "m x",
        "м x",
        "r x",
        "р я",
        "манга я",
        "game Berserk",
        "Berserk",
    ],
)
def test_parser_rejects_missing_prefix_title_and_one_character(raw):
    assert parse_inline_query(raw) is None


@pytest.mark.asyncio
async def test_debounce_cancels_stale_user_timer_and_keeps_latest_only():
    sleepers = []

    async def controlled_sleep(delay):
        future = asyncio.get_running_loop().create_future()
        sleepers.append((delay, future))
        await future

    service = InlineSearchService(sleep=controlled_sleep)
    first = asyncio.create_task(service.debounce(7))
    await asyncio.sleep(0)
    second = asyncio.create_task(service.debounce(7))
    await asyncio.sleep(0)

    assert await first is None
    assert [delay for delay, _future in sleepers] == [
        DEBOUNCE_SECONDS,
        DEBOUNCE_SECONDS,
    ]
    sleepers[-1][1].set_result(None)
    generation = await second
    assert generation is not None
    assert service.is_current(7, generation)


@pytest.mark.asyncio
async def test_invalidation_cancels_timer_without_starting_another_one():
    calls = []
    started = asyncio.Event()

    async def sleeping(delay):
        calls.append(delay)
        started.set()
        await asyncio.Future()

    service = InlineSearchService(sleep=sleeping)
    pending = asyncio.create_task(service.debounce(9))
    await started.wait()

    service.invalidate_debounce(9)

    assert await pending is None
    assert calls == [DEBOUNCE_SECONDS]


@pytest.mark.asyncio
async def test_cache_normalizes_case_obeys_ttl_and_caches_empty_success():
    clock = {"now": 100.0}
    calls = []

    async def fetcher(media_type, title, page, _actor):
        calls.append((media_type, title, page))
        return []

    service = InlineSearchService(clock=lambda: clock["now"], fetcher=fetcher)
    first = parse_inline_query("anime Berserk")
    same = parse_inline_query("АНИМЕ berserk")
    assert first is not None and same is not None

    page1 = await service.get_page(
        first,
        1,
        authorized=lambda: True,
        actor=_ACTOR,
    )
    clock["now"] += CACHE_TTL_SECONDS - 1
    cached = await service.get_page(
        same,
        1,
        authorized=lambda: True,
        actor=_ACTOR,
    )
    clock["now"] += 1
    expired = await service.get_page(
        same,
        1,
        authorized=lambda: True,
        actor=_ACTOR,
    )

    assert page1 is cached
    assert expired is not cached
    assert calls == [("anime", "Berserk", 1), ("anime", "berserk", 1)]


@pytest.mark.asyncio
async def test_failed_page_is_not_cached():
    responses = [None, [{"id": "1"}]]
    calls = 0

    async def fetcher(_media_type, _title, _page, _actor):
        nonlocal calls
        calls += 1
        return responses.pop(0)

    service = InlineSearchService(fetcher=fetcher)
    query = parse_inline_query("manga Berserk")
    assert query is not None

    assert await service.get_page(
        query,
        1,
        authorized=lambda: True,
        actor=_ACTOR,
    ) is None
    assert await service.get_page(
        query,
        1,
        authorized=lambda: True,
        actor=_ACTOR,
    ) is not None
    assert calls == 2


@pytest.mark.asyncio
async def test_concurrent_identical_misses_coalesce_into_one_fetch():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetcher(_media_type, _title, _page, _actor):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return [{"id": "33"}]

    service = InlineSearchService(fetcher=fetcher)
    query = parse_inline_query("anime Berserk")
    assert query is not None
    first = asyncio.create_task(
        service.get_page(query, 1, authorized=lambda: True, actor=_ACTOR)
    )
    second = asyncio.create_task(
        service.get_page(query, 1, authorized=lambda: True, actor=_ACTOR)
    )
    await started.wait()
    release.set()

    page1, page2 = await asyncio.gather(first, second)
    assert page1 is page2
    assert calls == 1


@pytest.mark.asyncio
async def test_inline_page_budget_counts_uncached_pages_only():
    calls = []

    async def fetcher(_media_type, _title, page, _actor):
        calls.append(page)
        return []

    service = InlineSearchService(fetcher=fetcher)
    query = parse_inline_query("anime budget")
    assert query is not None

    for page in range(1, INLINE_PAGE_LIMIT + 1):
        assert await service.get_page(
            query,
            page,
            authorized=lambda: True,
            actor=_ACTOR,
        ) is not None
    assert await service.get_page(
        query,
        1,
        authorized=lambda: True,
        actor=_ACTOR,
    ) is not None
    with pytest.raises(InlineSearchLimitExceeded) as exc_info:
        await service.get_page(
            query,
            INLINE_PAGE_LIMIT + 1,
            authorized=lambda: True,
            actor=InlineActor(202, "Bob\nInjected", None),
        )
    assert exc_info.value.retry_after > 0
    assert calls == list(range(1, INLINE_PAGE_LIMIT + 1))


@pytest.mark.asyncio
async def test_limit_log_is_russian_attributed_safe_and_once_per_window(caplog):
    async def fetcher(_media_type, _title, _page, _actor):
        return []

    service = InlineSearchService(fetcher=fetcher)
    service._page_budget = RollingBudget(2, 60.0)
    query = parse_inline_query("anime log")
    alice = InlineActor(101, "Alice", "alice")
    injected = InlineActor(202, "Bob\nПодделка", None)
    assert query is not None

    for page, actor in ((1, alice), (2, injected)):
        assert await service.get_page(
            query,
            page,
            authorized=lambda: True,
            actor=actor,
        ) is not None

    with caplog.at_level(logging.WARNING, logger="shikiupdatesbot"):
        for page in (3, 4):
            with pytest.raises(InlineSearchLimitExceeded):
                await service.get_page(
                    query,
                    page,
                    authorized=lambda: True,
                    actor=alice,
                )

    messages = [
        record.getMessage()
        for record in caplog.records
        if "исчерпан минутный лимит" in record.getMessage()
    ]
    assert len(messages) == 1
    assert "последний потребител" in messages[0]
    assert "отклонённый запрос" in messages[0]
    assert "Telegram ID 101" in messages[0]
    assert "Telegram ID 202" in messages[0]
    assert "Bob\\nПодделка" in messages[0]
    assert "\nПодделка" not in messages[0]


@pytest.mark.asyncio
async def test_global_attempt_reserve_becomes_same_visible_inline_limit(caplog):
    async def fetcher(_media_type, _title, _page, _actor):
        raise ShikimoriBudgetExceeded(
            BudgetSnapshot(
                used=60,
                capacity=60,
                retry_after=12.5,
                last_actor=101,
                actor_counts=((101, 8),),
            )
        )

    service = InlineSearchService(fetcher=fetcher)
    query = parse_inline_query("anime reserve")
    assert query is not None

    with caplog.at_level(logging.WARNING, logger="shikiupdatesbot"):
        with pytest.raises(InlineSearchLimitExceeded) as exc_info:
            await service.get_page(
                query,
                1,
                authorized=lambda: True,
                actor=_ACTOR,
            )

    assert exc_info.value.retry_after == 12.5
    assert "резерв общего минутного бюджета HTTP-попыток" in caplog.text
    assert "системный трафик — 52" in caplog.text


@pytest.mark.asyncio
async def test_authorization_repeats_before_fetch_and_cache_fill():
    allowed = {"value": True}
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetcher(_media_type, _title, _page, _actor):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return [{"id": "1"}]

    service = InlineSearchService(fetcher=fetcher)
    query = parse_inline_query("anime revoke")
    assert query is not None
    task = asyncio.create_task(
        service.get_page(
            query,
            1,
            authorized=lambda: allowed["value"],
            actor=_ACTOR,
        )
    )
    await started.wait()
    allowed["value"] = False
    release.set()

    assert await task is None
    allowed["value"] = True
    assert await service.get_page(
        query,
        1,
        authorized=lambda: True,
        actor=_ACTOR,
    ) is not None
    assert calls == 2


def test_continuations_are_query_bound_chained_and_expire_without_mutation():
    clock = {"now": 50.0}
    service = InlineSearchService(clock=lambda: clock["now"])
    query = parse_inline_query("manga Higurashi")
    same = parse_inline_query("МАНГА higurashi")
    cross_title = parse_inline_query("manga Berserk")
    cross_media = parse_inline_query("ranobe Higurashi")
    assert query and same and cross_title and cross_media

    page2 = service.issue_continuation(query, page=2, preceding_expires_at=100.0)
    assert service.resolve_continuation(same, page2) == 2
    page3 = service.issue_continuation(query, page=3, preceding_expires_at=110.0)
    assert service.resolve_continuation(query, page3) == 3

    snapshot = dict(service._continuations)
    assert service.resolve_continuation(query, "malformed") is None
    assert service.resolve_continuation(cross_title, page2) is None
    assert service.resolve_continuation(cross_media, page2) is None
    assert service.resolve_continuation(query, uuid_like_unissued := "0" * 32) is None
    assert uuid_like_unissued not in service._continuations
    assert service._continuations == snapshot

    clock["now"] = 100.0
    assert service.resolve_continuation(query, page2) is None
    assert service._continuations == snapshot
