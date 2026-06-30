# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Хендлеры и фоновый цикл ShikiUpdatesBot.

Верхний слой: команды и FSM (/start, /stop, /subs, /broadcast, /backup,
/status, /stats, /favs), inline-меню, рассылка, цикл уведомлений (check_and_
notify*, polling_loop) и ротация квартала. Зависит от всех нижних модулей;
main.py лишь регистрирует эти функции в Dispatcher.
"""

import asyncio
import io
import time

import aiohttp
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from backup import (
    BACKUP_TAG,
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
    log,
)
from healthcheck import heartbeat
from messages import (
    BROADCAST_HEADER,
    build_favourite_message,
    build_message,
    classify_event,
    extract_score,
    extract_score_change,
    format_rate_entry,
)
from shiki_api import (
    _FAV_CATEGORIES,
    _INDUSTRY_CATEGORIES,
    ANIME_ALLOWED_KINDS,
    fetch_current_rates,
    fetch_favourites,
    fetch_history,
    get_media_info,
    is_relevant,
)
from stats import (
    _collect_favourites,
    _load_prev_quarter_summary,
    _save_quarter_snapshot,
    _update_by_quarter,
    build_current_stats_messages,
    build_favourites_messages,
    build_quarterly_report_messages,
    build_stats_all_messages,
    record_current_event,
    sync_stats_all,
)
from storage import (
    _empty_stats_current,
    load_seen_favourites,
    load_seen_ids,
    load_stats_all,
    load_stats_current,
    load_subscribers,
    save_seen_favourites,
    save_seen_ids,
    save_stats_all,
    save_stats_current,
    save_subscribers,
)
from utils import (
    current_quarter,
    h,
    quarter_label,
)

# Фиксированная пауза между фазами стартовых фетчей (анти-429, boot-throttle).
# Без джиттера — предсказуемый ритм (firewall-философия).
BOOT_PHASE_DELAY = 2.0  # секунд


class BroadcastStates(StatesGroup):
    waiting_content = State()   # ждём сообщение от владельца
    waiting_confirm = State()   # ждём нажатия кнопки подтверждения


def _confirm_kb() -> InlineKeyboardMarkup:
    """Инлайн-клавиатура с кнопками подтверждения/отмены рассылки."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📢 Отправить", callback_data="broadcast_send"),
        InlineKeyboardButton(text="❌ Отмена",    callback_data="broadcast_cancel"),
    ]])


