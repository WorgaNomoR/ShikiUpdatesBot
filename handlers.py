# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Хендлеры и фоновый цикл ShikiUpdatesBot.

Верхний слой: команды и FSM (/start, /stop, /subs, /block, /unblock, /blocklist,
/broadcast, /backup, /facts, /pick, /status, /stats, /favs, /fact, /info,
/version), inline-меню, рассылка, цикл уведомлений (check_and_notify*,
polling_loop) и ротация квартала. Зависит от всех нижних модулей;
main.py лишь регистрирует эти функции в Dispatcher.
"""

import asyncio
import io
import math
import time

import aiohttp
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import (
    State,
    StatesGroup,
)
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultsButton,
    Message,
    ReplyParameters,
)

from access_control import (
    INLINE_ACCESS_ALLOWED,
    INLINE_ACCESS_BLOCKED,
    inline_access_status,
)
from backup import (
    BACKUP_TAG,
    IMPORT_DOCUMENT_MAX_BYTES,
    _backup_after_subscription,
    _weekly_backup_if_due,
    restore_backup_zip,
    send_backup,
)
from config import (
    CHECK_INTERVAL,
    DISPLAY_NAME,
    ERROR_NOTIFY_INTERVAL,
    FULL_SYNC_INTERVAL,
    OWNER_ID,
    SHIKI_BASE_URL,
    SHIKI_USER,
    log,
)
from fact_bank import (
    FACT_BANK_MAX_BYTES,
    FACT_FILE_INVALID,
    FACT_FILE_MISSING,
    FactBankSnapshot,
    FactBankValidationError,
    StaleFactBankError,
    canonical_active_fact_bank,
    clear_fact_bank,
    fact_bank_candidate_revision,
    fact_bank_delta,
    parse_fact_bank_bytes,
    publish_fact_bank,
    reload_fact_bank,
    serialize_fact_bank,
)
from healthcheck import heartbeat
from inline_cards import (
    build_fact_keyboard,
    build_fact_result,
    build_fact_text,
    build_inline_result,
    finalize_inline_results,
)
from inline_facts import (
    FACT_QUERY_MATCH,
    FACT_QUERY_REJECT,
    build_fact_share_query,
    classify_fact_query,
    fact_from_share_query,
    select_fact,
    select_next_fact,
)
from inline_search import (
    SHIKIMORI_PAGE_SIZE,
    InlineActor,
    InlineSearchLimitExceeded,
    InlineSearchService,
    parse_inline_query,
)
from messages import (
    BROADCAST_HEADER,
    DISPLAY_NAME_CONTEXT,
    build_favourite_message,
    build_message,
    build_startup_snapshot,
    classify_event,
    clean_description,
    extract_score,
    extract_score_change,
    format_rate_entry,
)
from runtime import RESOURCE_ROOT
from runtime_status import (
    RuntimeSnapshot,
    get_runtime_snapshot,
    mark_full_sync_success,
    set_polling_active,
)
from shiki_api import (
    _FAV_CATEGORIES,
    _INDUSTRY_CATEGORIES,
    ANIME_ALLOWED_KINDS,
    HISTORY_PAGE_LIMIT,
    ProfilePrivacyError,
    fetch_current_rates,
    fetch_favourites,
    fetch_history,
    get_media_info,
    is_relevant,
)
from stats import (
    PICK_CATEGORY_ANIME,
    PICK_CATEGORY_MANGA,
    PICK_CATEGORY_RANOBE,
    PickCandidate,
    _collect_favourites,
    _load_prev_quarter_summary,
    _save_quarter_snapshot,
    _update_by_quarter,
    build_current_stats_messages,
    build_favourites_messages,
    build_pick_catalog,
    build_quarterly_report_messages,
    build_stats_all_messages,
    record_current_event,
    select_contrast_pick_candidate,
    select_pick_candidate,
    sync_stats_all,
)
from storage import (
    STATS_ALL_INVALID,
    STATS_ALL_MISSING,
    STATS_ALL_VALID,
    BlockedUsersMutationError,
    BlockedUsersStateError,
    _empty_stats_current,
    add_blocked_user,
    list_blocked_users,
    load_seen_favourites,
    load_seen_ids,
    load_stats_all,
    load_stats_all_snapshot,
    load_stats_current,
    load_subscribers,
    load_update_state,
    remove_blocked_user,
    restorable_state_transaction,
    save_seen_favourites,
    save_seen_ids,
    save_stats_all,
    save_stats_current,
    save_subscribers,
    validate_telegram_user_id,
)
from telegram_delivery import is_blocked_error as _is_blocked_error
from telegram_delivery import send_with_retry
from updates import (
    build_version_keyboard,
    build_version_text,
    refresh_update_state,
)
from utils import (
    _parse_iso_utc,
    _rel_url,
    _subscriber_link,
    current_quarter,
    h,
    previous_quarter,
    quarter_label,
)

# Фиксированная пауза между фазами стартовых фетчей (анти-429, boot-throttle).
# Без джиттера — предсказуемый ритм (firewall-философия).
BOOT_PHASE_DELAY = 2.0  # секунд
_HISTORY_CATCHUP_MAX_PAGES = 5
_STATUS_CACHE_TTL = 60.0
_TELEGRAM_MESSAGE_LIMIT = 4096
_BLOCKLIST_HINT = (
    "Подсказка: <code>/block 123456789</code> — заблокировать; "
    "<code>/unblock 123456789</code> — разблокировать."
)
_PICK_CALLBACK_PREFIX = "pick:"
_PICK_CATEGORIES: frozenset[str] = frozenset({
    PICK_CATEGORY_ANIME,
    PICK_CATEGORY_MANGA,
    PICK_CATEGORY_RANOBE,
})
_FACT_NEXT_CALLBACK_PREFIX = "fact:next:"
FACTS_APPLY_CALLBACK_PREFIX = "facts:apply:"
FACTS_ASK_CLEAR_CALLBACK_PREFIX = "facts:ask-clear:"
FACTS_CONFIRM_CLEAR_CALLBACK_PREFIX = "facts:confirm-clear:"
FACT_BANK_EXAMPLE_PATH = RESOURCE_ROOT / "examples" / "facts.json"
INFO_PREVIEW_PATH = RESOURCE_ROOT / "assets" / "info-preview.png"
_info_preview_file_id: str | None = None
_status_cache: tuple[list[dict], list[dict]] | None = None
_status_cache_at = 0.0
_status_cache_lock: asyncio.Lock | None = None
_inline_search_service = InlineSearchService()


def _profile_privacy_owner_text() -> str:
    """Полная инструкция владельцу по открытию публичного списка."""
    settings_url = f"{SHIKI_BASE_URL.rstrip('/')}/{SHIKI_USER}/edit/profile"
    return (
        "⚠️ Профиль Shikimori закрыт.\n\n"
        "ShikiUpdatesBot использует только публичные данные и не может прочитать "
        "закрытые историю, выгрузки списков и текущие статусы.\n\n"
        "Открой настройки профиля и установи:\n"
        "«Могут видеть мой список» → «Все посетители сайта»\n\n"
        f"{settings_url}"
    )


def _profile_privacy_public_text() -> str:
    """Краткое объяснение без настроек и owner-only ссылки."""
    return (
        "⚠️ Профиль Shikimori закрыт. "
        "Владельцу бота нужно открыть доступ к списку в настройках профиля."
    )


def _get_status_cache_lock() -> asyncio.Lock:
    """Лениво создаёт лок, схлопывающий конкурентные обновления /status."""
    global _status_cache_lock
    if _status_cache_lock is None:
        _status_cache_lock = asyncio.Lock()
    return _status_cache_lock


def _cached_status_rates(now: float) -> tuple[list[dict], list[dict]] | None:
    """Возвращает свежий внутрипроцессный кеш /status или None."""
    if _status_cache is None or now - _status_cache_at >= _STATUS_CACHE_TTL:
        return None
    return _status_cache


async def _get_status_rates() -> tuple[list[dict], list[dict]] | None:
    """Получает свежие rates, переиспользуя общий успешный результат 60 секунд."""
    global _status_cache, _status_cache_at

    cached = _cached_status_rates(time.monotonic())
    if cached is not None:
        return cached

    async with _get_status_cache_lock():
        cached = _cached_status_rates(time.monotonic())
        if cached is not None:
            return cached

        anime_list, manga_list = await asyncio.gather(
            fetch_current_rates("anime", ["watching", "rewatching"]),
            fetch_current_rates("manga", ["watching", "rewatching"]),
        )
        if anime_list is None or manga_list is None:
            return None

        _status_cache = (anime_list, manga_list)
        _status_cache_at = time.monotonic()
        return _status_cache


class BroadcastStates(StatesGroup):
    waiting_content = State()   # ждём сообщение от владельца
    waiting_confirm = State()   # ждём нажатия кнопки подтверждения


class PickStates(StatesGroup):
    active = State()  # одно текущее owner-menu и его неповторяющийся цикл


def _confirm_kb() -> InlineKeyboardMarkup:
    """Инлайн-клавиатура с кнопками подтверждения/отмены рассылки."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📢 Отправить", callback_data="broadcast_send"),
        InlineKeyboardButton(text="❌ Отмена",    callback_data="broadcast_cancel"),
    ]])


async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    """Удалить сообщение по возможности, не распространяя ошибку.

    Глушит штатные «message to delete not found» / уже удалённое / истёкшее
    окно: чистка чата не должна ронять основной флоу. Переиспользуемый примитив
    для любых FSM-флоу, где надо подчистить служебные сообщения.
    """
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        log.debug("  _safe_delete: пропускаю %s (chat=%s msg=%s)", e, chat_id, message_id)


async def _send_broadcast_message(bot: Bot, chat_id: int, data: dict) -> list[Message]:
    """Отправляет одно сообщение рассылки. Возвращает список фактически
    отправленных Message (стикер = 2 сообщения: шапка + стикер) — нужно,
    чтобы превью можно было целиком удалить по id."""
    msg_type  = data["msg_type"]
    user_text = data.get("user_text", "")
    file_id   = data.get("file_id")
    sent: list[Message] = []

    if msg_type == "text":
        body = f"\n<blockquote>{h(user_text)}</blockquote>" if user_text else ""
        sent.append(await bot.send_message(
            chat_id=chat_id, text=f"{BROADCAST_HEADER}{body}", parse_mode=ParseMode.HTML,
        ))

    elif msg_type == "sticker":
        sent.append(await bot.send_message(chat_id=chat_id, text=BROADCAST_HEADER, parse_mode=ParseMode.HTML))
        sent.append(await bot.send_sticker(chat_id=chat_id, sticker=file_id))

    else:
        caption = f"{BROADCAST_HEADER}\n\n{h(user_text)}" if user_text else BROADCAST_HEADER
        common = dict(chat_id=chat_id, caption=caption, parse_mode=ParseMode.HTML)
        if msg_type == "photo":
            sent.append(await bot.send_photo(photo=file_id, show_caption_above_media=True, **common))
        elif msg_type == "video":
            sent.append(await bot.send_video(video=file_id, show_caption_above_media=True, **common))
        elif msg_type == "animation":
            sent.append(await bot.send_animation(animation=file_id, show_caption_above_media=True, **common))
        elif msg_type == "document":
            sent.append(await bot.send_document(document=file_id, **common))
        elif msg_type == "voice":
            sent.append(await bot.send_voice(voice=file_id, **common))

    return sent


# ═══════════════════════════════════════════════════════════════════
#  РОТАЦИЯ КВАРТАЛА
# ═══════════════════════════════════════════════════════════════════

_PENDING_QUARTER_DELIVERY = "pending_quarter_delivery"


def _valid_pending_quarter_delivery(cur: dict) -> dict | None:
    """Вернуть корректное состояние отложенной доставки квартала."""
    pending = cur.get(_PENDING_QUARTER_DELIVERY)
    if pending is None:
        return None
    if not isinstance(pending, dict) or set(pending) != {
        "old_period",
        "new_period",
        "report_messages",
        "report_sent",
    }:
        return None
    messages = pending.get("report_messages")
    if (
        not isinstance(pending.get("old_period"), str)
        or not pending["old_period"]
        or not isinstance(pending.get("new_period"), str)
        or pending["new_period"] != cur.get("period")
        or not isinstance(messages, list)
        or not all(isinstance(message, str) for message in messages)
        or not isinstance(pending.get("report_sent"), bool)
    ):
        return None
    return pending


async def _deliver_pending_quarter(bot: Bot, cur: dict) -> dict:
    """Дослать квартальный отчёт и бэкап, отмечая только успешные этапы."""
    pending = _valid_pending_quarter_delivery(cur)
    if pending is None:
        if cur.get(_PENDING_QUARTER_DELIVERY) is None:
            return cur
        log.warning("rotate_quarter: повреждённое состояние доставки сброшено.")
        async with restorable_state_transaction():
            cur = load_stats_current()
            if (
                cur.get(_PENDING_QUARTER_DELIVERY) is not None
                and _valid_pending_quarter_delivery(cur) is None
            ):
                cur[_PENDING_QUARTER_DELIVERY] = None
                save_stats_current(cur)
        return cur

    if not pending["report_sent"]:
        for msg in pending["report_messages"]:
            if not await _send_long(bot, OWNER_ID, msg):
                return load_stats_current()
            await asyncio.sleep(0.4)

        async with restorable_state_transaction():
            cur = load_stats_current()
            if cur.get(_PENDING_QUARTER_DELIVERY) != pending:
                return cur
            pending = dict(pending)
            pending["report_sent"] = True
            cur["last_report_sent"] = pending["new_period"]
            cur[_PENDING_QUARTER_DELIVERY] = pending
            save_stats_current(cur)
        log.info(
            "rotate_quarter: отчёт за %s отправлен владельцу (%d сообщ.).",
            pending["old_period"],
            len(pending["report_messages"]),
        )

    backup_sent = await send_backup(
        bot,
        f"🗓️ Ротация квартала: {h(quarter_label(pending['old_period']))} → "
        f"{h(quarter_label(pending['new_period']))}.\n"
        f"Снапшот состояния.\n\n{BACKUP_TAG}",
    )
    if not backup_sent:
        return load_stats_current()

    async with restorable_state_transaction():
        cur = load_stats_current()
        if cur.get(_PENDING_QUARTER_DELIVERY) != pending:
            return cur
        cur["last_backup_at"] = time.time()
        cur[_PENDING_QUARTER_DELIVERY] = None
        save_stats_current(cur)
    return cur


