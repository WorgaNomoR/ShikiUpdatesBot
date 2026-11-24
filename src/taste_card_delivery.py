# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Локальное формирование и доставка одной карточки без изменения списков."""

import asyncio
from dataclasses import (
    dataclass,
    field,
)
from datetime import (
    datetime,
    timezone,
)
from weakref import WeakKeyDictionary

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile

from config import (
    SHIKI_BASE_URL,
    SHIKI_USER,
    log,
)
from profile_avatar import (
    ProfileAvatar,
    current_profile_avatar,
)
from storage import (
    published_stats_all_revision,
    read_stats_all_snapshot,
)
from taste_card import (
    build_taste_card,
    unavailable_text,
)
from taste_card_data import build_taste_input
from taste_card_render import (
    RenderedTasteCard,
    render_taste_card,
)
from telegram_delivery import (
    RetryPolicy,
    SendResult,
    send_with_retry,
)

_RENDER_FAILURE = "Не удалось сформировать карточку статистики. Попробуй позже."
_SEND_FAILURE = "Не удалось отправить карточку статистики. Попробуй позже."
_SEND_UNCERTAIN = (
    "Не удалось подтвердить доставку карточки. Она могла прийти; "
    "повторный запрос может создать дубль."
)


@dataclass(frozen=True)
class _CachedCard:
    revision: tuple
    avatar_version: tuple
    payload: RenderedTasteCard


@dataclass
class _Generation:
    worker: asyncio.Lock = field(default_factory=asyncio.Lock)
    cards: dict[str, _CachedCard] = field(default_factory=dict)


_generations = WeakKeyDictionary()


@dataclass(frozen=True)
class TasteDeliveryResult:
    """Исход интерактивного запроса, не являющийся durable-обязательством."""

    state: str
    send_result: SendResult | None = None


async def _notice(bot: Bot, chat_id: int, text: str) -> None:
    """Один best-effort результат с безопасной политикой повторов."""
    try:
        await send_with_retry(
            lambda: bot.send_message(chat_id=chat_id, text=text),
            policy=RetryPolicy.SAFE_ONLY,
        )
    except Exception as error:
        log.warning("Карточка статистики: пояснение не доставлено (%s).", type(error).__name__)


def _prepare(category: str, avatar: ProfileAvatar) -> tuple[RenderedTasteCard | str, tuple | None]:
    """Чтение, независимые поля, расчёт и Pillow не блокируют event loop или общий кеш."""
    snapshot = read_stats_all_snapshot()
    source = build_taste_input(
        snapshot, category, profile=SHIKI_USER, base_url=SHIKI_BASE_URL,
        now=datetime.now(timezone.utc),
    )
    card = build_taste_card(source)
    if not card.available:
        return unavailable_text(card), None
    return render_taste_card(card, avatar=avatar.image), snapshot.revision


async def _generate(category: str, avatar: ProfileAvatar) -> tuple[RenderedTasteCard | str, tuple | None]:
    """Дождаться ограниченного worker даже при отмене, не отправляя его результат."""
    worker = asyncio.create_task(asyncio.to_thread(_prepare, category, avatar))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            cancelled = True
            if worker.cancelled():
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def deliver_taste_card(bot: Bot, chat_id: int, category: str) -> TasteDeliveryResult:
    """Снять один snapshot, сгенерировать одно фото и сохранить свидетельства отправки."""
    loop = asyncio.get_running_loop()
    generation = _generations.setdefault(loop, _Generation())
    avatar = current_profile_avatar()
    revision = published_stats_all_revision()
    cached = generation.cards.get(category)
    if cached and cached.revision == revision and cached.avatar_version == avatar.version:
        prepared = cached.payload
    else:
        generation.cards.pop(category, None)
        if generation.worker.locked():
            await _notice(bot, chat_id, "Карточка уже формируется. Попробуй через несколько секунд.")
            return TasteDeliveryResult("busy")
        try:
            async with generation.worker:
                prepared, source_revision = await _generate(category, avatar)
        except Exception as error:
            log.warning("Карточка статистики: формирование не выполнено (%s).", type(error).__name__)
            await _notice(bot, chat_id, _RENDER_FAILURE)
            return TasteDeliveryResult("render_error")
        current_avatar = current_profile_avatar()
        if avatar.image and avatar.user_id != current_avatar.user_id:
            await _notice(bot, chat_id, _RENDER_FAILURE)
            return TasteDeliveryResult("render_error")
        if (
            isinstance(prepared, RenderedTasteCard) and source_revision is not None
            and source_revision == published_stats_all_revision() and avatar.version == current_avatar.version
        ):
            generation.cards[category] = _CachedCard(source_revision, avatar.version, prepared)
    if isinstance(prepared, str):
        await _notice(bot, chat_id, prepared)
        return TasteDeliveryResult("unavailable")

    async def check_avatar_identity() -> None:
        if avatar.image and avatar.user_id != current_profile_avatar().user_id:
            raise RuntimeError("taste_card_avatar_identity_changed")

    result = await send_with_retry(
        lambda: bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(prepared.image, filename=prepared.filename),
            caption=prepared.caption,
            parse_mode=ParseMode.HTML,
        ),
        policy=RetryPolicy.AT_LEAST_ONCE,
        before_attempt=check_avatar_identity,
    )
    if result.delivered:
        return TasteDeliveryResult("sent", result)
    await _notice(bot, chat_id, _SEND_UNCERTAIN if result.uncertain else _SEND_FAILURE)
    return TasteDeliveryResult("uncertain" if result.uncertain else "failed", result)
