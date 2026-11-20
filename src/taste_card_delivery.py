# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Локальное формирование и доставка одной карточки без изменения списков."""

import asyncio
from dataclasses import dataclass
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
from storage import read_stats_all_snapshot
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

_generation_locks = WeakKeyDictionary()
_RENDER_FAILURE = "Не удалось сформировать карточку вкуса. Попробуй позже."
_SEND_FAILURE = "Не удалось отправить карточку вкуса. Попробуй позже."
_SEND_UNCERTAIN = (
    "Не удалось подтвердить доставку карточки. Она могла прийти; "
    "повторный запрос может создать дубль."
)


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
        log.warning("Карточка вкуса: пояснение не доставлено (%s).", type(error).__name__)


def _prepare(category: str) -> RenderedTasteCard | str:
    """Чтение, независимые поля, расчёт и Pillow не блокируют event loop или общий кеш."""
    source = build_taste_input(
        read_stats_all_snapshot(), category, profile=SHIKI_USER, base_url=SHIKI_BASE_URL,
        now=datetime.now(timezone.utc),
    )
    card = build_taste_card(source)
    return render_taste_card(card) if card.available else unavailable_text(card)


async def _generate(category: str) -> RenderedTasteCard | str:
    """Дождаться ограниченного worker даже при отмене, не отправляя его результат."""
    worker = asyncio.create_task(asyncio.to_thread(_prepare, category))
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
    lock = _generation_locks.setdefault(loop, asyncio.Lock())
    if lock.locked():
        await _notice(bot, chat_id, "Карточка уже формируется. Попробуй через несколько секунд.")
        return TasteDeliveryResult("busy")
    try:
        async with lock:
            prepared = await _generate(category)
    except Exception as error:
        log.warning("Карточка вкуса: формирование не выполнено (%s).", type(error).__name__)
        await _notice(bot, chat_id, _RENDER_FAILURE)
        return TasteDeliveryResult("render_error")
    if isinstance(prepared, str):
        await _notice(bot, chat_id, prepared)
        return TasteDeliveryResult("unavailable")
    result = await send_with_retry(
        lambda: bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(prepared.image, filename=prepared.filename),
            caption=prepared.caption,
            parse_mode=ParseMode.HTML,
        ),
        policy=RetryPolicy.AT_LEAST_ONCE,
    )
    if result.delivered:
        return TasteDeliveryResult("sent", result)
    await _notice(bot, chat_id, _SEND_UNCERTAIN if result.uncertain else _SEND_FAILURE)
    return TasteDeliveryResult("uncertain" if result.uncertain else "failed", result)