async def rotate_quarter_if_needed(bot: Bot, cur: dict, stats_all: dict, resync: bool = True) -> dict:
    """
    Проверяем смену квартала. Если сменился:
      1. Защита last_report_sent от двойной отправки.
      2. Синхронизируем stats_all (чтобы метаданные завершённых были свежими);
         на старте пропускаем — polling_loop уже синкнул (resync=False).
      3. Строим отчёт, сохраняем снапшот quarters/<period>.json.
      4. Обновляем by_quarter в агрегатах stats_all.
      5. Отправляем отчёт владельцу.
      6. Сбрасываем stats_current на новый период.
    Возвращает (возможно новый) stats_current.
    """
    now_period = current_quarter()
    async with restorable_state_transaction():
        cur = load_stats_current()
        rotation_needed = cur.get("period") != now_period

    if not rotation_needed:
        return await _deliver_pending_quarter(bot, cur)

    # Свежие метаданные перед отчётом. На старте (resync=False) пропускаем:
    # polling_loop уже синкнул stats_all на общей сессии, а второй синк своей
    # сессией сразу после первого ловил 429 (boot-burst в день ротации).
    if resync:
        try:
            stats_all, synced_ok = await sync_stats_all()
            if synced_ok:
                mark_full_sync_success()
        except ProfilePrivacyError:
            raise
        except Exception as e:
            log.error("rotate_quarter: sync_stats_all упал: %s", e)

    async with restorable_state_transaction():
        cur = load_stats_current()
        if cur.get("period") == now_period:
            return cur

        old_period = cur.get("period", "???")
        if cur.get("last_report_sent") == now_period:
            # Отчёт уже отправлен (перезапуск в день ротации) — просто сбрасываем
            log.info("rotate_quarter: отчёт за переход в %s уже был отправлен.", now_period)
            fresh = _empty_stats_current(now_period)
            fresh["last_report_sent"] = now_period
            save_stats_current(fresh)
            return fresh

        log.info("rotate_quarter: квартал сменился %s → %s.", old_period, now_period)

        # Сравниваем закрываемый квартал с предшествующим ему снапшотом.
        prev_period = previous_quarter(old_period)
        prev_quarter = _load_prev_quarter_summary(prev_period) if prev_period else None

        try:
            report_msgs = build_quarterly_report_messages(cur, stats_all, prev_quarter)
        except Exception as e:
            log.error("rotate_quarter: build_quarterly_report_messages упал: %s", e)
            report_msgs = [
                f"⚠️ Отчёт за {h(quarter_label(old_period))} "
                f"не удалось сформировать: {h(str(e))}"
            ]

        # Снапшот квартала и новый текущий квартал публикуются под одним lock.
        _save_quarter_snapshot(old_period, cur, stats_all)

        try:
            _update_by_quarter(stats_all, old_period, cur)
            save_stats_all(stats_all)
        except Exception as e:
            log.error("rotate_quarter: обновление by_quarter: %s", e)

        fresh = _empty_stats_current(now_period)
        fresh[_PENDING_QUARTER_DELIVERY] = {
            "old_period": old_period,
            "new_period": now_period,
            "report_messages": report_msgs,
            "report_sent": False,
        }
        save_stats_current(fresh)

    return await _deliver_pending_quarter(bot, fresh)


# ═══════════════════════════════════════════════════════════════════
#  ОТПРАВКА ДЛИННЫХ СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════════

async def _send_long(bot: Bot, chat_id: int, text: str,
                     disable_preview: bool = False) -> bool:
    """
    Отправка с разбивкой по строкам если > 4000 символов (не рвём HTML-теги).

    disable_preview — отключить превью ссылок. По умолчанию False (превью есть):
    для большинства отчётов первая ссылка ведёт на осмысленный тайтл (топ
    квартала), и карточка уместна. True используем для /favs, где первая
    ссылка всегда одна и та же (первое избранное) и превью лишь мешает.
    """
    MAX = 4000
    try:
        if len(text) <= MAX:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=disable_preview)
            return True
        chunks: list[str] = []
        buf = ""
        for line in text.splitlines(keepends=True):
            if len(buf) + len(line) > MAX:
                if buf:
                    chunks.append(buf)
                buf = line
            else:
                buf += line
        if buf:
            chunks.append(buf)
        for chunk in chunks:
            await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=disable_preview)
            await asyncio.sleep(0.5)
        return True
    except Exception as e:
        log.error("_send_long: не удалось отправить (chat_id=%d): %s", chat_id, e)
        return False


# ═══════════════════════════════════════════════════════════════
#  /stats — МЕНЮ С КНОПКАМИ (расширяемое)
#
#  Чтобы добавить новый вид отчёта:
#    1. Написать async-builder, возвращающий list[str] (сообщения).
#    2. Добавить запись в _STATS_MENU: (callback_key, label, builder, row).
#  Всё остальное (клавиатура, обработка нажатия) работает автоматически.
#
#  row — номер ряда кнопки. Кнопки с одинаковым row встают в один ряд
#  (горизонтальная группа), с разным — в разные ряды (вертикаль).
# ═══════════════════════════════════════════════════════════════

async def _stats_report_current() -> list[str]:
    """Отчёт за текущий квартал."""
    stats_all = load_stats_all()
    cur = load_stats_current()
    return build_current_stats_messages(cur, stats_all)


async def _stats_report_all() -> list[str]:
    """Отчёт за всё время."""
    stats_all = load_stats_all()
    return build_stats_all_messages(stats_all)


async def _stats_report_favourites() -> list[str]:
    """Отчёт по избранному (любимое). Переиспользуем для /favs и для кнопки."""
    stats_all = load_stats_all()
    return build_favourites_messages(stats_all)


# Реестр вариантов отчёта. Кортеж: (ключ callback_data, подпись кнопки, builder, ряд)
# callback_data будет вида "stats:<ключ>".
_STATS_MENU: list[tuple[str, str, "callable", int]] = [
    ("current", "📆 За текущий квартал", _stats_report_current, 0),
    ("all",     "📚 За всё время",       _stats_report_all,     1),
]

# Быстрый доступ к builder по ключу
_STATS_BUILDERS: dict[str, "callable"] = {key: b for key, _, b, _ in _STATS_MENU}


def _stats_menu_kb() -> InlineKeyboardMarkup:
    """
    Строит клавиатуру меню из _STATS_MENU.
    Кнопки группируются по полю row: одинаковый row → один ряд.
    Порядок рядов — по возрастанию номера row.
    """
    rows: dict[int, list[InlineKeyboardButton]] = {}
    for key, label, _builder, row in _STATS_MENU:
        rows.setdefault(row, []).append(
            InlineKeyboardButton(text=label, callback_data=f"stats:{key}")
        )
    keyboard = [rows[r] for r in sorted(rows)]
    # Кнопка закрытия меню — отдельным рядом снизу. Это не вариант отчёта
    # (builder'а нет), поэтому не в _STATS_MENU: ключ "close" обрабатывается
    # в stats_menu_cb до lookup'а builder'а.
    keyboard.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="stats:close")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _send_stats_reports(bot: Bot, chat_id: int, msgs: list[str],
                              disable_preview: bool = False) -> None:
    """Отправляет список сообщений отчёта в чат (по сообщению на тему)."""
    for msg in msgs:
        if not msg or not msg.strip():
            continue
        await _send_long(bot, chat_id, msg, disable_preview=disable_preview)
        await asyncio.sleep(0.3)


async def cmd_stats(message: Message) -> None:
    """
    /stats      — показывает меню выбора отчёта (кнопки).
    /stats all  — сразу полный отчёт за всё время (быстрый путь, без меню).

    Доступна всем подписчикам. Не делает сетевых запросов (читает файлы) —
    мгновенно и не может упасть из-за недоступности API.
    """
    arg = ""
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1].strip().lower()

    # Быстрый путь: /stats all — сразу полный отчёт, минуя меню (совместимость)
    if arg in ("all", "всё", "все"):
        try:
            msgs = await _stats_report_all()
        except Exception as e:
            log.error("cmd_stats: формирование all: %s", e)
            await message.answer("⚠️ Не удалось сформировать статистику, попробуй позже.")
            return
        await _send_stats_reports(message.bot, message.chat.id, msgs)
        return

    # Иначе — показываем меню с кнопками. Отправляем ОТВЕТОМ на команду
    # (reply): так у меню появляется reply_to_message — само сообщение /stats,
    # и кнопка ❌ Закрыть сможет удалить заодно и команду.
    await message.reply(
        "📊 <b>Какую статистику показать?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_stats_menu_kb(),
    )


async def _cleanup_inline_menu(message: Message | None) -> None:
    """Удалить inline-меню и команду, на которую оно отвечает, если возможно."""
    if message is None:
        return
    try:
        await message.delete()
    except Exception as e:
        log.debug("_cleanup_inline_menu: не удалось удалить меню: %s", e)
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            log.debug("_cleanup_inline_menu: не удалось убрать кнопки меню: %s", e)

    command = getattr(message, "reply_to_message", None)
    if command is not None:
        try:
            await command.delete()
        except Exception as e:
            log.debug("_cleanup_inline_menu: не удалось удалить команду: %s", e)


async def _claim_inline_menu(message: Message) -> bool:
    """Одноразово забрать меню для действия, которое нельзя повторять."""
    try:
        await message.delete()
    except Exception as e:
        log.debug("_claim_inline_menu: меню уже обработано или недоступно: %s", e)
        return False
    return True


async def stats_menu_cb(callback: CallbackQuery) -> None:
    """
    Обработчик нажатия кнопки в меню /stats.
    callback_data: "stats:<ключ>" — ключ ищется в _STATS_BUILDERS.
    После выбора: убираем сообщение с кнопками и шлём выбранный отчёт.
    """
    data = callback.data or ""
    key = data.split(":", 1)[1] if ":" in data else ""

    # ❌ Закрыть — не вариант отчёта (builder'а нет): просто убираем меню.
    # Обрабатываем до lookup'а, иначе ключ ушёл бы в ветку 'Неизвестный вариант'.
    if key == "close":
        await callback.answer()
        await _cleanup_inline_menu(callback.message)
        return

    builder = _STATS_BUILDERS.get(key)

    if builder is None:
        await callback.answer("Неизвестный вариант.", show_alert=False)
        return

    await callback.answer()

    # Удаляем сообщение с кнопками — оно больше не нужно.
    # delete() может упасть (сообщение старое/уже удалено) — не критично.
    try:
        await callback.message.delete()
    except Exception as e:
        log.debug("stats_menu_cb: не удалось удалить меню: %s", e)
        # Фолбэк: хотя бы убрать кнопки, чтобы повторно не нажимали
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            log.debug("stats_menu_cb: не удалось убрать кнопки меню: %s", e)

    # Строим и шлём отчёт
    try:
        msgs = await builder()
    except Exception as e:
        log.error("stats_menu_cb: формирование (%s): %s", key, e)
        await callback.message.answer("⚠️ Не удалось сформировать статистику, попробуй позже.")
        return

    await _send_stats_reports(callback.message.bot, callback.message.chat.id, msgs)


def _pick_root_keyboard() -> InlineKeyboardMarkup:
    """Собрать неизменяемый выбор трёх пользовательских категорий."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Аниме", callback_data="pick:anime")],
        [InlineKeyboardButton(text="📚 Манга", callback_data="pick:manga")],
        [InlineKeyboardButton(text="📖 Ранобэ", callback_data="pick:ranobe")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="pick:cancel")],
    ])


def _pick_result_keyboard() -> InlineKeyboardMarkup:
    """Собрать действия над текущим результатом /pick."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Ещё вариант", callback_data="pick:more")],
        [InlineKeyboardButton(
            text="🎲 Что-нибудь совсем другое",
            callback_data="pick:contrast",
        )],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="pick:close")],
    ])


def _pick_category_label(category: str) -> str:
    """Вернуть читательское имя категории без раскрытия внутренних ключей."""
    return {
        PICK_CATEGORY_ANIME: "Аниме",
        PICK_CATEGORY_MANGA: "Манга",
        PICK_CATEGORY_RANOBE: "Ранобэ",
    }.get(category, "Тайтлы")


def _pick_freshness(updated_at: str | None) -> str:
    """Отформатировать локальную метку синхронизации или безопасный fallback."""
    parsed = _parse_iso_utc(updated_at)
    if parsed is None:
        return "время обновления неизвестно"
    return f"обновлено {parsed.strftime('%d.%m.%Y %H:%M')} UTC"


def _pick_unresolved_notice(count: int) -> str:
    """Объяснить исключение unknown-записей без угадывания их категории."""
    if count <= 0:
        return ""
    return (
        "\n\n⚠️ Запланированных тайтлов с пока не определённым типом: "
        f"<b>{count}</b>. Они временно не входят ни в мангу, ни в ранобэ."
    )


def _pick_root_text(snapshot_state: str, catalog, *, notice: str | None = None) -> str:
    """Показать состояние локальных данных и оставить выбор управляемым."""
    prefix = f"{notice}\n\n" if notice else ""
    if snapshot_state == STATS_ALL_MISSING:
        body = (
            "Локальная статистика ещё не готова. Дождись успешной полной "
            "синхронизации — бот не будет запускать её из этого меню."
        )
        freshness = ""
        unresolved = ""
    elif snapshot_state == STATS_ALL_INVALID or catalog is None:
        body = (
            "Локальные данные статистики сейчас недоступны или повреждены. "
            "Бот продолжит работать, а следующая успешная полная синхронизация "
            "попробует восстановить данные."
        )
        freshness = ""
        unresolved = ""
    else:
        body = "Выбери, что подобрать из запланированного списка."
        freshness = f"\n\n🕒 {_pick_freshness(catalog.updated_at)}."
        unresolved = _pick_unresolved_notice(catalog.unresolved_count)
    return f"{prefix}🎲 <b>Что выбрать дальше?</b>\n\n{body}{freshness}{unresolved}"


