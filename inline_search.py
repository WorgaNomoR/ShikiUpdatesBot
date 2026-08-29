# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Разбор inline-запросов и их ограниченное процессное состояние."""

import asyncio
import logging
import time
import uuid
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass

from request_budget import (
    BudgetSnapshot,
    RollingBudget,
)
from shiki_api import (
    ShikimoriBudgetExceeded,
    fetch_inline_search,
)

DEBOUNCE_SECONDS = 2.0
CACHE_TTL_SECONDS = 10 * 60.0
INLINE_PAGE_LIMIT = 30
INLINE_PAGE_PERIOD = 60.0
SHIKIMORI_PAGE_SIZE = 49

_PREFIXES = {
    "a": "anime",
    "а": "anime",
    "anime": "anime",
    "аниме": "anime",
    "m": "manga",
    "м": "manga",
    "manga": "manga",
    "манга": "manga",
    "r": "ranobe",
    "р": "ranobe",
    "ranobe": "ranobe",
    "ранобэ": "ranobe",
}

log = logging.getLogger("shikiupdatesbot")

PageFetcher = Callable[
    [str, str, int, "InlineActor"],
    Awaitable[list[dict] | None],
]
Sleep = Callable[[float], Awaitable[None]]
AuthorizationCheck = Callable[[], bool]


@dataclass(frozen=True)
class ParsedInlineQuery:
    """Проверенная поисковая пара и канонический ключ заголовка."""

    media_type: str
    title: str
    normalized_title: str


@dataclass(frozen=True)
class SearchPage:
    """Успешная страница и срок её валидности в процессном кеше."""

    items: tuple[dict, ...]
    expires_at: float


@dataclass(frozen=True)
class InlineActor:
    """Актуальная Telegram-идентификация разрешённого поиска."""

    user_id: int
    full_name: str
    username: str | None


class InlineSearchLimitExceeded(RuntimeError):
    """Новая страница не запущена из-за защитного лимита Shikimori."""

    def __init__(self, retry_after: float):
        self.retry_after = max(0.0, retry_after)
        super().__init__("Лимит запросов к Shikimori временно исчерпан")


@dataclass(frozen=True)
class _Continuation:
    media_type: str
    normalized_title: str
    page: int
    expires_at: float


def parse_inline_query(raw_query: object) -> ParsedInlineQuery | None:
    """Нормализовать пробелы и отделить обязательный префикс типа медиа."""
    if not isinstance(raw_query, str):
        return None
    normalized = " ".join(raw_query.split())
    prefix, separator, title = normalized.partition(" ")
    media_type = _PREFIXES.get(prefix.casefold())
    if not separator or media_type is None or len(title) < 2:
        return None
    return ParsedInlineQuery(
        media_type=media_type,
        title=title,
        normalized_title=title.casefold(),
    )


async def _default_fetcher(
    media_type: str,
    title: str,
    page: int,
    actor: InlineActor,
) -> list[dict] | None:
    return await fetch_inline_search(
        media_type,
        title,
        page,
        actor_user_id=actor.user_id,
    )