async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    """Best-effort удаление сообщения.

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

async def rotate_quarter_if_needed(bot: Bot, cur: dict, stats_all: dict) -> dict:
    """
    Проверяем смену квартала. Если сменился:
      1. Защита last_report_sent от двойной отправки.
      2. Синхронизируем stats_all (чтобы метаданные завершённых были свежими).
      3. Строим отчёт, сохраняем снапшот quarters/<period>.json.
      4. Обновляем by_quarter в агрегатах stats_all.
      5. Отправляем отчёт владельцу.
      6. Сбрасываем stats_current на новый период.
    Возвращает (возможно новый) stats_current.
    """
    now_period = current_quarter()
    if cur.get("period") == now_period:
        return cur  # квартал не сменился

    old_period = cur.get("period", "???")

    if cur.get("last_report_sent") == now_period:
        # Отчёт уже отправлен (перезапуск в день ротации) — просто сбрасываем
        log.info("rotate_quarter: отчёт за переход в %s уже был отправлен.", now_period)
        fresh = _empty_stats_current(now_period)
        fresh["last_report_sent"] = now_period
        save_stats_current(fresh)
        return fresh

    log.info("rotate_quarter: квартал сменился %s → %s.", old_period, now_period)

    # Свежие метаданные перед отчётом
    try:
        stats_all, _ = await sync_stats_all()
    except Exception as e:
        log.error("rotate_quarter: sync_stats_all упал: %s", e)

    # Сравнение с прошлым кварталом (читаем снапшот предыдущего, если есть)
    prev_quarter = _load_prev_quarter_summary(old_period)

    try:
        report_msgs = build_quarterly_report_messages(cur, stats_all, prev_quarter)
    except Exception as e:
        log.error("rotate_quarter: build_quarterly_report_messages упал: %s", e)
        report_msgs = [f"⚠️ Отчёт за {h(quarter_label(old_period))} не удалось сформировать: {h(str(e))}"]

    # Снапшот квартала
    _save_quarter_snapshot(old_period, cur, stats_all)

    # Обновляем by_quarter в агрегатах
    try:
        _update_by_quarter(stats_all, old_period, cur)
        save_stats_all(stats_all)
    except Exception as e:
        log.error("rotate_quarter: обновление by_quarter: %s", e)

    # Новый текущий квартал
    fresh = _empty_stats_current(now_period)
    fresh["last_report_sent"] = now_period
    save_stats_current(fresh)

    # Отправка отчёта владельцу — по сообщению на тему
    for msg in report_msgs:
        await _send_long(bot, OWNER_ID, msg)
        await asyncio.sleep(0.4)
    log.info("rotate_quarter: отчёт за %s отправлен владельцу (%d сообщ.).", old_period, len(report_msgs))

    # Снапшот состояния по случаю ротации (страховка + сбрасывает недельный таймер)
    fresh["last_backup_at"] = time.time()
    save_stats_current(fresh)
    await send_backup(
        bot,
        f"🗓️ Ротация квартала: {h(quarter_label(old_period))} → "
        f"{h(quarter_label(now_period))}.\nСнапшот состояния.\n\n{BACKUP_TAG}",
    )

    return fresh


# ═══════════════════════════════════════════════════════════════════
#  ОТПРАВКА ДЛИННЫХ СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════════

async def _send_long(bot: Bot, chat_id: int, text: str,
                     disable_preview: bool = False) -> None:
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
            return
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
    except Exception as e:
        log.error("_send_long: не удалось отправить (chat_id=%d): %s", chat_id, e)


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
        # callback.message может быть None (сообщение старше 48 ч) или
        # InaccessibleMessage — тогда удалять нечего, тихо выходим.
        msg = callback.message
        if msg is None:
            return
        # Убираем сообщение с кнопками...
        try:
            await msg.delete()
        except Exception as e:
            log.debug("stats_menu_cb: не удалось удалить меню при закрытии: %s", e)
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        # ...и саму команду /stats, на которую меню отвечало (reply_to_message),
        # чтобы чат остался чистым. getattr — на случай InaccessibleMessage без
        # этого поля. В личке бот вправе удалять входящие; если нельзя — лог и дальше.
        cmd_msg = getattr(msg, "reply_to_message", None)
        if cmd_msg is not None:
            try:
                await cmd_msg.delete()
            except Exception as e:
                log.debug("stats_menu_cb: не удалось удалить команду /stats: %s", e)
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
        except Exception:
            pass

    # Строим и шлём отчёт
    try:
        msgs = await builder()
    except Exception as e:
        log.error("stats_menu_cb: формирование (%s): %s", key, e)
        await callback.message.answer("⚠️ Не удалось сформировать статистику, попробуй позже.")
        return

    await _send_stats_reports(callback.message.bot, callback.message.chat.id, msgs)


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


async def check_and_notify_favourites(
    bot: Bot, seen: set[str],
) -> tuple[set[str], bool]:
    """
    Проверяем избранное:
    1. Загружаем текущий список с Shikimori
    2. Находим новые элементы (которых нет в seen)
    3. Отправляем уведомления и обновляем seen
    4. Если что-то новое нашли — пересобираем stats["favourites"] из УЖЕ
       скачанного списка (без повторного запроса к API), чтобы /favs показывал
       свежее сразу, не дожидаясь 6-часового ресинка.

    Ключ в seen: "{category}_{id}", например "animes_5114".
    Возвращает (seen, found_new).
    """
    async with aiohttp.ClientSession() as session:
        favourites = await fetch_favourites(session)

    if favourites is None:
        log.info("Запрос избранного не удался — пропускаем цикл.")
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
        except Exception as e:
            log.error("check_and_notify_favourites: не удалось обновить stats_all: %s", e)
    else:
        log.info("Изменений в избранном нет.")

    save_seen_favourites(seen)
    return seen, found_new


def _is_blocked_error(exc: Exception) -> bool:
    """True, если ошибка отправки означает, что получатель недоступен
    (заблокировал бота / удалён / чат не найден) — повод его отписать."""
    err = str(exc).lower()
    return ("bot was blocked" in err
            or "user is deactivated" in err
            or "chat not found" in err)


def _unsubscribe_blocked(subs: dict[int, str], to_remove: list[int]) -> None:
    """Удаляет заблокировавших из subs и сохраняет актуальный список."""
    if not to_remove:
        return
    for cid in to_remove:
        subs.pop(cid, None)
    save_subscribers(subs)
    log.info("Отписано %d пользователей, заблокировавших бота.", len(to_remove))


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
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
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

    _unsubscribe_blocked(subs, to_remove)


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
        entries = await fetch_history(session)

    if entries is None:
        log.info("Запрос истории не удался — пропускаем цикл.")
        return seen_ids, cur

    # baseline пуст (первый запуск либо стартовая инициализация не прошла
    # из-за 429/сети) — молча фиксируем текущую историю как baseline и
    # НИЧЕГО не шлём. Провал старта становится безобидной доинициализацией.
    if not seen_ids:
        seen_ids = {e["id"] for e in entries}
        save_seen_ids(seen_ids)
        log.info("История: baseline инициализирован в цикле (%d ID), без отправки.", len(seen_ids))
        return seen_ids, cur

    new_entries = [e for e in entries if e["id"] not in seen_ids]

    if not new_entries:
        log.info("Новых записей нет.")
        return seen_ids, cur

    log.info("Найдено новых записей: %d", len(new_entries))

    # Сортируем по ID: от старых к новым — хронологический порядок сообщений
    new_entries.sort(key=lambda e: e["id"])

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

        # Фиксируем событие в статистике квартала (до отправки — независимо от неё)
        description = entry.get("description", "") or ""
        event_type  = classify_event(description)
        if event_type == "completed":
            score = extract_score(description)
        elif event_type == "score_changed":
            chg = extract_score_change(description)
            score = chg[1] if chg else None
        else:
            score = None
        cur = record_current_event(cur, entry, event_type, media_type, score)

        text = build_message(entry)
        await send_to_all_chats(bot, text)

        # Пауза между разными событиями — не спамим Telegram
        await asyncio.sleep(1)

    save_seen_ids(seen_ids)
    save_stats_current(cur)
    return seen_ids, cur


def _should_full_sync(last_full_sync: float | None, now: float, interval: float) -> bool:
    """Пора ли пересинкивать stats_all: ещё ни разу успешно в этой сессии
    (last_full_sync is None ⇒ ретраим каждый цикл, пока не выйдет) либо с
    последнего успешного синка прошло больше interval секунд."""
    return last_full_sync is None or (now - last_full_sync) >= interval


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

    # boot-throttle: одна общая ClientSession на все стартовые фетчи (анти-429),
    # фиксированные паузы между фазами; избранное тянем ОДИН раз и переиспользуем.
    async with aiohttp.ClientSession() as session:
        if not seen_ids:
            log.info("Первый запуск — инициализируем историю без отправки сообщений.")
            entries = await fetch_history(session)
            if entries is None:
                log.warning("Не удалось получить историю при инициализации — пропускаем, повторим на следующем цикле.")
            else:
                seen_ids = {e["id"] for e in entries}
                save_seen_ids(seen_ids)
                log.info("Инициализировано %d ID истории.", len(seen_ids))
            await asyncio.sleep(BOOT_PHASE_DELAY)

        # Избранное фетчим ОДИН раз: и для инициализации seen_favs, и для sync (fav=).
        favourites = await fetch_favourites(session)
        if not seen_favs:
            log.info("Инициализируем избранное без отправки сообщений.")
            if favourites is None:
                log.warning("Не удалось получить избранное при инициализации — пропускаем, повторим на следующем цикле.")
            else:
                for category in _FAV_CATEGORIES:
                    for item in (favourites.get(category) or []):
                        if item.get("id") is not None:
                            seen_favs.add(f"{category}_{item['id']}")
                save_seen_favourites(seen_favs)
                log.info("Инициализировано %d записей избранного.", len(seen_favs))
        await asyncio.sleep(BOOT_PHASE_DELAY)

        # Актуализируем полную статистику из list_export на той же сессии,
        # с уже полученным избранным (fav=) — без повторного фетча favourites.
        log.info("Синхронизируем статистику за всё время (stats_all)...")
        try:
            stats_all, synced_ok = await sync_stats_all(session=session, fav=favourites)
        except Exception as e:
            log.exception("Не удалось синхронизировать stats_all при старте: %s", e)
            stats_all = load_stats_all()
            synced_ok = False
    # Метка последнего успешного полного синка (monotonic). None ⇒ в этой
    # сессии ещё не синкнулись успешно — цикл будет ретраить каждый раз.
    last_full_sync = time.monotonic() if synced_ok else None

    # Если квартал успел смениться пока бот не работал — ротируем и шлём отчёт.
    try:
        cur = await rotate_quarter_if_needed(bot, cur, stats_all)
    except Exception as e:
        log.exception("Ошибка ротации квартала при старте: %s", e)

    last_error_notify_at = 0.0

    while True:
        try:
            log.info("Проверяем историю и избранное...")
            seen_ids, cur = await check_and_notify(bot, seen_ids, cur)
            seen_favs, _  = await check_and_notify_favourites(bot, seen_favs)

            # Периодический (и ретрай-после-неудачного-старта) ресинк stats_all,
            # чтобы сбой одного запроса не оставлял статистику протухшей/пустой
            # до перезапуска. Дёшево: list_export ×2 + избранное, meta — только
            # по новым id. save_stats_all обновляет кэш, ротация ниже видит свежее.
            if _should_full_sync(last_full_sync, time.monotonic(), FULL_SYNC_INTERVAL):
                try:
                    _, synced_ok = await sync_stats_all()
                    if synced_ok:
                        last_full_sync = time.monotonic()
                    else:
                        log.warning("stats_all: ресинк не удался (429?), повторим в следующем цикле.")
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
        except Exception as e:
            log.exception("Непредвиденная ошибка в цикле проверки, продолжаем: %s", e)

            now = time.monotonic()
            if now - last_error_notify_at >= ERROR_NOTIFY_INTERVAL:
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
    _polling_task = asyncio.create_task(polling_loop(bot))
    _polling_task.add_done_callback(_on_polling_done)
    return True


async def probe_owner_and_start(bot: Bot) -> None:
    """owner-reachability gate. Шлёт владельцу '🟢 Бот запущен' — проба аварийного
    канала + легитимный сигнал рестарта (без дебаунса). Доставилось → стартуем
    фоновый цикл; не доставилось (владелец заблокировал бота / TelegramForbiddenError
    и т.п.) → WARNING, цикл НЕ стартуем. Апдейт-поллинг (dp.start_polling) жив всегда:
    бот отвечает на команды, владелец /start добудит цикл без рестарта контейнера."""
    try:
        await bot.send_message(OWNER_ID, "🟢 Бот запущен")
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
#  Импорт  = по белому списку (subscribers, stats_current, quarters/*);
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
        "(подписчики, статистика, кварталы).\n"
        "📥 <b>Импорт</b> — восстановлю из архива подписчиков, текущий квартал "
        "и снапшоты кварталов.",
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
        "Возьму из него только нужное — подписчиков, текущий квартал и снапшоты "
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
    msg = callback.message
    if msg is None:   # сообщение старше 48 ч / InaccessibleMessage — удалять нечего
        return
    try:
        await msg.delete()
    except Exception as e:
        log.debug("backup_close_cb: не удалось удалить меню: %s", e)
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    cmd_msg = getattr(msg, "reply_to_message", None)
    if cmd_msg is not None:
        try:
            await cmd_msg.delete()
        except Exception as e:
            log.debug("backup_close_cb: не удалось удалить команду /backup: %s", e)


async def backup_receive(message: Message, state: FSMContext) -> None:
    """Принять .zip от владельца, восстановить по белому списку, отчитаться."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
    doc = message.document
    if not doc or not (doc.file_name or "").lower().endswith(".zip"):
        await message.answer("📎 Жду <b>.zip</b>-архив бэкапа. Или /cancel.",
                             parse_mode=ParseMode.HTML)
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
        await message.answer(f"❌ Не удалось скачать файл: {h(str(e))}",
                             parse_mode=ParseMode.HTML)
        return

    try:
        result = restore_backup_zip(raw)
    except ValueError as e:
        await message.answer(f"❌ Архив не восстановлен: {h(str(e))}",
                             parse_mode=ParseMode.HTML)
        return

    restored, skipped = result["restored"], result["skipped"]
    lines = [f"✅ Восстановлено файлов: <b>{len(restored)}</b>"]
    lines += [f"  • <code>{h(n)}</code>" for n in restored]
    if "subscribers.json" in restored:
        lines.append(f"\n👥 Подписчиков теперь: <b>{len(load_subscribers())}</b>")
    if skipped:
        lines.append(f"\n⏭️ Пропущено (вне белого списка/битые): {len(skipped)}")
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════════
#  КОМАНДЫ БОТА
# ═══════════════════════════════════════════════════════════════