def _pick_clip(text: str, limit: int) -> str:
    """Ограничить повреждённое длинное поле до безопасного размера Telegram."""
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1].rstrip()}…"


def _pick_escape_clip(text: str, limit: int) -> str:
    """Экранировать текст и обрезать его, не разрывая HTML entity."""
    escaped = h(text)
    if len(escaped) <= limit:
        return escaped

    raw_parts: list[str] = []
    escaped_length = 0
    budget = max(0, limit - 1)
    for character in text:
        escaped_character = h(character)
        if escaped_length + len(escaped_character) > budget:
            break
        raw_parts.append(character)
        escaped_length += len(escaped_character)
    return f"{h(''.join(raw_parts).rstrip())}…"


def _pick_candidate_text(candidate: PickCandidate, catalog) -> str:
    """Безопасно отрендерить один локальный результат с нормализованной ссылкой."""
    title = _pick_escape_clip(candidate.title, 700)
    relative_url = _rel_url(candidate.url)
    if relative_url:
        relative_url = f"/{relative_url.lstrip('/')}"
        url = h(f"{SHIKI_BASE_URL.rstrip('/')}{relative_url}")
        rendered_title = (
            f'<a href="{url}">{title}</a>'
            if len(url) <= 1000
            else f"<b>{title}</b>"
        )
    else:
        rendered_title = f"<b>{title}</b>"

    details: list[str] = []
    if candidate.year is not None:
        details.append(f"🗓️ {candidate.year}")
    if candidate.genres:
        genres = ", ".join(_pick_clip(genre, 100) for genre in candidate.genres[:20])
        if len(candidate.genres) > 20:
            genres = f"{genres}, …"
        details.append(f"🎭 {_pick_escape_clip(genres, 1200)}")
    details_body = "\n".join(details)
    details_text = f"\n\n{details_body}" if details_body else ""
    unresolved = (
        ""
        if candidate.category == PICK_CATEGORY_ANIME
        else _pick_unresolved_notice(catalog.unresolved_count)
    )
    return (
        f"🎲 <b>Вариант — {_pick_category_label(candidate.category)}</b>\n\n"
        f"{rendered_title}{details_text}\n\n"
        f"🕒 {_pick_freshness(catalog.updated_at)}."
        f"{unresolved}"
    )


def _pick_candidate_to_state(candidate: PickCandidate) -> dict:
    """Сериализовать текущий anchor только во внутрипроцессный FSM."""
    return {
        "id": candidate.id,
        "category": candidate.category,
        "title": candidate.title,
        "url": candidate.url,
        "year": candidate.year,
        "genres": list(candidate.genres),
    }


def _pick_candidate_from_state(value: object) -> PickCandidate | None:
    """Защитно восстановить anchor из FSM без доверия к его форме."""
    if not isinstance(value, dict):
        return None
    candidate_id = value.get("id")
    category = value.get("category")
    title = value.get("title")
    url = value.get("url")
    year = value.get("year")
    genres = value.get("genres")
    if (
        not isinstance(candidate_id, str)
        or category not in _PICK_CATEGORIES
        or not isinstance(title, str)
        or not isinstance(url, str)
        or (year is not None and type(year) is not int)
        or not isinstance(genres, list)
        or any(not isinstance(genre, str) for genre in genres)
    ):
        return None
    return PickCandidate(
        id=candidate_id,
        category=category,
        title=title,
        url=url,
        year=year,
        genres=tuple(genres),
    )


def _load_pick_catalog() -> tuple[str, object | None]:
    """Прочитать только локальный snapshot и проверить picker-структуру."""
    snapshot = load_stats_all_snapshot()
    if snapshot.state != STATS_ALL_VALID:
        return snapshot.state, None
    catalog = build_pick_catalog(snapshot.data)
    if catalog is None:
        return STATS_ALL_INVALID, None
    return snapshot.state, catalog


async def _discard_previous_pick_menu(message: Message, state: FSMContext) -> None:
    """Инвалидировать прежнюю сессию и по возможности убрать её сообщения."""
    previous = await state.get_data()
    await state.clear()
    if previous.get("pick_menu_chat_id") != message.chat.id:
        return
    menu_id = previous.get("pick_menu_message_id")
    command_id = previous.get("pick_command_message_id")
    if type(menu_id) is int:
        await _safe_delete(message.bot, message.chat.id, menu_id)
    if type(command_id) is int:
        await _safe_delete(message.bot, message.chat.id, command_id)


def _pick_state_is_active(value: object) -> bool:
    """Учесть объект State и строковое значение реального FSM storage."""
    return value == PickStates.active or value == PickStates.active.state


async def cmd_pick(message: Message, state: FSMContext) -> None:
    """Открыть скрытый owner-only выбор из локального planned snapshot."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer("🚫 Эта команда только для владельца бота.")
        return

    current_state = await state.get_state()
    if current_state is not None and not _pick_state_is_active(current_state):
        await message.answer(
            "⚠️ Сначала заверши текущую операцию или отправь /cancel."
        )
        return
    if _pick_state_is_active(current_state):
        await _discard_previous_pick_menu(message, state)
    else:
        await state.clear()
    snapshot_state, catalog = _load_pick_catalog()
    try:
        menu = await message.reply(
            _pick_root_text(snapshot_state, catalog),
            parse_mode=ParseMode.HTML,
            reply_markup=_pick_root_keyboard(),
        )
    except Exception as e:
        log.warning("cmd_pick: не удалось открыть меню: %s", e)
        return
    await state.set_state(PickStates.active)
    await state.update_data(
        pick_menu_chat_id=message.chat.id,
        pick_menu_message_id=menu.message_id,
        pick_command_message_id=message.message_id,
        pick_category=None,
        pick_shown_ids=[],
        pick_anchor=None,
    )


async def _pick_callback_session(
    callback: CallbackQuery,
    state: FSMContext,
) -> dict | None:
    """Проверить владельца, сообщение и принадлежность текущей FSM-сессии."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return None
    if callback.message is None:
        await callback.answer("Меню устарело. Отправь /pick ещё раз.", show_alert=True)
        return None
    if await state.get_state() != PickStates.active:
        await callback.answer("Меню устарело. Отправь /pick ещё раз.", show_alert=True)
        return None
    data = await state.get_data()
    if (
        data.get("pick_menu_chat_id") != callback.message.chat.id
        or data.get("pick_menu_message_id") != callback.message.message_id
    ):
        await callback.answer("Это меню уже неактивно.", show_alert=True)
        return None
    return data