class InlineSearchService:
    """Владеть задержкой ввода, TTL-кешем, схлопыванием и продолжениями."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        fetcher: PageFetcher = _default_fetcher,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._fetcher = fetcher
        self._page_budget = RollingBudget(
            INLINE_PAGE_LIMIT,
            INLINE_PAGE_PERIOD,
            clock=clock,
        )
        self._cache: dict[tuple[str, str, int], SearchPage] = {}
        self._inflight: dict[
            tuple[str, str, int],
            asyncio.Task[SearchPage | None],
        ] = {}
        self._generations: dict[int, int] = {}
        self._pending: dict[int, tuple[int, asyncio.Task[None]]] = {}
        self._continuations: dict[str, _Continuation] = {}
        self._actors: dict[int, InlineActor] = {}
        self._limit_log_until: dict[str, float] = {}

    def _remember_actor(self, actor: InlineActor) -> None:
        """Обновить имя и не копить участников за пределами текущего окна."""
        active_ids = {
            int(user_id)
            for user_id, _count in self._page_budget.snapshot().actor_counts
            if isinstance(user_id, int)
        }
        self._actors = {
            user_id: known_actor
            for user_id, known_actor in self._actors.items()
            if user_id in active_ids
        }
        self._actors[actor.user_id] = actor

    def _actor_log_text(self, user_id: object) -> str:
        """Представить actor без возможности подделать строку журнала."""
        if not isinstance(user_id, int):
            return "системный трафик"
        actor = self._actors.get(user_id)
        if actor is None:
            return f"Telegram ID {user_id}"
        username = f"@{actor.username}" if actor.username else "нет"
        return (
            f"Telegram ID {user_id}, имя={actor.full_name!r}, "
            f"username={username!r}"
        )

    def _raise_limit(
        self,
        *,
        source: str,
        snapshot: BudgetSnapshot,
        rejected_actor: InlineActor,
    ) -> None:
        """Один раз за период записать атрибуцию и вернуть управляемую ошибку."""
        now = self._clock()
        if self._limit_log_until.get(source, 0.0) <= now:
            ranked = sorted(
                snapshot.actor_counts,
                key=lambda entry: (-entry[1], str(entry[0])),
            )
            consumers = ", ".join(
                f"{self._actor_log_text(user_id)} — {count}"
                for user_id, count in ranked
            ) or "нет данных"
            system_count = snapshot.used - sum(count for _actor, count in ranked)
            if system_count > 0:
                consumers = f"{consumers}; системный трафик — {system_count}"
            log.warning(
                "Inline-поиск Shikimori: исчерпан %s; использовано %d/%d; "
                "до освобождения %.1f с; последний потребитель: %s; "
                "отклонённый запрос: %s; расход за окно: %s.",
                source,
                snapshot.used,
                snapshot.capacity,
                snapshot.retry_after,
                self._actor_log_text(snapshot.last_actor),
                self._actor_log_text(rejected_actor.user_id),
                consumers,
            )
            self._limit_log_until[source] = now + max(snapshot.retry_after, 1.0)
        raise InlineSearchLimitExceeded(snapshot.retry_after)

    def invalidate_debounce(self, user_id: int) -> None:
        """Инвалидировать ожидающий запрос, не создавая нового таймера."""
        self._generations[user_id] = self._generations.get(user_id, 0) + 1
        pending = self._pending.pop(user_id, None)
        if pending is not None:
            pending[1].cancel()

    async def debounce(self, user_id: int) -> int | None:
        """Дождаться тишины пользователя или вернуть None после инвалидирования."""
        generation = self._generations.get(user_id, 0) + 1
        self._generations[user_id] = generation
        previous = self._pending.pop(user_id, None)
        if previous is not None:
            previous[1].cancel()

        timer = asyncio.create_task(self._sleep(DEBOUNCE_SECONDS))
        self._pending[user_id] = (generation, timer)
        try:
            await timer
        except asyncio.CancelledError:
            return None
        finally:
            if self._pending.get(user_id) == (generation, timer):
                self._pending.pop(user_id, None)
        if self._generations.get(user_id) != generation:
            return None
        return generation

    def is_current(self, user_id: int, generation: int) -> bool:
        """Проверить поколение непосредственно перед кешем и сетью."""
        return self._generations.get(user_id) == generation

    def resolve_continuation(
        self,
        query: ParsedInlineQuery,
        offset: str,
    ) -> int | None:
        """Прочитать только ранее выданное и ещё действующее продолжение."""
        continuation = self._continuations.get(offset)
        if continuation is None:
            return None
        if continuation.expires_at <= self._clock():
            return None
        if (
            continuation.media_type != query.media_type
            or continuation.normalized_title != query.normalized_title
        ):
            return None
        return continuation.page

    def issue_continuation(
        self,
        query: ParsedInlineQuery,
        *,
        page: int,
        preceding_expires_at: float,
    ) -> str:
        """Выдать непрозрачное продолжение после полной предыдущей страницы."""
        now = self._clock()
        self._continuations = {
            token: continuation
            for token, continuation in self._continuations.items()
            if continuation.expires_at > now
        }
        token = uuid.uuid4().hex
        self._continuations[token] = _Continuation(
            media_type=query.media_type,
            normalized_title=query.normalized_title,
            page=page,
            expires_at=preceding_expires_at,
        )
        return token

    async def get_page(
        self,
        query: ParsedInlineQuery,
        page: int,
        *,
        authorized: AuthorizationCheck,
        actor: InlineActor,
    ) -> SearchPage | None:
        """Вернуть кешированную либо схлопнутую новую успешную страницу."""
        if not authorized():
            return None
        self._remember_actor(actor)
        key = (query.media_type, query.normalized_title, page)
        now = self._clock()
        self._cache = {
            cache_key: value
            for cache_key, value in self._cache.items()
            if value.expires_at > now
        }
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > now:
            return cached

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._load_page(
                    key,
                    query,
                    page,
                    authorized=authorized,
                    actor=actor,
                )
            )
            self._inflight[key] = task
        result = await asyncio.shield(task)
        if not authorized():
            return None
        return result

    async def _load_page(
        self,
        key: tuple[str, str, int],
        query: ParsedInlineQuery,
        page: int,
        *,
        authorized: AuthorizationCheck,
        actor: InlineActor,
    ) -> SearchPage | None:
        try:
            if not authorized():
                return None
            if not self._page_budget.try_acquire(actor=actor.user_id):
                self._raise_limit(
                    source="минутный лимит новых поисковых страниц",
                    snapshot=self._page_budget.snapshot(),
                    rejected_actor=actor,
                )
            try:
                items = await self._fetcher(
                    query.media_type,
                    query.title,
                    page,
                    actor,
                )
            except ShikimoriBudgetExceeded as e:
                self._raise_limit(
                    source="резерв общего минутного бюджета HTTP-попыток",
                    snapshot=e.snapshot,
                    rejected_actor=actor,
                )
            except InlineSearchLimitExceeded:
                raise
            except Exception:
                return None
            if items is None:
                return None
            if not authorized():
                return None
            result = SearchPage(
                items=tuple(items),
                expires_at=self._clock() + CACHE_TTL_SECONDS,
            )
            self._cache[key] = result
            return result
        finally:
            self._inflight.pop(key, None)