async def cmd_start(message: Message) -> None:
    """Подписаться на уведомления (для владельца — заодно добудить фоновый цикл)."""
    if message.from_user is not None and message.from_user.id == OWNER_ID:
        if start_polling_loop(message.bot):
            log.info("Фоновый цикл добужен владельцем через /start.")

    subs = load_subscribers()
    chat_id = message.chat.id
    name = message.from_user.full_name if message.from_user else str(chat_id)

    if chat_id in subs:
        await message.answer(
            f"☕ Ты уже подписан, {name}! Буду слать новости о {DISPLAY_NAME}."
        )
        return

    subs[chat_id] = name
    save_subscribers(subs)
    log.info("Новый подписчик: %s (chat_id=%d). Всего: %d.", name, chat_id, len(subs))
    await _backup_after_subscription(message.bot, chat_id, name, subscribed=True)
    reply = (
        f"✅ Подписка оформлена, {name}!\n"
        f"Теперь ты будешь получать уведомления об активности {DISPLAY_NAME} на Shikimori. \U0001f3cc\n\n"
        "Чтобы отписаться — /stop"
    )
    await message.answer(reply)


async def cmd_stop(message: Message) -> None:
    """Отписаться от уведомлений."""
    subs = load_subscribers()
    chat_id = message.chat.id
    name = message.from_user.full_name if message.from_user else str(chat_id)

    if chat_id not in subs:
        await message.answer(
            "🤔 Ты и так не подписан. Напиши /start чтобы подписаться."
        )
        return

    subs.pop(chat_id)
    save_subscribers(subs)
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
        lines.append(f"{i}. {h(uname)} (<code>{cid}</code>)")
    sep = "\n"
    await message.answer(sep.join(lines), parse_mode=ParseMode.HTML)


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

    _unsubscribe_blocked(subs, to_remove)

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
    Запрашивает аниме и мангу в статусах watching + rewatching
    параллельно, затем собирает ответ с учётом всех комбинаций.
    """
    # Оба запроса параллельно — быстрее
    anime_list, manga_list = await asyncio.gather(
        fetch_current_rates("anime", ["watching", "rewatching"]),
        fetch_current_rates("manga", ["watching", "rewatching"]),
    )

    # Если хотя бы один запрос вернул None — API недоступен
    if anime_list is None or manga_list is None:
        await message.answer("⚠️ Не удалось получить данные от Shikimori. Попробуй позже.")
        return

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