async def _pick_edit(
    callback: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> bool:
    """Изменить control message, не распространяя Telegram-сбой в FSM."""
    try:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return True
    except Exception as e:
        log.debug("pick: не удалось обновить меню: %s", e)
        await callback.answer("Не удалось обновить меню. Попробуй ещё раз.", show_alert=True)
        return False


async def _pick_show_category(
    callback: CallbackQuery,
    state: FSMContext,
    data: dict,
    category: str,
    *,
    contrast: bool = False,
) -> None:
    """Выбрать и атомарно отразить следующий результат текущей категории."""
    snapshot_state, catalog = _load_pick_catalog()
    if snapshot_state != STATS_ALL_VALID or catalog is None:
        if await _pick_edit(
            callback,
            _pick_root_text(snapshot_state, catalog),
            _pick_root_keyboard(),
        ):
            await state.update_data(
                pick_category=None,
                pick_shown_ids=[],
                pick_anchor=None,
            )
            await callback.answer()
        return

    candidates = catalog.candidates_for(category)
    if not candidates:
        notice = (
            "📭 В последней локальной синхронизации нет запланированных "
            f"вариантов в категории «{_pick_category_label(category)}»."
        )
        if await _pick_edit(
            callback,
            _pick_root_text(snapshot_state, catalog, notice=notice),
            _pick_root_keyboard(),
        ):
            await state.update_data(
                pick_category=None,
                pick_shown_ids=[],
                pick_anchor=None,
            )
            await callback.answer()
        return

    shown_ids = data.get("pick_shown_ids")
    if not isinstance(shown_ids, list):
        shown_ids = []
    anchor = _pick_candidate_from_state(data.get("pick_anchor"))
    if contrast:
        if anchor is None or anchor.category != category:
            await callback.answer("Текущий вариант устарел. Выбери категорию заново.", show_alert=True)
            return
        selection = select_contrast_pick_candidate(candidates, anchor, shown_ids)
    else:
        selection = select_pick_candidate(candidates, shown_ids)
    if selection.candidate is None:
        await callback.answer("Подходящих вариантов пока нет.", show_alert=True)
        return

    if not await _pick_edit(
        callback,
        _pick_candidate_text(selection.candidate, catalog),
        _pick_result_keyboard(),
    ):
        return
    await state.update_data(
        pick_category=category,
        pick_shown_ids=sorted(selection.shown_ids),
        pick_anchor=_pick_candidate_to_state(selection.candidate),
    )
    await callback.answer()


async def pick_menu_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Оркестрировать все callback пути текущего owner-only меню /pick."""
    data = await _pick_callback_session(callback, state)
    if data is None:
        return
    raw_data = callback.data or ""
    action = raw_data.removeprefix(_PICK_CALLBACK_PREFIX)
    if action in {"cancel", "close"}:
        await state.clear()
        await callback.answer()
        await _cleanup_inline_menu(callback.message)
        return
    if action in _PICK_CATEGORIES:
        await _pick_show_category(callback, state, data, action)
        return
    if action in {"more", "contrast"}:
        category = data.get("pick_category")
        if category not in _PICK_CATEGORIES:
            await callback.answer("Сначала выбери категорию.", show_alert=True)
            return
        await _pick_show_category(
            callback,
            state,
            data,
            category,
            contrast=action == "contrast",
        )
        return
    await callback.answer("Неизвестное действие.", show_alert=True)


async def cmd_favs(message: Message) -> None:
    """
    /favs — показывает избранное (любимое аниме и манга).
    Одна категория, выбирать нечего — показываем сразу, без меню.
    Доступна всем. Не делает сетевых запросов (читает файлы).
    """
    try:
        msgs = await _stats_report_favourites()
    except Exception as e:
        log.error("cmd_favs: формирование: %s", e)
        await message.answer("⚠️ Не удалось загрузить избранное, попробуй позже.")
        return
    await _send_stats_reports(message.bot, message.chat.id, msgs, disable_preview=True)

# ═══════════════════════════════════════════════════════════════
#  ОСНОВНАЯ ЛОГИКА
# ═══════════════════════════════════════════════════════════════


# Sentinel «favourites не передан» (прямой/тестовый вызов → фетчим сами) vs
# явный None из цикла («уже пытались, недоступно» → НЕ рефетчим, деградируем).
_FAV_UNSET = object()


async def check_and_notify_favourites(
    bot: Bot, seen: set[str], favourites=_FAV_UNSET,
) -> tuple[set[str], bool]:
    """
    Проверяем избранное:
    0. favourites: если передан уже скачанный ответ /favourites (цикл тянет его
       ОДИН раз и делит между уведомлениями и ресинком) — используем его и НЕ
       ходим в сеть. favourites=_FAV_UNSET (не передан, прямой вызов) — фетчим
       сами. Явный None («в этом цикле избранное недоступно») — пропускаем цикл,
       БЕЗ повторного фетча.
    1. Загружаем текущий список с Shikimori
    2. Находим новые элементы (которых нет в seen)
    3. Отправляем уведомления и обновляем seen
    4. Если что-то новое нашли — пересобираем stats["favourites"] из УЖЕ
       скачанного списка (без повторного запроса к API), чтобы /favs показывал
       свежее сразу, не дожидаясь 6-часового ресинка.

    Ключ в seen: "{category}_{id}", например "animes_5114".
    Возвращает (seen, found_new).
    """
    if favourites is _FAV_UNSET:
        async with aiohttp.ClientSession() as session:
            favourites = await fetch_favourites(session)

    if favourites is None:
        log.info("Избранное недоступно — пропускаем цикл (без повторного фетча).")
        return seen, False

    # baseline пуст (первый запуск либо стартовая инициализация не прошла
    # из-за 429/сети) — молча фиксируем текущее избранное как baseline,
    # НИЧЕГО не шлём.
    if not seen:
        for category in _FAV_CATEGORIES:
            for item in (favourites.get(category) or []):
                if item.get("id") is not None:
                    seen.add(f"{category}_{item['id']}")
        save_seen_favourites(seen)
        log.info("Избранное: baseline инициализирован в цикле (%d), без отправки.", len(seen))
        return seen, False

    # Архив для джойна ссылок в уведомлениях: Favourites API отдаёт url=null,
    # поэтому тянем ссылку из titles{} по id (как в /favs). Чтение из кэша —
    # дёшево; запись (save_stats_all) только если ниже нашлось новое.
    stats = load_stats_all()
    # API-категория → ключ titles для джойна ссылки (остальные ссылки не имеют)
    url_join_media = {"animes": "anime", "mangas": "manga", "ranobe": "manga"}

    found_new = False
    # ID людей индустрии, по которым уже отправили уведомление в этом цикле —
    # чтобы один человек в нескольких ролях не дал дубль сообщений.
    notified_people: set[str] = set()

    for category in _FAV_CATEGORIES:
        items = favourites.get(category) or []
        for item in items:
            item_id = item.get("id")
            if item_id is None:
                continue
            key = f"{category}_{item_id}"
            if key in seen:
                continue

            # Новый элемент в избранном. seen-ключ роли фиксируем всегда (даже
            # если уведомление ниже подавим как дубль), иначе он будет считаться
            # «новым» в каждом следующем цикле.
            seen.add(key)
            found_new = True

            # Дедуп слитого блока «Люди индустрии»: один человек может лежать
            # сразу в нескольких ролях (seyu + producers) — шлём одно
            # уведомление на person id за цикл.
            if category in _INDUSTRY_CATEGORIES:
                if str(item_id) in notified_people:
                    continue
                notified_people.add(str(item_id))

            log.info("Новое в избранном: %s (id=%s)", category, item_id)

            # Подтягиваем ссылку из архива (баг: API отдаёт url=null).
            # Если тайтла нет в titles{} (или это персонаж/человек) — ссылки
            # не будет, и это ок (graceful: жирный текст без ссылки).
            media_key = url_join_media.get(category)
            if media_key:
                rec = stats.get(media_key, {}).get("titles", {}).get(str(item_id))
                rec_url = (rec or {}).get("url")
                if rec_url:
                    item = {**item, "url": rec_url}  # копия — не мутируем исходный

            text = build_favourite_message(category, item)
            await send_to_all_chats(bot, text)
            await asyncio.sleep(1)

    if found_new:
        # Пересобираем stats["favourites"] из уже скачанного списка — /favs
        # станет свежим в этом же цикле, без второго запроса к API.
        try:
            stats = await _collect_favourites(None, stats, fav=favourites)
            save_stats_all(stats)
        except ProfilePrivacyError:
            raise
        except Exception as e:
            log.error("check_and_notify_favourites: не удалось обновить stats_all: %s", e)
    else:
        log.info("Изменений в избранном нет.")

    save_seen_favourites(seen)
    return seen, found_new


async def _unsubscribe_blocked(to_remove: list[int]) -> None:
    """Удаляет заблокировавших из subs и сохраняет актуальный список."""
    if not to_remove:
        return
    async with restorable_state_transaction():
        subs = load_subscribers()
        removed = 0
        for cid in to_remove:
            if subs.pop(cid, None) is not None:
                removed += 1
        if removed:
            save_subscribers(subs)
    log.info("Отписано %d пользователей, заблокировавших бота.", removed)


async def send_to_all_chats(bot: Bot, text: str) -> None:
    """
    Отправляем одно сообщение всем подписчикам.
    Список берём из файла каждый раз — чтобы подхватывать новых подписчиков
    без перезапуска бота.
    Если конкретный chat_id недоступен (пользователь заблокировал бота) —
    автоматически отписываем его и продолжаем рассылку остальным.
    """
    subs = load_subscribers()
    if not subs:
        log.info("Подписчиков нет — некому слать.")
        return

    # Список тех, кого нужно отписать (заблокировали бота)
    to_remove: list[int] = []

    for chat_id, name in subs.items():
        try:
            await send_with_retry(
                lambda: bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
            )
            log.info("  → Отправлено подписчику %s (chat_id=%d)", name, chat_id)
        except Exception as e:
            if _is_blocked_error(e):
                log.warning("  ✗ %s (chat_id=%d) заблокировал бота — отписываем.", name, chat_id)
                to_remove.append(chat_id)
            else:
                log.error("  ✗ Не удалось отправить %s (chat_id=%d): %s", name, chat_id, e)
        # Небольшая пауза между отправками — не триггерим flood control
        await asyncio.sleep(0.3)

    await _unsubscribe_blocked(to_remove)


async def _fetch_history_catchup(
    session: aiohttp.ClientSession,
    seen_ids: set[int],
) -> list[dict] | None:
    """Собирает пропущенную историю до известного ID или конца выдачи."""
    entries_by_id: dict[int, dict] = {}

    for page in range(1, _HISTORY_CATCHUP_MAX_PAGES + 1):
        page_entries = await fetch_history(session, page=page)
        if page_entries is None:
            log.warning(
                "История: страница %d не загрузилась, catch-up отменён без обновления seen_ids.",
                page,
            )
            return None

        try:
            page_ids = {entry["id"] for entry in page_entries}
            for entry in page_entries:
                entries_by_id.setdefault(entry["id"], entry)
        except (KeyError, TypeError) as e:
            log.warning(
                "История: некорректная страница %d (%s), catch-up отменён без обновления seen_ids.",
                page,
                e,
            )
            return None

        if page_ids & seen_ids:
            return list(entries_by_id.values())

        # API Shikimori читает limit + 1 запись как признак следующей страницы.
        # При limit=50 короткая выдача содержит меньше 51 записи и означает конец.
        if len(page_entries) < HISTORY_PAGE_LIMIT + 1:
            return list(entries_by_id.values())

    log.warning(
        "История: за %d страниц не найдена известная граница; "
        "catch-up отменён без обновления seen_ids.",
        _HISTORY_CATCHUP_MAX_PAGES,
    )
    return None


async def check_and_notify(bot: Bot, seen_ids: set[int], cur: dict) -> tuple[set[int], dict]:
    """
    Главная функция проверки:
    1. Загружаем историю с Shikimori
    2. Фильтруем новые записи (которых нет в seen_ids)
    3. Для каждой новой — формируем сообщение и шлём во все чаты
    4. Обновляем seen_ids и возвращаем его
    5. Параллельно фиксируем значимые события в cur (статистика квартала)
    """
    async with aiohttp.ClientSession() as session:
        if seen_ids:
            entries = await _fetch_history_catchup(session, seen_ids)
        else:
            # Первый baseline намеренно ограничен одной страницей.
            entries = await fetch_history(session)

    if entries is None:
        log.info("Запрос истории не удался — пропускаем цикл.")
        return seen_ids, load_stats_current()

    # baseline пуст (первый запуск либо стартовая инициализация не прошла
    # из-за 429/сети) — молча фиксируем текущую историю как baseline и
    # НИЧЕГО не шлём. Провал старта становится безобидной доинициализацией.
    if not seen_ids:
        seen_ids = {e["id"] for e in entries}
        save_seen_ids(seen_ids)
        log.info("История: baseline инициализирован в цикле (%d ID), без отправки.", len(seen_ids))
        return seen_ids, load_stats_current()

    new_entries = [e for e in entries if e["id"] not in seen_ids]

    if not new_entries:
        log.info("Новых записей нет.")
        return seen_ids, load_stats_current()

    log.info("Найдено новых записей: %d", len(new_entries))

    # Сортируем по ID: от старых к новым — хронологический порядок сообщений
    new_entries.sort(key=lambda e: e["id"])

    state_updates: list[tuple[dict, str, str, int | None]] = []
    for entry in new_entries:
        entry_id   = entry["id"]
        media_type, kind = get_media_info(entry)

        # ── Фильтр по виду (kind) ──────────────────────────────────────
        # ID запоминаем в любом случае — чтобы не проверять повторно.
        # Сообщение шлём только если вид «значимый».
        seen_ids.add(entry_id)

        if not is_relevant(media_type, kind):
            log.info(
                "Пропускаем entry id=%d (%s / kind=%s) — не входит в список значимых.",
                entry_id, media_type, kind or "unknown",
            )
            continue
        # ──────────────────────────────────────────────────────────────

        log.info(
            "Обрабатываем entry id=%d (%s / kind=%s): %s",
            entry_id, media_type, kind, entry.get("description", ""),
        )

        # Готовим дельту квартальной статистики независимо от результата отправки.
        description = entry.get("description", "") or ""
        event_type  = classify_event(description)
        if event_type == "ignored":
            log.info(
                "Пропускаем служебную запись истории entry id=%d: %r",
                entry_id,
                clean_description(description),
            )
            continue
        if event_type == "score_removed":
            state_updates.append((entry, event_type, media_type, None))
            log.info(
                "Отмена оценки учтена без уведомления entry id=%d: %r",
                entry_id,
                clean_description(description),
            )
            continue
        if event_type == "unknown":
            log.warning(
                "Неизвестное описание истории entry id=%d: %r",
                entry_id,
                clean_description(description),
            )
        else:
            if event_type in ("completed", "score_set"):
                score = extract_score(description)
            elif event_type == "score_changed":
                chg = extract_score_change(description)
                score = chg[1] if chg else None
            else:
                score = None
            state_updates.append((entry, event_type, media_type, score))

        text = build_message(entry)
        await send_to_all_chats(bot, text)

        # Пауза между разными событиями — не спамим Telegram
        await asyncio.sleep(1)

    save_seen_ids(seen_ids)
    async with restorable_state_transaction():
        cur = load_stats_current()
        for entry, event_type, media_type, score in state_updates:
            cur = record_current_event(cur, entry, event_type, media_type, score)
        if state_updates:
            save_stats_current(cur)
    return seen_ids, cur


def _should_full_sync(last_full_sync: float | None, now: float, interval: float) -> bool:
    """Пора ли пересинкивать stats_all: ещё ни разу успешно в этой сессии
    (last_full_sync is None ⇒ ретраим каждый цикл, пока не выйдет) либо с
    последнего успешного синка прошло больше interval секунд."""
    return last_full_sync is None or (now - last_full_sync) >= interval


async def _notify_profile_privacy_owner(
    bot: Bot,
    last_notify_at: float | None,
) -> float | None:
    """Шлёт owner-only диагностику с общим интервалом фоновых ошибок."""
    now = time.monotonic()
    if (
        last_notify_at is not None
        and now - last_notify_at < ERROR_NOTIFY_INTERVAL
    ):
        return last_notify_at

    try:
        await bot.send_message(
            OWNER_ID,
            _profile_privacy_owner_text(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as notify_error:
        log.exception(
            "Не удалось отправить владельцу диагностику приватности: %s",
            notify_error,
        )
    return now


async def polling_loop(bot: Bot) -> None:
    """
    Бесконечный цикл проверки каждые CHECK_INTERVAL секунд.

    Первый запуск (seen_ids.json не существует):
      — бот молча запоминает все текущие ID из истории и избранного
      — сообщения НЕ отправляются (не спамим историей за последние месяцы)
      — с этого момента бот следит только за НОВЫМИ событиями
    """
    seen_ids  = load_seen_ids()
    seen_favs = load_seen_favourites()
    cur = load_stats_current()
    log.info(
        "Бот запущен. Отображаемое имя: %s | Подписчиков: %d | Виденных ID: %d | Интервал: %d сек.",
        DISPLAY_NAME, len(load_subscribers()), len(seen_ids), CHECK_INTERVAL,
    )

    last_error_notify_at: float | None = None
    privacy_error: ProfilePrivacyError | None = None
    pending_seen_ids: set[int] | None = None
    pending_seen_favs: set[str] | None = None
    stats_all = load_stats_all()
    synced_ok = False

    # boot-throttle: одна общая ClientSession на все стартовые фетчи (анти-429),
    # фиксированные паузы между фазами; избранное тянем ОДИН раз и переиспользуем.
    async with aiohttp.ClientSession() as session:
        try:
            if not seen_ids:
                log.info("Первый запуск — инициализируем историю без отправки сообщений.")
                entries = await fetch_history(session)
                if entries is None:
                    log.warning(
                        "Не удалось получить историю при инициализации — "
                        "пропускаем, повторим на следующем цикле."
                    )
                else:
                    pending_seen_ids = {e["id"] for e in entries}
            await asyncio.sleep(BOOT_PHASE_DELAY)

            # Избранное фетчим ОДИН раз: для baseline и sync (fav=).
            favourites = await fetch_favourites(session)
            if not seen_favs:
                log.info("Инициализируем избранное без отправки сообщений.")
                if favourites is None:
                    log.warning(
                        "Не удалось получить избранное при инициализации — "
                        "пропускаем, повторим на следующем цикле."
                    )
                else:
                    pending_seen_favs = {
                        f"{category}_{item['id']}"
                        for category in _FAV_CATEGORIES
                        for item in (favourites.get(category) or [])
                        if item.get("id") is not None
                    }
            await asyncio.sleep(BOOT_PHASE_DELAY)

            # Сохранять baseline можно только после этой последней сетевой фазы:
            # privacy failure одного endpoint отменяет весь стартовый результат.
            log.info("Синхронизируем статистику за всё время (stats_all)...")
            try:
                stats_all, synced_ok = await sync_stats_all(
                    session=session,
                    fav=favourites,
                )
            except ProfilePrivacyError:
                raise
            except Exception as e:
                log.exception("Не удалось синхронизировать stats_all при старте: %s", e)
        except ProfilePrivacyError as error:
            privacy_error = error
            log.warning(
                "На старте обнаружен закрытый профиль (%s); "
                "baseline и статистика не продвигаются.",
                error.endpoint,
            )

    if privacy_error is None:
        if pending_seen_ids is not None:
            seen_ids = pending_seen_ids
            save_seen_ids(seen_ids)
            log.info("Инициализировано %d ID истории.", len(seen_ids))
        if pending_seen_favs is not None:
            seen_favs = pending_seen_favs
            save_seen_favourites(seen_favs)
            log.info("Инициализировано %d записей избранного.", len(seen_favs))
    else:
        last_error_notify_at = await _notify_profile_privacy_owner(
            bot,
            last_error_notify_at,
        )

    # Метка последнего успешного полного синка (monotonic). None ⇒ в этой
    # сессии ещё не синкнулись успешно — цикл будет ретраить каждый раз.
    last_full_sync = time.monotonic() if synced_ok else None
    if synced_ok:
        mark_full_sync_success()

    # Если квартал успел смениться пока бот не работал — ротируем и шлём отчёт.
    if privacy_error is None:
        try:
            cur = await rotate_quarter_if_needed(bot, cur, stats_all, resync=False)
        except Exception as e:
            log.exception("Ошибка ротации квартала при старте: %s", e)

    while True:
        try:
            log.info("Проверяем историю и избранное...")
            seen_ids, cur = await check_and_notify(bot, seen_ids, cur)
            # Избранное фетчим ОДИН раз за цикл и переиспользуем — в уведомлениях
            # и в ресинке stats_all (fav=), как на старте. Дедуп убирает второй
            # фетч избранного внутри sync_stats_all → на цикл 1 запрос вместо 2.
            async with aiohttp.ClientSession() as fav_session:
                cycle_favourites = await fetch_favourites(fav_session)
            seen_favs, _  = await check_and_notify_favourites(
                bot, seen_favs, favourites=cycle_favourites,
            )

            # Периодический (и ретрай-после-неудачного-старта) ресинк stats_all,
            # чтобы сбой одного запроса не оставлял статистику протухшей/пустой
            # до перезапуска. Дёшево: list_export ×2 + избранное, meta — только
            # по новым id. save_stats_all обновляет кэш, ротация ниже видит свежее.
            if _should_full_sync(last_full_sync, time.monotonic(), FULL_SYNC_INTERVAL):
                try:
                    _, synced_ok = await sync_stats_all(fav=cycle_favourites)
                    if synced_ok:
                        last_full_sync = time.monotonic()
                        mark_full_sync_success()
                    else:
                        log.warning("stats_all: ресинк не удался (429?), повторим в следующем цикле.")
                except ProfilePrivacyError:
                    raise
                except Exception as e:
                    log.exception("stats_all: ресинк в цикле упал: %s", e)

            # Проверяем смену квартала (раз в цикл, дёшево).
            # Внутри — защита last_report_sent от повторной отправки.
            cur = await rotate_quarter_if_needed(bot, cur, load_stats_all())

            # Еженедельный авто-бэкап состояния (по last_backup_at в stats_current).
            cur = await _weekly_backup_if_due(bot, cur)

            heartbeat()  # отметить успешный цикл для healthcheck-watchdog
            log.info("Следующая проверка через %d мин.", CHECK_INTERVAL // 60)
        except asyncio.CancelledError:
            # Штатная отмена задачи — пробрасываем, не глушим
            raise
        except ProfilePrivacyError as error:
            log.warning(
                "Закрытый профиль обнаружен в фоновом цикле (%s); "
                "состояние сохранено без изменений.",
                error.endpoint,
            )
            last_error_notify_at = await _notify_profile_privacy_owner(
                bot,
                last_error_notify_at,
            )
            heartbeat()
        except Exception as e:
            log.exception("Непредвиденная ошибка в цикле проверки, продолжаем: %s", e)

            now = time.monotonic()
            if (
                last_error_notify_at is None
                or now - last_error_notify_at >= ERROR_NOTIFY_INTERVAL
            ):
                last_error_notify_at = now

                try:
                    error_text = str(e)
                    if len(error_text) > 1000:
                        error_text = error_text[:1000] + "..."

                    await bot.send_message(
                        OWNER_ID,
                        "⚠️ ShikiUpdatesBot: ошибка в цикле проверки.\n\n"
                        f"Тип: {type(e).__name__}\n"
                        f"Текст: {error_text}\n\n"
                        "Цикл не остановлен, следующая проверка будет позже.",
                    )
                except Exception as notify_error:
                    log.exception(
                        "Не удалось отправить уведомление владельцу об ошибке: %s",
                        notify_error,
                    )
        await asyncio.sleep(CHECK_INTERVAL)


# ───────────────────────────────────────────────────────────────
#  ЗАПУСК ФОНОВОГО ЦИКЛА + ПРОБА ДОСТУПНОСТИ ВЛАДЕЛЬЦА (owner-gate)
# ───────────────────────────────────────────────────────────────

_polling_task: "asyncio.Task | None" = None


def _on_polling_done(task: "asyncio.Task") -> None:
    """Логируем, если polling_loop завершился неожиданно."""
    if task is _polling_task:
        set_polling_active(False)
    if task.cancelled():
        log.warning("polling_loop: задача отменена.")
    elif exc := task.exception():
        log.critical(
            "polling_loop завершился с необработанной ошибкой: %s", exc, exc_info=exc,
        )


def start_polling_loop(bot: Bot) -> bool:
    """Идемпотентно запускает фоновый цикл. True — запустили сейчас, False — уже жив."""
    global _polling_task
    if _polling_task is not None and not _polling_task.done():
        return False
    try:
        _polling_task = asyncio.create_task(polling_loop(bot))
    except Exception:
        set_polling_active(False)
        raise
    set_polling_active(True)
    _polling_task.add_done_callback(_on_polling_done)
    return True


def _build_startup_text() -> str:
    """Стартовый health-снапшот для owner-gate. При любой ошибке сборки —
    голое '🟢 Бот запущен': проба доставки (тест аварийного канала + гейт
    фонового цикла) не должна падать из-за кривого таймстемпа. Все источники —
    локальные загрузчики (без сети); времена берутся от прошлого запуска, их
    протухлость и есть диагностика."""
    try:
        stats_all = load_stats_all()
        cur = load_stats_current()
        return build_startup_snapshot(
            display_name=DISPLAY_NAME,
            shiki_user=SHIKI_USER,
            check_interval_sec=CHECK_INTERVAL,
            subscriber_count=len(load_subscribers()),
            seen_ids_count=len(load_seen_ids()),
            seen_favs_count=len(load_seen_favourites()),
            stats_updated_at=stats_all.get("updated_at"),
            last_backup_at=cur.get("last_backup_at"),
        )
    except Exception as e:
        log.warning("Не удалось собрать стартовый снапшот, шлём голый пинг: %s", e)
        return "🟢 Бот запущен"


async def probe_owner_and_start(bot: Bot) -> None:
    """Проверка доступности владельца. Шлёт '🟢 Бот запущен' — пробу аварийного
    канала + легитимный сигнал рестарта (без дебаунса). Доставилось → стартуем
    фоновый цикл; не доставилось (владелец заблокировал бота / TelegramForbiddenError
    и т.п.) → WARNING, цикл НЕ стартуем. Апдейт-поллинг (dp.start_polling) жив всегда:
    бот отвечает на команды, владелец /start добудит цикл без рестарта контейнера."""
    try:
        await bot.send_message(OWNER_ID, _build_startup_text(), parse_mode=None)
    except Exception as e:
        log.warning(
            "Владелец недоступен при старте (%s: %s) — фоновый цикл не запущен. "
            "Разбудить: владелец шлёт /start.", type(e).__name__, e,
        )
        return
    if start_polling_loop(bot):
        log.info("Владелец на связи — фоновый цикл запущен.")


# ═══════════════════════════════════════════════════════════════
#  РЕЗЕРВНОЕ КОПИРОВАНИЕ (/backup) — ЭКСПОРТ / ИМПОРТ + АВТО-БЭКАП
# ═══════════════════════════════════════════════════════════════
#
#  Экспорт = zip всего DATA_DIR (минус *.tmp-огрызки _atomic_write).
#  Импорт  = по белому списку (список блокировок, subscribers, stats_current,
#            update_state, quarters/*);
#            всё прочее в архиве намеренно отбрасывается — seen_ids,
#            seen_favourites и stats_all регенерируются сами, тащить их
#            обратно незачем. Асимметрия экспорт(всё)/импорт(бел.список)
#            сознательная: архив — и страховка состояния, и зонд внутрь
#            эфемерного контейнера (apply.build без тома на /data).
#  Доставка — всегда владельцу (OWNER_ID); в subscribers лежат chat_id.


# Бэкап при остановке (SIGTERM-триггер): дополняет событийные бэкапы, ловит
# «последнюю милю» перед смертью контейнера на редеплое. Дебаунс — не слать,
# если только что уже бэкапили; короткий таймаут — лучше не успеть, чем зависнуть.

# Файлы DATA_DIR, которые восстанавливаем при импорте (см. асимметрию выше).
# Каталог снапшотов кварталов: разрешаем quarters/<period>.json.


class BackupStates(StatesGroup):
    waiting_import_file = State()   # ждём .zip-архив от владельца


class FactsStates(StatesGroup):
    waiting_upload_file = State()   # ждём facts.json от владельца
    waiting_apply_confirmation = State()  # preview уже показан


def _backup_menu_kb() -> InlineKeyboardMarkup:
    """Инлайн-меню /backup: экспорт и импорт."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📤 Экспорт", callback_data="backup:export"),
            InlineKeyboardButton(text="📥 Импорт",  callback_data="backup:import"),
        ],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="backup:close")],
    ])


async def cmd_backup(message: Message) -> None:
    """Меню резервного копирования (только для владельца)."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer("🚫 Эта команда только для владельца бота.")
        return
    # Отправляем ОТВЕТОМ на команду (reply): у меню появляется reply_to_message
    # = само сообщение /backup, и кнопка ❌ Закрыть удалит заодно и команду.
    await message.reply(
        "💾 <b>Резервное копирование</b>\n\n"
        "📤 <b>Экспорт</b> — пришлю zip-архив всего состояния "
        "(подписчики, дополнительные факты, статистика, кварталы).\n"
        "📥 <b>Импорт</b> — восстановлю из архива список блокировок, подписчиков, "
        "дополнительные факты, сведения о доступных обновлениях и данные "
        "текущего и завершённых кварталов.",
        reply_markup=_backup_menu_kb(),
        parse_mode=ParseMode.HTML,
    )


async def backup_export_cb(callback: CallbackQuery) -> None:
    """Кнопка «Экспорт» — собираем и шлём архив, меню убираем."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return
    await callback.answer("Собираю архив...")
    bot, chat_id = callback.message.bot, callback.message.chat.id
    await _safe_delete(bot, chat_id, callback.message.message_id)
    caption = (f"📤 Экспорт состояния.\n"
               f"Подписчиков: <b>{len(load_subscribers())}</b>\n\n{BACKUP_TAG}")
    if not await send_backup(bot, caption):
        await bot.send_message(chat_id, "❌ Не удалось собрать/отправить архив — см. логи.")


async def backup_import_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Импорт» — входим в FSM ожидания .zip-файла."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(BackupStates.waiting_import_file)
    prompt = await callback.message.edit_text(
        "📥 Пришли <b>.zip</b>-архив бэкапа (как файл-документ).\n\n"
        "Возьму из него только нужное — список блокировок, подписчиков, дополнительные "
        "факты, сведения о доступных обновлениях и данные текущего и завершённых "
        "кварталов. Лишнее в архиве не помешает, спокойно пропущу.\n\n/cancel — отмена",
        parse_mode=ParseMode.HTML,
    )
    await state.update_data(prompt_msg_id=prompt.message_id)


async def backup_close_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «❌ Закрыть» — убираем меню и саму команду /backup. Тот же
    отработанный паттерн, что и ❌ Закрыть в /stats: меню отправлено reply'ем
    на команду, поэтому reply_to_message = сообщение /backup, и его тоже чистим."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return
    await state.clear()   # защитно: Закрыть снимает любое повисшее FSM-состояние
    await callback.answer()
    await _cleanup_inline_menu(callback.message)


async def backup_receive(message: Message, state: FSMContext) -> None:
    """Принять .zip от владельца, восстановить по белому списку, отчитаться."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
    doc = message.document
    if not doc or not (doc.file_name or "").lower().endswith(".zip"):
        await message.answer("📎 Жду <b>.zip</b>-архив бэкапа. Или /cancel.",
                             parse_mode=ParseMode.HTML)
        return
    if not isinstance(doc.file_size, int) or doc.file_size > IMPORT_DOCUMENT_MAX_BYTES:
        await message.answer(
            "📦 Архив должен иметь известный размер не больше <b>20 МиБ</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    fsm = await state.get_data()
    await state.clear()
    prompt_id = fsm.get("prompt_msg_id")
    if prompt_id:
        await _safe_delete(message.bot, message.chat.id, prompt_id)

    try:
        buf = io.BytesIO()
        await message.bot.download(doc, destination=buf)
        raw = buf.getvalue()
    except Exception as e:
        log.warning("backup_receive: не удалось скачать архив: %s", e)
        await message.answer(
            "❌ Не удалось скачать архив. Попробуй ещё раз.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        result = await restore_backup_zip(raw)
    except ValueError as e:
        log.warning("backup_receive: архив не восстановлен: %s", e)
        await message.answer(
            "❌ Архив не восстановлен. Проверь формат и целостность файла.",
            parse_mode=ParseMode.HTML,
        )
        return

    restored, skipped = result["restored"], result["skipped"]
    lines = [f"✅ Восстановлено файлов: <b>{len(restored)}</b>"]
    lines += [f"  • <code>{h(n)}</code>" for n in restored]
    if "subscribers.json" in restored:
        lines.append(f"\n👥 Подписчиков теперь: <b>{len(load_subscribers())}</b>")
    if "update_state.json" in restored:
        lines.append("\n🔄 Сведения о доступных обновлениях восстановлены.")
    if "blocked_users.json" in restored:
        lines.append("\n🚫 Список блокировок восстановлен.")
    if "facts.json" in restored:
        lines.append("\n💡 Дополнительный банк фактов восстановлен и активирован.")
    if skipped:
        lines.append(f"\n⏭️ Пропущено (вне белого списка/битые): {len(skipped)}")
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════════
#  УПРАВЛЕНИЕ ДОПОЛНИТЕЛЬНЫМ БАНКОМ ФАКТОВ
# ═══════════════════════════════════════════════════════════════


def _facts_status_text(
    snapshot: FactBankSnapshot,
    *,
    notice: str | None = None,
) -> str:
    """Показать владельцу активный состав и состояние внешнего файла."""
    if snapshot.file_state == FACT_FILE_MISSING:
        file_state = "⚪ файл ещё не создан; работает базовый банк"
    elif snapshot.file_state == FACT_FILE_INVALID:
        file_state = "🔴 файл не читается или повреждён; работает базовый банк"
    else:
        file_state = "🟢 файл корректен"
    version = h(snapshot.bank_version) if snapshot.bank_version else "—"
    prefix = f"{notice}\n\n" if notice else ""
    return (
        f"{prefix}💡 <b>Банк фактов</b>\n\n"
        f"Встроенных: <b>{len(snapshot.base_facts)}</b>\n"
        f"Дополнительных: <b>{len(snapshot.additional_facts)}</b>\n"
        f"Всего активно: <b>{len(snapshot.facts)}</b>\n"
        f"Версия дополнения: <code>{version}</code>\n\n"
        f"{file_state}"
    )


def _facts_menu_keyboard(snapshot: FactBankSnapshot) -> InlineKeyboardMarkup:
    """Собрать скрытое owner-only меню без кнопки очистки пустого банка."""
    rows = [[InlineKeyboardButton(
        text="📤 Загрузить дополнительные",
        callback_data="facts:upload",
    )]]
    if snapshot.additional_facts:
        rows.append([InlineKeyboardButton(
            text="📥 Скачать дополнительные",
            callback_data="facts:download",
        )])
        rows.append([InlineKeyboardButton(
            text="🗑 Очистить дополнительные",
            callback_data=(
                f"{FACTS_ASK_CLEAR_CALLBACK_PREFIX}{snapshot.revision}"
            ),
        )])
    else:
        rows.append([InlineKeyboardButton(
            text="📄 Скачать пример facts.json",
            callback_data="facts:example",
        )])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="facts:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe_facts_edit(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Обновить owner-menu, не превращая Telegram-сбой в сбой мутации."""
    try:
        return await message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as e:
        detail = str(e).casefold()
        if (
            "message is not modified" in detail
            or "message to edit not found" in detail
        ):
            log.debug("facts: служебное сообщение уже не требует обновления: %s", e)
        else:
            log.warning("facts: Telegram не позволил обновить owner-menu: %s", e)
    except Exception as e:
        log.warning(
            "facts: не удалось обновить owner-menu (%s)",
            type(e).__name__,
        )
    return None


async def _facts_edit_status(
    message: Message | None,
    *,
    notice: str | None = None,
) -> FactBankSnapshot:
    """Перечитать файл и заменить текущее owner-menu актуальным статусом."""
    snapshot = reload_fact_bank()
    if message is not None:
        await _safe_facts_edit(
            message,
            _facts_status_text(snapshot, notice=notice),
            reply_markup=_facts_menu_keyboard(snapshot),
        )
    return snapshot


async def cmd_facts(message: Message, state: FSMContext) -> None:
    """Открыть скрытое меню управления дополнительными фактами."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer("🚫 Эта команда только для владельца бота.")
        return
    await state.clear()
    snapshot = reload_fact_bank()
    await message.reply(
        _facts_status_text(snapshot),
        parse_mode=ParseMode.HTML,
        reply_markup=_facts_menu_keyboard(snapshot),
    )


async def facts_upload_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Перевести owner-flow в ожидание полного JSON-кандидата."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Меню устарело. Отправь /facts ещё раз.", show_alert=True)
        return
    await state.set_state(FactsStates.waiting_upload_file)
    prompt = await _safe_facts_edit(
        callback.message,
        "📤 Пришли <b>JSON-файл</b> дополнительных фактов как документ. "
        "Имя может быть любым, расширение — <code>.json</code>.\n\n"
        "Сначала проверю весь файл и покажу изменения. Диск и активный банк "
        "поменяются только после кнопки «Применить».\n\n/cancel — отмена",
    )
    if prompt is None:
        await state.clear()
        await callback.answer(
            "Не удалось открыть меню. Отправь /facts ещё раз.",
            show_alert=True,
        )
        return
    await callback.answer()
    command = callback.message.reply_to_message
    command_msg_id = command.message_id if command is not None else None
    await state.update_data(
        prompt_msg_id=prompt.message_id,
        command_msg_id=command_msg_id,
    )


async def _reject_facts_upload(
    message: Message,
    state: FSMContext,
    text: str,
) -> None:
    """Удалить отклонённую попытку и переиспользовать один upload-промпт."""
    data = await state.get_data()
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    prompt_id = data.get("prompt_msg_id")
    if isinstance(prompt_id, int) and prompt_id > 0:
        try:
            await message.bot.edit_message_text(
                text=text,
                chat_id=message.chat.id,
                message_id=prompt_id,
                parse_mode=ParseMode.HTML,
            )
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).casefold():
                return
            log.debug("facts: не удалось переиспользовать upload-промпт: %s", e)
        except Exception as e:
            log.warning(
                "facts: не удалось обновить upload-промпт (%s)",
                type(e).__name__,
            )
    fallback = await message.answer(text, parse_mode=ParseMode.HTML)
    await state.update_data(prompt_msg_id=fallback.message_id)


async def facts_receive(message: Message, state: FSMContext) -> None:
    """Проверить загруженный JSON и показать replacement-preview без мутации."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
    document = message.document
    if not document or not (document.file_name or "").casefold().endswith(".json"):
        await _reject_facts_upload(
            message,
            state,
            "📎 Жду файл-документ с расширением <code>.json</code>. Или /cancel.",
        )
        return
    file_size = document.file_size
    if (
        isinstance(file_size, bool)
        or not isinstance(file_size, int)
        or file_size < 0
        or file_size > FACT_BANK_MAX_BYTES
    ):
        await _reject_facts_upload(
            message,
            state,
            "📦 JSON-файл должен иметь известный размер не больше "
            f"<b>{FACT_BANK_MAX_BYTES // 1024} КиБ</b>.",
        )
        return

    try:
        buffer = io.BytesIO()
        await message.bot.download(document, destination=buffer)
        candidate = parse_fact_bank_bytes(buffer.getvalue())
    except FactBankValidationError as e:
        await _reject_facts_upload(
            message,
            state,
            "❌ <b>Файл не прошёл проверку</b>\n\n"
            f"{h(str(e))}\n\nИсправь файл и пришли его снова. Или /cancel.",
        )
        return
    except Exception as e:
        log.warning("facts: не удалось скачать файл-кандидат: %s", e)
        await _reject_facts_upload(
            message,
            state,
            "❌ Не удалось скачать JSON-файл. Попробуй ещё раз. Или /cancel.",
        )
        return

    snapshot = reload_fact_bank()
    added, changed, removed = fact_bank_delta(candidate, snapshot)
    canonical = serialize_fact_bank(candidate)
    candidate_revision = fact_bank_candidate_revision(candidate)
    fsm = await state.get_data()
    command_msg_id = fsm.get("command_msg_id")
    prompt_id = fsm.get("prompt_msg_id")
    if prompt_id is not None:
        await _safe_delete(message.bot, message.chat.id, prompt_id)
    control_id = fsm.get("control_msg_id")
    if control_id is not None:
        await _safe_delete(message.bot, message.chat.id, control_id)
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    await state.set_state(FactsStates.waiting_apply_confirmation)
    reply_parameters = (
        ReplyParameters(
            message_id=command_msg_id,
            allow_sending_without_reply=True,
        )
        if isinstance(command_msg_id, int) and command_msg_id > 0
        else None
    )
    preview = await message.answer(
        "🔎 <b>Предпросмотр замены</b>\n\n"
        f"Версия: <code>{h(candidate.bank_version or '—')}</code>\n"
        f"Фактов в кандидате: <b>{len(candidate.facts)}</b>\n\n"
        f"Добавится: <b>{added}</b>\n"
        f"Изменится: <b>{changed}</b>\n"
        f"Удалится: <b>{removed}</b>\n\n"
        "Применение полностью заменит текущий дополнительный банк.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Применить",
                callback_data=(
                    f"{FACTS_APPLY_CALLBACK_PREFIX}{snapshot.revision}:"
                    f"{candidate_revision}"
                ),
            ),
            InlineKeyboardButton(text="❌ Отмена", callback_data="facts:cancel"),
        ]]),
        reply_parameters=reply_parameters,
    )
    await state.update_data(
        prompt_msg_id=prompt_id,
        control_msg_id=preview.message_id,
        candidate_json=canonical,
        candidate_revision=candidate_revision,
        expected_revision=snapshot.revision,
    )


def _parse_facts_apply_callback(data: str) -> tuple[str, str] | None:
    """Разобрать hash-привязку preview к исходному и новому банкам."""
    if not data.startswith(FACTS_APPLY_CALLBACK_PREFIX):
        return None
    expected, separator, candidate = data.removeprefix(
        FACTS_APPLY_CALLBACK_PREFIX
    ).partition(":")
    if not separator or len(expected) != 16 or len(candidate) != 16:
        return None
    return expected, candidate


async def facts_apply_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Применить только всё ещё актуальный preview-кандидат."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return
    parsed = _parse_facts_apply_callback(callback.data or "")
    data = await state.get_data()
    if parsed is None or not data.get("candidate_json"):
        await callback.answer(
            "Это подтверждение уже недействительно. Отправь /facts ещё раз.",
            show_alert=True,
        )
        return
    expected_revision, candidate_revision = parsed
    if (
        data.get("expected_revision") != expected_revision
        or data.get("candidate_revision") != candidate_revision
    ):
        await callback.answer(
            "Это подтверждение устарело. Отправь /facts ещё раз.",
            show_alert=True,
        )
        return
    try:
        candidate = parse_fact_bank_bytes(data["candidate_json"].encode("utf-8"))
        if fact_bank_candidate_revision(candidate) != candidate_revision:
            raise FactBankValidationError("preview-кандидат изменился")
        snapshot = await publish_fact_bank(
            candidate,
            expected_revision=expected_revision,
        )
    except StaleFactBankError:
        await state.clear()
        await callback.answer(
            "Банк уже изменился. Открой /facts и проверь новое состояние.",
            show_alert=True,
        )
        await _facts_edit_status(
            callback.message,
            notice="⚠️ Подтверждение устарело; изменения не применены.",
        )
        return
    except (FactBankValidationError, OSError) as e:
        log.warning("facts: кандидат не опубликован: %s", e)
        await callback.answer(
            "Не удалось безопасно применить банк; прежний банк сохранён.",
            show_alert=True,
        )
        return

    await state.clear()
    await callback.answer("Дополнительный банк применён.")
    if callback.message is not None:
        await _safe_facts_edit(
            callback.message,
            _facts_status_text(snapshot, notice="✅ Дополнительный банк применён."),
            reply_markup=_facts_menu_keyboard(snapshot),
        )


async def facts_cancel_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Отменить upload/preview/clear и вернуться к актуальному статусу."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Отменено.")
    await _facts_edit_status(callback.message, notice="❌ Изменения отменены.")


async def facts_download_cb(callback: CallbackQuery) -> None:
    """Отправить текущий дополнительный банк в канонической форме."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Меню устарело. Отправь /facts ещё раз.", show_alert=True)
        return
    snapshot = reload_fact_bank()
    if not snapshot.additional_facts:
        await callback.answer(
            "Дополнительных фактов больше нет. Проверь состояние файла.",
            show_alert=True,
        )
        await _facts_edit_status(callback.message)
        return
    payload = canonical_active_fact_bank().encode("utf-8")
    if not await _claim_inline_menu(callback.message):
        await callback.answer(
            "Это меню уже обработано. Отправь /facts ещё раз.",
            show_alert=True,
        )
        return
    await callback.answer("Готовлю facts.json...")
    await callback.message.answer_document(
        BufferedInputFile(payload, filename="facts.json"),
        caption="💡 Канонический дополнительный банк фактов.",
        parse_mode=ParseMode.HTML,
    )


async def facts_example_cb(callback: CallbackQuery) -> None:
    """Отправить владельцу встроенный валидный пример дополнительного банка."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Меню устарело. Отправь /facts ещё раз.", show_alert=True)
        return
    try:
        example = parse_fact_bank_bytes(FACT_BANK_EXAMPLE_PATH.read_bytes())
        payload = serialize_fact_bank(example).encode("utf-8")
    except (FactBankValidationError, OSError) as e:
        log.error("facts: встроенный пример недоступен: %s", e)
        await callback.answer(
            "Пример facts.json недоступен в этой сборке.",
            show_alert=True,
        )
        return
    if not await _claim_inline_menu(callback.message):
        await callback.answer(
            "Это меню уже обработано. Отправь /facts ещё раз.",
            show_alert=True,
        )
        return
    await callback.answer("Готовлю пример facts.json...")
    await callback.message.answer_document(
        BufferedInputFile(payload, filename="facts.json"),
        caption=(
            "💡 Готовый пример из пяти фактов. Его можно отредактировать и "
            "загрузить через /facts."
        ),
        parse_mode=ParseMode.HTML,
    )


def _fact_count_word(count: int) -> str:
    """Выбрать русскую форму слова «факт» для указанного количества."""
    remainder = count % 100
    if 11 <= remainder <= 14:
        return "фактов"
    last_digit = count % 10
    if last_digit == 1:
        return "факт"
    if 2 <= last_digit <= 4:
        return "факта"
    return "фактов"


async def facts_ask_clear_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Первое нажатие очистки: показать единственное подтверждение с числом."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return
    data = callback.data or ""
    expected_revision = data.removeprefix(FACTS_ASK_CLEAR_CALLBACK_PREFIX)
    snapshot = reload_fact_bank()
    if expected_revision != snapshot.revision or not snapshot.additional_facts:
        await callback.answer(
            "Банк уже изменился. Открой /facts и проверь новое состояние.",
            show_alert=True,
        )
        await _facts_edit_status(callback.message)
        return
    await state.clear()
    await callback.answer()
    if callback.message is not None:
        count = len(snapshot.additional_facts)
        await _safe_facts_edit(
            callback.message,
            "🗑 <b>Очистить дополнительные факты?</b>\n\n"
            f"Будет удалено: <b>{count}</b>. Встроенные 50+6 останутся.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"Да, удалить {count} {_fact_count_word(count)}",
                    callback_data=(
                        f"{FACTS_CONFIRM_CLEAR_CALLBACK_PREFIX}"
                        f"{snapshot.revision}"
                    ),
                )],
                [InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="facts:cancel",
                )],
            ]),
        )


async def facts_confirm_clear_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Вторым и последним нажатием атомарно очистить дополнительный банк."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return
    expected_revision = (callback.data or "").removeprefix(
        FACTS_CONFIRM_CLEAR_CALLBACK_PREFIX
    )
    if len(expected_revision) != 16:
        await callback.answer("Подтверждение повреждено.", show_alert=True)
        return
    try:
        snapshot = await clear_fact_bank(expected_revision=expected_revision)
    except StaleFactBankError:
        await callback.answer(
            "Банк уже изменился. Очистка не выполнена.",
            show_alert=True,
        )
        await _facts_edit_status(callback.message)
        return
    except OSError as e:
        log.warning("facts: не удалось очистить дополнительный банк: %s", e)
        await callback.answer(
            "Не удалось безопасно очистить банк; прежний банк сохранён.",
            show_alert=True,
        )
        return
    await state.clear()
    await callback.answer("Дополнительные факты удалены.")
    if callback.message is not None:
        await _safe_facts_edit(
            callback.message,
            _facts_status_text(snapshot, notice="✅ Дополнительные факты удалены."),
            reply_markup=_facts_menu_keyboard(snapshot),
        )


async def facts_close_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Закрыть owner-menu и защитно сбросить его FSM."""
    if callback.from_user is None or callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только для владельца.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _cleanup_inline_menu(callback.message)


# ═══════════════════════════════════════════════════════════════
#  КОМАНДЫ БОТА
# ═══════════════════════════════════════════════════════════════

def _fact_keyboard(fact_id: str, *, initiator_id: int) -> InlineKeyboardMarkup:
    """Привязать обновление к показанному факту и инициатору команды."""
    return build_fact_keyboard(
        next_callback_data=(
            f"{_FACT_NEXT_CALLBACK_PREFIX}{initiator_id}:{fact_id}"
        ),
        share_query=build_fact_share_query(fact_id),
    )


def _fact_message_seed(initiator_id: int, message_id: int) -> str:
    """Собрать стабильное зерно выбора факта для конкретной команды."""
    return f"{initiator_id}\0{message_id}"


def _parse_fact_next_callback(data: str) -> tuple[int, str] | None:
    """Разобрать callback обновления и отклонить старый или поддельный формат."""
    if not data.startswith(_FACT_NEXT_CALLBACK_PREFIX):
        return None
    initiator_raw, separator, fact_id = data.removeprefix(
        _FACT_NEXT_CALLBACK_PREFIX
    ).partition(":")
    if not separator or not fact_id:
        return None
    try:
        initiator_id = int(initiator_raw)
    except ValueError:
        return None
    if initiator_id <= 0:
        return None
    return initiator_id, fact_id


async def cmd_fact(message: Message) -> None:
    """Показать публичный локальный факт без подписки и сетевых обращений."""
    if message.from_user is None:
        return
    initiator_id = message.from_user.id
    fact = select_fact(_fact_message_seed(initiator_id, message.message_id))
    await message.answer(
        build_fact_text(fact),
        parse_mode=ParseMode.HTML,
        reply_markup=_fact_keyboard(fact.id, initiator_id=initiator_id),
    )


async def fact_next_cb(callback: CallbackQuery) -> None:
    """Заменить показанный факт следующим и всегда подтвердить callback."""
    parsed = _parse_fact_next_callback(callback.data or "")
    if parsed is None:
        await callback.answer(
            "Этот факт устарел. Отправь /fact ещё раз.",
            show_alert=True,
        )
        return
    initiator_id, current_id = parsed
    sender_id = getattr(getattr(callback, "from_user", None), "id", None)
    if sender_id != initiator_id:
        await callback.answer(
            "Обновить этот факт может только тот, кто его запросил.",
            show_alert=True,
        )
        return
    try:
        fact = select_next_fact(current_id)
    except ValueError:
        await callback.answer(
            "Этот факт устарел. Отправь /fact ещё раз.",
            show_alert=True,
        )
        return

    await callback.answer()
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            build_fact_text(fact),
            parse_mode=ParseMode.HTML,
            reply_markup=_fact_keyboard(fact.id, initiator_id=initiator_id),
        )
    except TelegramBadRequest as e:
        log.warning(
            "fact: Telegram не позволил заменить сообщение (%s)",
            e,
        )


def _return_to_inline_keyboard() -> InlineKeyboardMarkup:
    """Кнопка ручного возврата к выбору чата без автоматического поиска."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Вернуться к поиску",
            switch_inline_query="",
        )
    ]])


def _inline_limit_text() -> str:
    """Объяснение после перехода из кнопки исчерпанного inline-лимита."""
    return (
        "⏳ <b>Shikimori попросил сделать паузу</b>\n\n"
        "Минутный лимит запросов к Shikimori временно исчерпан. "
        "Бот приостановил новые поисковые запросы, чтобы сохранить запас для "
        "уведомлений, /status и остальных функций.\n\n"
        "Обычно поиск возобновляется меньше чем через минуту. Вернитесь к поиску и "
        "повторите запрос чуть позже."
    )


async def _answer_inline_access(inline_query: InlineQuery, status: str) -> bool:
    """Ответить на отклонённый inline-запрос; вернуть признак завершения."""
    if status == INLINE_ACCESS_ALLOWED:
        return False
    kwargs = {"cache_time": 0}
    if status != INLINE_ACCESS_BLOCKED:
        kwargs["button"] = InlineQueryResultsButton(
            text="Подписаться для поиска",
            start_parameter="inline_search",
        )
    await inline_query.answer([], **kwargs)
    return True


async def cmd_inline_search(inline_query: InlineQuery) -> None:
    """Развести публичные факты и доступный подписчикам поиск тайтлов."""
    user_id = inline_query.from_user.id
    try:
        fact_query = classify_fact_query(inline_query.query)
        if fact_query == FACT_QUERY_MATCH:
            if inline_query.offset:
                await inline_query.answer([], cache_time=0)
                return
            fact = fact_from_share_query(inline_query.query)
            if fact is None:
                fact = select_fact(f"{user_id}\0{inline_query.id}")
            await inline_query.answer(
                [build_fact_result(fact, page=1)],
                cache_time=0,
            )
            return
        if fact_query == FACT_QUERY_REJECT:
            await inline_query.answer([], cache_time=0)
            return

        status = inline_access_status(user_id)
        if await _answer_inline_access(inline_query, status):
            return
        actor = InlineActor(
            user_id=user_id,
            full_name=inline_query.from_user.full_name,
            username=inline_query.from_user.username,
        )

        query = parse_inline_query(inline_query.query)
        offset = inline_query.offset or ""
        generation: int | None = None
        if offset:
            if query is None:
                await inline_query.answer([], cache_time=0)
                return
            page = _inline_search_service.resolve_continuation(query, offset)
            if page is None:
                await inline_query.answer([], cache_time=0)
                return
        else:
            if query is None:
                _inline_search_service.invalidate_debounce(user_id)
                await inline_query.answer([], cache_time=0)
                return
            generation = await _inline_search_service.debounce(user_id)
            if generation is None:
                await inline_query.answer([], cache_time=0)
                return
            if not _inline_search_service.is_current(user_id, generation):
                await inline_query.answer([], cache_time=0)
                return
            page = 1

        def authorized() -> bool:
            return inline_access_status(user_id) == INLINE_ACCESS_ALLOWED

        current_status = inline_access_status(user_id)
        if current_status != INLINE_ACCESS_ALLOWED:
            await _answer_inline_access(inline_query, current_status)
            return
        try:
            search_page = await _inline_search_service.get_page(
                query,
                page,
                authorized=authorized,
                actor=actor,
            )
        except InlineSearchLimitExceeded as e:
            current_status = inline_access_status(user_id)
            if current_status != INLINE_ACCESS_ALLOWED:
                await _answer_inline_access(inline_query, current_status)
                return
            retry_after = max(1, math.ceil(e.retry_after))
            await inline_query.answer(
                [],
                cache_time=0,
                button=InlineQueryResultsButton(
                    text=f"⏳ Лимит Shikimori: повторите через {retry_after} с",
                    start_parameter="inline_search_limit",
                ),
            )
            return
        if search_page is None:
            await inline_query.answer([], cache_time=0)
            return
        if (
            generation is not None
            and not _inline_search_service.is_current(user_id, generation)
        ):
            await inline_query.answer([], cache_time=0)
            return

        bot_username = None
        try:
            bot_user = await inline_query.bot.me()
            bot_username = bot_user.username
        except Exception as e:
            log.warning(
                "inline-search: не удалось определить username бота (%s)",
                type(e).__name__,
            )

        rendered_results = []
        for item in search_page.items:
            try:
                rendered_results.append(
                    build_inline_result(
                        query.media_type,
                        item,
                        bot_username=bot_username,
                    )
                )
            except Exception as e:
                log.warning(
                    "inline-search: пропущена повреждённая карточка (%s)",
                    type(e).__name__,
                )
        fact_seed = (
            f"{user_id}\0{query.media_type}\0{query.title.casefold()}"
        )
        results = finalize_inline_results(
            rendered_results,
            page=page,
            fact_seed=fact_seed,
        )
        next_offset = ""
        if len(search_page.items) == SHIKIMORI_PAGE_SIZE:
            next_offset = _inline_search_service.issue_continuation(
                query,
                page=page + 1,
                preceding_expires_at=search_page.expires_at,
            )
        await inline_query.answer(
            results,
            cache_time=0,
            next_offset=next_offset,
        )
    except Exception as e:
        if isinstance(e, TelegramBadRequest):
            log.warning(
                "inline-search: Telegram отклонил результаты; "
                "отправляю безопасный пустой ответ: %s",
                e,
            )
        else:
            log.warning("inline-search: безопасный пустой ответ (%s)", type(e).__name__)
        try:
            await inline_query.answer([], cache_time=0)
        except Exception:
            log.debug("inline-search: Telegram не принял пустой ответ")


async def cmd_start(
    message: Message,
    command: CommandObject | None = None,
) -> None:
    """Подписаться на уведомления (для владельца — заодно добудить фоновый цикл)."""
    chat_id = message.chat.id
    name = message.from_user.full_name if message.from_user else str(chat_id)
    info_start = bool(
        command is not None
        and command.args == "info"
        and message.from_user is not None
        and chat_id == message.from_user.id
    )
    if info_start:
        await _send_info(message)
        return

    inline_limit_start = bool(
        command is not None
        and command.args == "inline_search_limit"
        and message.from_user is not None
        and chat_id == message.from_user.id
    )
    if inline_limit_start:
        await message.answer(
            _inline_limit_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=_return_to_inline_keyboard(),
        )
        return

    if message.from_user is not None and message.from_user.id == OWNER_ID:
        if start_polling_loop(message.bot):
            log.info("Фоновый цикл добужен владельцем через /start.")

    inline_search_start = bool(
        command is not None
        and command.args == "inline_search"
        and message.from_user is not None
        and chat_id == message.from_user.id
    )
    answer_kwargs = {"parse_mode": ParseMode.HTML}
    if inline_search_start:
        answer_kwargs["reply_markup"] = _return_to_inline_keyboard()

    async with restorable_state_transaction():
        subs = load_subscribers()
        already_subscribed = chat_id in subs
        if not already_subscribed:
            subs[chat_id] = name
            save_subscribers(subs)

    if already_subscribed:
        await message.answer(
            f"☕ Ты уже подписан, {h(name)}! Буду слать новости об активности "
            f"{h(DISPLAY_NAME_CONTEXT.genitive)}.",
            **answer_kwargs,
        )
        return

    log.info("Новый подписчик: %s (chat_id=%d). Всего: %d.", name, chat_id, len(subs))
    await _backup_after_subscription(message.bot, chat_id, name, subscribed=True)
    reply = (
        f"✅ Подписка оформлена, {h(name)}!\n"
        "Теперь ты будешь получать уведомления об активности "
        f"{h(DISPLAY_NAME_CONTEXT.genitive)} на Shikimori. \U0001f3cc\n\n"
        "Чтобы отписаться — /stop"
    )
    await message.answer(reply, **answer_kwargs)


async def cmd_stop(message: Message) -> None:
    """Отписаться от уведомлений."""
    chat_id = message.chat.id
    name = message.from_user.full_name if message.from_user else str(chat_id)

    async with restorable_state_transaction():
        subs = load_subscribers()
        was_subscribed = chat_id in subs
        if was_subscribed:
            subs.pop(chat_id)
            save_subscribers(subs)

    if not was_subscribed:
        await message.answer(
            "🤔 Ты и так не подписан. Напиши /start чтобы подписаться."
        )
        return

    log.info("Отписался: %s (chat_id=%d). Осталось: %d.", name, chat_id, len(subs))
    await _backup_after_subscription(message.bot, chat_id, name, subscribed=False)
    reply = (
        f"👋 Ты отписан, {name}. Жаль терять такого зрителя!\n"
        "Если передумаешь — /start"
    )
    await message.answer(reply)


async def cmd_subs(message: Message) -> None:
    """Список подписчиков (только для владельца)."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer("🚫 Эта команда только для владельца бота.")
        return

    subs = load_subscribers()
    if not subs:
        await message.answer("📭 Подписчиков пока нет.")
        return

    count = len(subs)
    lines = [f"👥 Подписчиков: <b>{count}</b>", ""]
    for i, (cid, uname) in enumerate(subs.items(), 1):
        lines.append(f"{i}. {_subscriber_link(cid, uname)}")
    sep = "\n"
    await message.answer(sep.join(lines), parse_mode=ParseMode.HTML)


def _command_target_user_id(message: Message) -> int | None:
    """Извлечь единственный ASCII decimal user ID из owner-команды."""
    text = getattr(message, "text", None)
    if not isinstance(text, str):
        return None
    parts = text.split()
    if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdecimal():
        return None
    try:
        return validate_telegram_user_id(int(parts[1]))
    except ValueError:
        return None


async def cmd_block(message: Message) -> None:
    """Скрытая owner-only команда постоянной блокировки Telegram user ID."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer(
            "🚫 Эта команда только для владельца бота.",
            parse_mode=ParseMode.HTML,
        )
        return
    user_id = _command_target_user_id(message)
    if user_id is None:
        await message.answer(
            "Использование: <code>/block 123456789</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if user_id == OWNER_ID:
        await message.answer(
            "🚫 Владельца бота нельзя заблокировать.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        added, subscriber_removed = await add_blocked_user(user_id)
    except (BlockedUsersMutationError, BlockedUsersStateError, OSError) as e:
        log.error("cmd_block: не удалось изменить список блокировок: %s", e)
        await message.answer(
            "❌ Не удалось безопасно изменить список блокировок. Подробности записаны в лог.",
            parse_mode=ParseMode.HTML,
        )
        return

    rendered_id = f"<code>{user_id}</code>"
    if added and subscriber_removed:
        text = f"✅ Пользователь {rendered_id} заблокирован и удалён из подписчиков."
    elif added:
        text = f"✅ Пользователь {rendered_id} заблокирован."
    elif subscriber_removed:
        text = f"ℹ️ Пользователь {rendered_id} уже был заблокирован; подписка удалена."
    else:
        text = f"ℹ️ Пользователь {rendered_id} уже заблокирован."
    await message.answer(text, parse_mode=ParseMode.HTML)


async def cmd_unblock(message: Message) -> None:
    """Скрытая owner-only команда снятия блокировки без возврата подписки."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer(
            "🚫 Эта команда только для владельца бота.",
            parse_mode=ParseMode.HTML,
        )
        return
    user_id = _command_target_user_id(message)
    if user_id is None:
        await message.answer(
            "Использование: <code>/unblock 123456789</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if user_id == OWNER_ID:
        await message.answer(
            "ℹ️ Владелец бота не находится в списке блокировок.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        removed = await remove_blocked_user(user_id)
    except (BlockedUsersMutationError, BlockedUsersStateError, OSError) as e:
        log.error("cmd_unblock: не удалось изменить список блокировок: %s", e)
        await message.answer(
            "❌ Не удалось безопасно изменить список блокировок. Подробности записаны в лог.",
            parse_mode=ParseMode.HTML,
        )
        return

    rendered_id = f"<code>{user_id}</code>"
    text = (
        f"✅ Пользователь {rendered_id} разблокирован. Подписка не восстановлена."
        if removed
        else f"ℹ️ Пользователь {rendered_id} не был заблокирован."
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


def _build_blocklist_messages(blocked_user_ids: set[int]) -> list[str]:
    """Собрать HTML-сообщения со списком, не разрывая строки ID."""
    ordered_ids = sorted(blocked_user_ids)
    if not ordered_ids:
        return [f"📭 Список блокировок пуст.\n\n{_BLOCKLIST_HINT}"]

    header = f"🚫 <b>Заблокированные пользователи: {len(ordered_ids)}</b>"
    rows = [f"• <code>{user_id}</code>" for user_id in ordered_ids]
    messages: list[str] = []
    current = header

    for row in rows:
        separator = "\n\n" if current == header else "\n"
        candidate = f"{current}{separator}{row}"
        if len(f"{candidate}\n\n{_BLOCKLIST_HINT}") <= _TELEGRAM_MESSAGE_LIMIT:
            current = candidate
            continue
        messages.append(current)
        current = row

    messages.append(f"{current}\n\n{_BLOCKLIST_HINT}")
    return messages


async def cmd_blocklist(message: Message) -> None:
    """Показать владельцу полный список заблокированных Telegram ID."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer(
            "🚫 Эта команда только для владельца бота.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        blocked_user_ids = list_blocked_users()
    except (BlockedUsersStateError, OSError) as e:
        log.error("cmd_blocklist: не удалось прочитать список блокировок: %s", e)
        await message.answer(
            "❌ Не удалось безопасно прочитать список блокировок. "
            "Подробности записаны в лог.",
            parse_mode=ParseMode.HTML,
        )
        return

    for text in _build_blocklist_messages(blocked_user_ids):
        await message.answer(text, parse_mode=ParseMode.HTML)


async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    """Начать рассылку сообщения подписчикам."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer("🚫 Эта команда только для владельца бота.")
        return
    await _safe_delete(message.bot, message.chat.id, message.message_id)  # убираем саму /broadcast
    await state.set_state(BroadcastStates.waiting_content)
    prompt = await message.answer(
        "✍️ Пришли сообщение для рассылки.\n"
        "Поддерживаются: текст, фото, видео, GIF, стикер, документ, голосовое.\n\n"
        "/cancel — передумал",
    )
    await state.update_data(prompt_msg_id=prompt.message_id)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Отменить текущую операцию (работает в любом FSM-состоянии)."""
    if await state.get_state() is None:
        await message.answer("🤷 Нечего отменять.")
        return
    data = await state.get_data()
    await state.clear()
    # /pick хранит только ID текущего локального control message и команды.
    # Удаляем их тем же best-effort контрактом, что использует Close.
    if (
        data.get("pick_menu_chat_id") == message.chat.id
        and type(data.get("pick_menu_message_id")) is int
    ):
        await _safe_delete(
            message.bot,
            message.chat.id,
            data["pick_menu_message_id"],
        )
        if type(data.get("pick_command_message_id")) is int:
            await _safe_delete(
                message.bot,
                message.chat.id,
                data["pick_command_message_id"],
            )
        await _safe_delete(message.bot, message.chat.id, message.message_id)
    # broadcast-флоу: подчищаем всё, что флоу мог создать к этому моменту
    if data.get("prompt_msg_id") is not None:
        await _safe_delete(message.bot, message.chat.id, data["prompt_msg_id"])
        for mid in data.get("preview_msg_ids", []):
            await _safe_delete(message.bot, message.chat.id, mid)
        if data.get("control_msg_id") is not None:
            await _safe_delete(message.bot, message.chat.id, data["control_msg_id"])
        await _safe_delete(message.bot, message.chat.id, message.message_id)  # эхо /cancel
    await message.answer("❌ Отменено.")


async def broadcast_receive(message: Message, state: FSMContext) -> None:
    """Принять контент от владельца, показать превью, убрать служебный мусор."""
    if message.sticker:
        data = {"msg_type": "sticker", "file_id": message.sticker.file_id, "user_text": ""}
    elif message.photo:
        data = {"msg_type": "photo",   "file_id": message.photo[-1].file_id, "user_text": message.caption or ""}
    elif message.video:
        data = {"msg_type": "video",   "file_id": message.video.file_id,     "user_text": message.caption or ""}
    elif message.animation:
        data = {"msg_type": "animation","file_id": message.animation.file_id, "user_text": message.caption or ""}
    elif message.document:
        data = {"msg_type": "document","file_id": message.document.file_id,  "user_text": message.caption or ""}
    elif message.voice:
        data = {"msg_type": "voice",   "file_id": message.voice.file_id,     "user_text": message.caption or ""}
    elif message.text:
        data = {"msg_type": "text",    "file_id": None,                      "user_text": message.text}
    else:
        await message.answer("⚠️ Такой тип сообщения не поддерживается. Попробуй другой или /cancel.")
        return

    fsm = await state.get_data()
    await state.update_data(**data)
    await state.set_state(BroadcastStates.waiting_confirm)

    # Чистим служебное: промпт бота и само сообщение владельца
    prompt_id = fsm.get("prompt_msg_id")
    if prompt_id:
        await _safe_delete(message.bot, message.chat.id, prompt_id)
    await _safe_delete(message.bot, message.chat.id, message.message_id)

    # Превью — ровно то же, что увидят подписчики (тот же helper)
    preview_msgs = await _send_broadcast_message(message.bot, message.chat.id, data)

    subs_count = len(load_subscribers())
    control = await message.answer(
        f"👀 Так увидят подписчики ↑\n\nОтправить {subs_count} подписчик(ам)?",
        reply_markup=_confirm_kb(),
    )
    await state.update_data(
        preview_msg_ids=[m.message_id for m in preview_msgs],
        control_msg_id=control.message_id,   # ← добавили: чтобы /cancel мог убрать и контрол
    )


async def broadcast_confirm_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение — рассылаем подписчикам, превью убираем, контрол правим в результат."""
    data = await state.get_data()
    await state.clear()
    bot, chat_id = callback.message.bot, callback.message.chat.id

    for mid in data.get("preview_msg_ids", []):
        await _safe_delete(bot, chat_id, mid)

    subs = load_subscribers()
    if not subs:
        await callback.answer()
        await callback.message.edit_text("📭 Подписчиков нет — некому отправлять.")
        return

    await callback.answer("Отправляю...")
    sent, failed = 0, 0
    to_remove: list[int] = []

    for cid, name in subs.items():
        try:
            await _send_broadcast_message(bot, cid, data)
            sent += 1
            log.info("  broadcast → %s (chat_id=%d)", name, cid)
        except Exception as e:
            if _is_blocked_error(e):
                log.warning("  broadcast ✗ %s (chat_id=%d) заблокировал бота.", name, cid)
                to_remove.append(cid)
            else:
                log.error("  broadcast ✗ %s (chat_id=%d): %s", name, cid, e)
            failed += 1
        await asyncio.sleep(0.3)

    await _unsubscribe_blocked(to_remove)

    await callback.message.edit_text(
        f"✅ Отправлено: {sent}" + (f", ошибок: {failed}" if failed else "") + "."
    )


async def broadcast_cancel_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена — ничего не шлём, чистим превью и контрол подчистую."""
    data = await state.get_data()
    await state.clear()
    bot, chat_id = callback.message.bot, callback.message.chat.id
    await callback.answer("Отменено.")
    for mid in data.get("preview_msg_ids", []):
        await _safe_delete(bot, chat_id, mid)
    await _safe_delete(bot, chat_id, callback.message.message_id)


async def cmd_status(message: Message) -> None:
    """
    /status — показывает что сейчас смотрит/читает пользователь.
    Переиспользует общий свежий результат для всех чатов, затем собирает
    ответ с учётом всех комбинаций.
    """
    try:
        rates = await _get_status_rates()
    except ProfilePrivacyError:
        from_user = getattr(message, "from_user", None)
        is_owner = from_user is not None and from_user.id == OWNER_ID
        text = (
            _profile_privacy_owner_text()
            if is_owner
            else _profile_privacy_public_text()
        )
        await message.answer(text, parse_mode=ParseMode.HTML)
        return
    if rates is None:
        await message.answer("⚠️ Не удалось получить данные от Shikimori. Попробуй позже.")
        return
    anime_list, manga_list = rates

    # Фильтруем аниме по разрешённым видам
    anime_list = [
        item for item in anime_list
        if (item.get("anime") or {}).get("kind", "") in ANIME_ALLOWED_KINDS
    ]

    lines: list[str] = []

    if anime_list:
        lines.append("🎌 <b>Сейчас смотрит:</b>")
        for item in anime_list:
            lines.append(format_rate_entry(item, "anime"))

    if manga_list:
        if lines:
            lines.append("")  # пустая строка-разделитель
        lines.append("📚 <b>Сейчас читает:</b>")
        for item in manga_list:
            lines.append(format_rate_entry(item, "manga"))

    if not lines:
        await message.answer(
            f"😴 {DISPLAY_NAME} сейчас ничего не смотрит и не читает. Подозрительно."
        )
        return

    sep = "\n"
    await message.answer(sep.join(lines), parse_mode=ParseMode.HTML)


async def cmd_version(message: Message) -> None:
    """Показать владельцу версию и состояние проверки обновлений."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer("🚫 Эта команда только для владельца бота.")
        return
    state = await refresh_update_state(force=True)
    await message.answer(
        _build_info_text(state),
        parse_mode=ParseMode.HTML,
        reply_markup=build_version_keyboard(
            state.get("release_url"),
            include_refresh=True,
        ),
    )


def _build_info_text(state: dict) -> str:
    """Собрать локальный runtime-снимок без раскрытия ошибок чтения."""
    try:
        last_backup_at = load_stats_current().get("last_backup_at")
    except Exception as e:
        log.warning("Не удалось прочитать время планового backup для /info: %s", e)
        last_backup_at = "unknown"
    try:
        runtime = get_runtime_snapshot()
    except Exception as e:
        log.warning("Не удалось прочитать runtime status для /info: %s", e)
        runtime = RuntimeSnapshot(None, None, False)
    return build_version_text(
        state,
        runtime=runtime,
        last_backup_at=last_backup_at,
    )


def _load_info_preview() -> BufferedInputFile | None:
    """Прочитать локальную иллюстрацию; при проблеме оставить текстовый ответ."""
    try:
        content = INFO_PREVIEW_PATH.read_bytes()
    except OSError as e:
        log.warning("Не удалось прочитать иллюстрацию /info: %s", e)
        return None
    if not content:
        log.warning("Иллюстрация /info пуста")
        return None
    return BufferedInputFile(content, filename=INFO_PREVIEW_PATH.name)


def _remember_info_preview_file_id(message: Message) -> None:
    """Запомнить Telegram file_id после первой локальной загрузки иллюстрации."""
    global _info_preview_file_id
    photos = getattr(message, "photo", None)
    if not isinstance(photos, (list, tuple)) or not photos:
        return
    file_id = getattr(photos[-1], "file_id", None)
    if isinstance(file_id, str) and file_id:
        _info_preview_file_id = file_id


async def _send_info(message: Message) -> None:
    """Отправить публичную карточку только из локального состояния."""
    global _info_preview_file_id
    state = load_update_state()
    is_owner = message.from_user is not None and message.from_user.id == OWNER_ID
    text = _build_info_text(state)
    keyboard = build_version_keyboard(
        state.get("release_url"),
        include_refresh=is_owner,
    )
    preview = _info_preview_file_id or _load_info_preview()
    if preview is None:
        await message.answer(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return
    try:
        sent = await message.answer_photo(
            preview,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except TelegramBadRequest as e:
        _info_preview_file_id = None
        log.warning("Не удалось отправить иллюстрацию /info: %s", e)
        await message.answer(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return
    if _info_preview_file_id is None:
        _remember_info_preview_file_id(sent)


async def cmd_info(message: Message) -> None:
    """Публично показать только сохранённое и process-local состояние."""
    await _send_info(message)


async def version_refresh_cb(callback: CallbackQuery) -> None:
    """Повторно проверить owner guard и вручную обновить GitHub cache."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return

    await callback.answer("Обновляю сведения…")
    state = await refresh_update_state(force=True)
    if callback.message is not None:
        text = _build_info_text(state)
        keyboard = build_version_keyboard(
            state.get("release_url"),
            include_refresh=True,
        )
        try:
            if getattr(callback.message, "photo", None):
                await callback.message.edit_caption(
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            else:
                await callback.message.edit_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
        except TelegramBadRequest as e:
            log.warning("Не удалось обновить сообщение /info: %s", e)
    build_pick_catalog,
