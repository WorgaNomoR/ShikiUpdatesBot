"""
Shikimori History Watcher Bot
Следит за историей и избранным пользователя на Shikimori 
и отправляет весёлые уведомления в Telegram.

Copyright (C) 2026  WorgaNomoR

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  
See the GNU General Public License for more details.
"""

import asyncio
import io
import json
import logging
import os
import random
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from healthcheck import heartbeat, start_health_server
from utils import (
    _is_partial_quarter,
    _rel_url,
    _safe_float,
    _safe_int,
    _utcnow,
    current_quarter,
    h,
    quarter_label,
    quarter_start,
    tracking_period_label,
)

# ─────────────────────────────────────────────
#  НАСТРОЙКИ — заполни перед запуском
# ─────────────────────────────────────────────
# Токен читается из переменной окружения BOT_TOKEN — не храни его в коде!
# Задать: export BOT_TOKEN="токен_от_BotFather"
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Твой Telegram ID — узнать у @userinfobot.
# Нужен для команд только для владельца (/subs, /backup, /broadcast).
# Задать: export OWNER_ID="123456789"
OWNER_ID = int(os.environ["OWNER_ID"])

SHIKI_USER     = "WNR"                   # ник на Shikimori (для API)
SHIKI_BASE_URL = "https://shikimori.io"  # домен — меняй здесь при смене зеркала

# Отображаемое имя в сообщениях. Опционально через env DISPLAY_NAME;
# по умолчанию — ник профиля (SHIKI_USER). Пустая строка/пробелы → фолбэк.
DISPLAY_NAME   = os.environ.get("DISPLAY_NAME", "").strip() or SHIKI_USER

CHECK_INTERVAL = 15 * 60                 # интервал проверки в секундах (15 минут)
ERROR_NOTIFY_INTERVAL = 30 * 60          # не чаще одного уведомления об ошибке в 30 минут
FULL_SYNC_INTERVAL = 6 * 60 * 60         # как часто пересинкивать stats_all в цикле (6 часов)
WEEKLY_BACKUP_INTERVAL = 7 * 24 * 60 * 60  # интервал еженедельного авто-бэкапа состояния (по last_backup_at)

# ─────────────────────────────────────────────
#  ПУТИ К ФАЙЛАМ ДАННЫХ
#  По умолчанию всё создаётся в /data.
#  Чтобы хранить в другом месте — задай переменную окружения
#  DATA_DIR=/путь/к/папке.
# ─────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError as e:
    logging.getLogger(__name__).warning(
        "Не удалось создать DATA_DIR=%s: %s. "
        "Файлы будут недоступны до исправления прав/пути.", DATA_DIR, e
    )

# Состояние уведомлений (что бот уже видел)
SEEN_IDS_FILE  = DATA_DIR / "seen_ids.json"         # ID обработанных событий истории
SUBS_FILE      = DATA_DIR / "subscribers.json"      # список подписчиков
SEEN_FAVS_FILE = DATA_DIR / "seen_favourites.json"  # ID виденного избранного

# Статистика
STATS_ALL_FILE     = DATA_DIR / "stats_all.json"      # вся история: тайтлы + агрегаты
STATS_CURRENT_FILE = DATA_DIR / "stats_current.json"  # события текущего квартала
QUARTERS_DIR       = DATA_DIR / "quarters"            # замороженные снапшоты кварталов

# ─────────────────────────────────────────────
#  URL источников статистики
# ─────────────────────────────────────────────
GRAPHQL_URL       = f"{SHIKI_BASE_URL}/api/graphql"
LIST_EXPORT_ANIME = f"{SHIKI_BASE_URL}/{SHIKI_USER}/list_export/animes.json"
LIST_EXPORT_MANGA = f"{SHIKI_BASE_URL}/{SHIKI_USER}/list_export/mangas.json"

# ─────────────────────────────────────────────
#  ФИЛЬТР ПО ТИПУ (kind)
#
#  Шикимори возвращает в target.kind строку-идентификатор типа.
#  Мы реагируем только на «значимые» виды — мусор вроде клипов,
#  промороликов и спецвыпусков молча пропускаем (но ID запоминаем).
#
#  Аниме (разрешённые): tv, movie, ova, ona
#  Манга (разрешённые): все, кроме one_shot и doujin
# ─────────────────────────────────────────────
ANIME_ALLOWED_KINDS: frozenset[str] = frozenset({
    "tv",       # TV Сериал (включает Короткие / Средние / Длинные — у них kind="tv")
    "movie",    # Фильм
    "ova",      # OVA
    "ona",      # ONA
})

MANGA_BLOCKED_KINDS: frozenset[str] = frozenset({
    "one_shot", # Ваншот
    "doujin",   # Додзинси (любительское)
})

# Все виды, которые Shikimori относит к манге (используется в get_media_info)
MANGA_KINDS: frozenset[str] = frozenset({
    "manga", "manhwa", "manhua", "novel", "ranobe", "one_shot", "doujin",
})

# ─────────────────────────────────────────────
#  API
# ─────────────────────────────────────────────
HISTORY_URL    = f"{SHIKI_BASE_URL}/api/users/{SHIKI_USER}/history?limit=50"
FAVOURITES_URL = f"{SHIKI_BASE_URL}/api/users/{SHIKI_USER}/favourites"

# Все категории избранного, которые отдаёт API Shikimori (мн. число).
# Один источник правды — используется в инициализации baseline, в цикле
# уведомлений и при сборе /favs. characters/people/mangakas/seyu/producers —
# люди и персонажи; animes/mangas/ranobe — произведения.
_FAV_CATEGORIES: tuple[str, ...] = (
    "animes", "mangas", "ranobe",
    "characters", "people", "mangakas", "seyu", "producers",
)

# Подмножество _FAV_CATEGORIES, которое во фронтенде сливается в один блок
# «Люди индустрии». Один человек может лежать сразу в нескольких ролях
# (например, и seyu, и producers) — по этим категориям дедупим по id.
_INDUSTRY_CATEGORIES: frozenset[str] = frozenset(
    {"people", "mangakas", "seyu", "producers"}
)

HEADERS = {
    "User-Agent": f"ShikimoriWatcherBot/1.0 (TelegramBot; monitoring {SHIKI_USER})",
    "Accept": "application/json",
}

# ─────────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  FSM — состояния для команды /broadcast
# ═══════════════════════════════════════════════════════════════

class BroadcastStates(StatesGroup):
    waiting_content = State()   # ждём сообщение от владельца
    waiting_confirm = State()   # ждём нажатия кнопки подтверждения


BROADCAST_HEADER = f"📢 <b>{DISPLAY_NAME} говорит:</b>"


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


# ═══════════════════════════════════════════════════════════════
#  БАНК СООБЩЕНИЙ
#
#  Переменные в шаблонах:
#    {n}     — отображаемое имя пользователя (DISPLAY_NAME)
#    {title} — название аниме или манги
#    {score} — оценка (только в completed_score_*)
#    {category} — тип (подставляется автоматически)
#
#  Каждый раздел дублируется для аниме и манги —
#  тексты немного разные, чтобы было живо.
# ═══════════════════════════════════════════════════════════════

MESSAGES = {

    # ────────────────────────────────
    #  АНИМЕ
    # ────────────────────────────────

    "anime": {

        # 📋 Добавил в «Запланированное»
        "planned": [
            "📋 {n} закинул <b>{title}</b> в бесконечный список «посмотрю когда-нибудь». Ждём.",
            "🗂️ <b>{title}</b> занял своё место в очереди. Дождётся ли? Обязательно! Скоро ли? Ну, как повезет!",
            "📌 <b>{title}</b> теперь в планах у {n}. Очередь живая, дойдёт черёд.",
            "🧠 {n} взял <b>{title}</b> на заметку. В список к просмотру попадает только отобранное.",
            "🔖 <b>{title}</b> добавлено в коллекцию намерений {n}. Осталось только посмотреть.",
            "📥 <b>{title}</b> отправляется в список к просмотру. Что-то в нём зацепило {n} 👀",
            "📥 {n} закинул <b>{title}</b> в список. Дойдут руки — а они дойдут.",
            "📌 <b>{title}</b> теперь в планах. {n} редко бросает список на полпути.",
            "🔖 {n} присмотрел <b>{title}</b>. Очередь движется, честно.",
            "👀 <b>{title}</b> в списке к просмотру. {n} уже прикидывает, когда втиснуть.",
            "🎯 {n} добавил <b>{title}</b> в планы. Не «когда-нибудь потом», а вполне себе скоро... или не очень.",
            "🍿 <b>{title}</b> ждёт своей очереди у {n}. И таки дождётся.",
            "🧠 {n} занёс <b>{title}</b> в список. Память подвести может — список нет.",
            "📋 Ещё один тайтл в планах у {n}. <b>{title}</b>, ты следующий. Ну, может через парочку.",
        ],

        # ▶️ Начал смотреть
        "watching": [
            "▶️ {n} начал смотреть <b>{title}</b>. Запасаемся попкорном.",
            "🎬 Поехали! <b>{title}</b> запущено. Возврата нет.",
            "👁️ {n} открыл <b>{title}</b> и пропал. Ждём отчёта.",
            "🍿 <b>{title}</b> в плеере, {n} у экрана. Классика.",
            "🚀 Старт! <b>{title}</b> вышло на орбиту просмотра.",
            "😤 {n} не выдержал и таки начал <b>{title}</b>. Посмотрим, чем это закончится.",
            "🎬 Поехали — <b>{title}</b> в плеере у {n}. Возврата нет.",
            "👀 {n} открыл <b>{title}</b> и пропал. Если что, он у экрана.",
            "🍿 <b>{title}</b> пошло. {n} устроился поудобнее.",
            "🚀 {n} взялся за <b>{title}</b>. Посмотрим, затянет или дропнет.",
            "😎 {n} наконец дошёл до <b>{title}</b>. Списку полегчало на один тайтл.",
            "🔥 <b>{title}</b> стартовало у {n}. Ставки на то, сколько серий за раз, принимаются.",
            "📺 {n} включил <b>{title}</b>. «Ещё одну серию и спать» — классика.",
            "⏯️ <b>{title}</b> в процессе у {n}. Дороги назад нет, только до финала.",
        ],

        # 🔁 Пересматривает
        "rewatching": [
            "🔁 {n} пересматривает <b>{title}</b>. Не надоело — значит шедевр (или мазохизм).",
            "♻️ <b>{title}</b> снова в деле. {n} возвращается к проверенному.",
            "🌀 Повторный заход на <b>{title}</b>. Уважаю.",
            "📺 {n} включил <b>{title}</b> ещё раз. Некоторые вещи просто не отпускают.",
            "🔂 <b>{title}</b> на втором (третьем? десятом?) круге у {n}. Это уже традиция.",
            "👏 Решился на ремастер впечатлений — <b>{title}</b> снова смотрит {n}.",
            "🔁 {n} пересматривает <b>{title}</b>. Значит, зацепило не на один раз.",
            "♻️ <b>{title}</b> снова в плеере у {n}. Хорошее не стареет.",
            "🌀 {n} вернулся к <b>{title}</b>. Некоторые вещи тянет пересмотреть.",
        ],

        # 💀 Бросил (dropped)
        "dropped": [
            "🗑️ <b>{title}</b> — в мусор. {n} не пощадил.",
            "💀 Dropped. <b>{title}</b> не пережило встречи с {n}.",
            "🚪 {n} покинул <b>{title}</b> без объяснений. Бывает.",
            "❌ <b>{title}</b> — дропнуто. Минус одно аниме в этом жестоком мире.",
            "😤 {n} посмотрел на <b>{title}</b> и сказал «нет». Твёрдая позиция.",
            "🏳️ <b>{title}</b> не справилось с испытанием {n}. Позор или избавление — решай сам.",
        ],

        # ✅ Завершил без оценки
        "completed_no_score": [
            "✅ {n} досмотрел <b>{title}</b>. Оценку зажал — интригует.",
            "🏁 <b>{title}</b> завершено. Впечатления {n} покрыты тайной.",
            "👀 Конец <b>{title}</b>. Молчание {n} красноречивее слов.",
            "📺 {n} прошёл путь <b>{title}</b> до конца. Без комментариев.",
            "🎌 <b>{title}</b> — пройдено. Оценка — не для слабонервных, видимо.",
            "🤐 Закончил <b>{title}</b> и молчит. Либо шедевр, либо травма.",
            "✅ {n} досмотрел <b>{title}</b>. Без оценки — иногда и так бывает.",
            "🏁 <b>{title}</b> завершено. {n} оценку не поставил, и это его право.",
            "📺 {n} закрыл <b>{title}</b>. Молча, но с чувством выполненного долга.",
            "🎬 <b>{title}</b> досмотрено. Оценка? Может, позже. Может, никогда.",
        ],

        # ⭐ Оценка 1–3
        "completed_score_low": [
            "💩 <b>{title}</b> — {score}/10. {n} страдал, но добил. Настоящий герой.",
            "😭 {score}/10 за <b>{title}</b>. Боль реальна. Зачем вообще?",
            "🤮 <b>{title}</b> получает {score}/10 от {n}. Это приговор.",
            "⚰️ {score}/10 — <b>{title}</b> мертво и похоронено в памяти {n}.",
            "🧟 {n} выжил после <b>{title}</b> ({score}/10). Это уже достижение.",
            "🔥 <b>{title}</b> — {score}/10. Сожжено дотла заслуженно.",
            "📉 <b>{title}</b> — {score}/10. {n} честно домучил. За что — вопрос открытый.",
            "🫠 {score}/10. <b>{title}</b> высосало время {n} и не извинилось.",
        ],

        # 😐 Оценка 4–6
        "completed_score_mid": [
            "😐 <b>{title}</b> — {score}/10. Ни рыба ни мясо, говорит {n}.",
            "🫤 {score}/10 за <b>{title}</b>. Не плохо, не хорошо. Просто... было.",
            "🤷 {n} поставил <b>{title}</b> {score}/10. Среднячок прожил и умер.",
            "📊 <b>{title}</b> — твёрдый {score}/10. {n} явно ожидал большего.",
            "🌫️ {score}/10 — <b>{title}</b> оставило {n} в тумане безразличия.",
            "😶 Посмотрел. Оценил. {score}/10. <b>{title}</b> не потрясло мир {n}.",
            "⚖️ <b>{title}</b> — {score}/10. {n} посмотрел. Бывает и так.",
        ],

        # 🌟 Оценка 7–9
        "completed_score_high": [
            "🌟 <b>{title}</b> — {score}/10! {n} доволен. Хороший вкус подтверждён.",
            "🔥 {score}/10 за <b>{title}</b>! {n} в восторге, и это заслужено.",
            "👏 <b>{title}</b> получает {score}/10 от {n}. Браво, студия!",
            "✨ {score}/10 — <b>{title}</b> попало в сердечко {n}.",
            "🎉 Вот это да! {score}/10 за <b>{title}</b>. Рекомендую к просмотру всем.",
            "💫 <b>{title}</b> — {score}/10. {n} явно не разочарован. Редкий случай.",
            "📈 {score}/10 — <b>{title}</b> попало в {n}. Почти идеально, но десятка — это святое.",
            "🎯 <b>{title}</b> заработало {score}/10. {n} доволен и не скрывает.",
        ],

        # 👑 Оценка 10
        "completed_score_perfect": [
            "👑 <b>{title}</b> — ДЕСЯТКА! {n} нашёл новый фаворит. Занесите в анналы.",
            "🏆 10/10! <b>{title}</b> вошло в пантеон {n}. Это серьёзно.",
            "💎 {n} раздаёт десятки! <b>{title}</b> — абсолютный шедевр по его версии.",
            "🌌 10/10 за <b>{title}</b>. {n} разрушен и счастлив одновременно.",
            "🎌 Максимум! <b>{title}</b> — теперь часть души {n}. Трогательно.",
            "🔮 <b>{title}</b> получает священную десятку. {n} преклоняется.",
            "🗿 <b>{title}</b> — 10/10. {n} сидит молча. Это высшая форма похвалы.",
            "🎆 Десятка! <b>{title}</b> теперь в личном пантеоне {n}. Редкая честь.",
        ],
    },  # конец "anime"

    # ────────────────────────────────
    #  МАНГА (свои тексты — читает, а не смотрит)
    # ────────────────────────────────

    "manga": {

        # 📋 Добавил в «Запланированное»
        "planned": [
            "📚 {n} добавил мангу <b>{title}</b> в список. Прочитает — это вопрос времени, не желания.",
            "🗂️ <b>{title}</b> записана в очередь. Полки ломятся, {n} не останавливается.",
            "📌 {n} запланировал <b>{title}</b>. Главы сами себя не прочитают.",
            "📌 <b>{title}</b> теперь в планах у {n}. Главы подождут, никуда не денутся.",
            "🔖 <b>{title}</b> зафиксирована. {n} снова расширяет свои непрочитанные владения.",
            "📥 Хоп — <b>{title}</b> теперь в планах. Сколько глав? Неважно. Прочитаю. Когда-нибудь.",
            "🔖 {n} присмотрел <b>{title}</b>. В очереди, но очередь у {n} рабочая.",
            "📖 <b>{title}</b> ждёт своего часа. {n} до неё доберётся, дайте срок.",
            "🎯 {n} закинул мангу <b>{title}</b> в планы. Том за томом — но потом.",
            "🧠 <b>{title}</b> в списке у {n}. Не свалка, просто очередь чуть длинновата 😅",
        ],

        # ▶️ Начал читать
        "watching": [
            "📖 {n} открыл мангу <b>{title}</b>. Поехали, глава за главой.",
            "🎌 {n} приступил к чтению <b>{title}</b>. Спать, видимо, не скоро.",
            "👁️ <b>{title}</b> в руках {n}. Ждём отчёта с полей.",
            "📜 {n} начал читать <b>{title}</b>. Надеемся, глав там хватит.",
            "🚀 Старт! <b>{title}</b> — новая манга в арсенале {n}.",
            "😤 {n} не устоял и взялся за <b>{title}</b>. Конца и края не видно, но кого это останавливало.",
            "📖 {n} открыл мангу <b>{title}</b>. Глава за главой, понеслось.",
            "🎌 {n} взялся за <b>{title}</b>. Спать сегодня, видимо, не план.",
            "👀 <b>{title}</b> в руках у {n}. Если пропадёт — он там, листает.",
            "📚 {n} начал читать <b>{title}</b>. Списку стало легче на одну позицию.",
            "🚀 <b>{title}</b> пошла у {n}. Посмотрим, проглотит за ночь или растянет.",
            "😎 {n} дорвался до <b>{title}</b>. «Ещё пару глав» — и так до утра.",
            "🔖 {n} приступил к <b>{title}</b>. Закладка двинулась с нулевой главы.",
            "🌙 Манга <b>{title}</b> открыта. {n} уже знает, что ляжет поздно.",
        ],

        # 🔁 Перечитывает
        "rewatching": [
            "🔁 {n} перечитывает <b>{title}</b>. Значит, оно того стоило.",
            "♻️ <b>{title}</b> снова открыта. {n} возвращается за второй дозой.",
            "🌀 Повторный заход на мангу <b>{title}</b>. Хороший знак.",
            "📚 {n} листает <b>{title}</b> по второму кругу. Некоторые детали проявляются только так.",
            "🔂 <b>{title}</b> на перечитке у {n}. Привязанность подтверждена.",
            "👏 {n} снова с <b>{title}</b> в руках. Уважаю преданность.",
            "🔁 {n} перечитывает <b>{title}</b>. Видимо, осело глубоко.",
            "♻️ <b>{title}</b> открыта повторно. {n} возвращается к проверенному.",
            "📖 {n} взялся за <b>{title}</b> по второму кругу. Детали проявляются только так.",
        ],

        # 💀 Бросил
        "dropped": [
            "🗑️ Манга <b>{title}</b> — дропнута. {n} не пощадил.",
            "💀 {n} закрыл <b>{title}</b> и больше не открывал. Всё.",
            "🚪 <b>{title}</b> осталась недочитанной. {n} ушёл без объяснений.",
            "❌ <b>{title}</b> — в архив. Минус одна манга в этом суровом мире.",
            "😤 {n} дал <b>{title}</b> шанс. Манга не оценила. Итог — дроп.",
            "🏳️ <b>{title}</b> не выдержала испытания {n}. Бывает с лучшими.",
        ],

        # ✅ Завершил без оценки
        "completed_no_score": [
            "✅ {n} дочитал мангу <b>{title}</b>. Молчит. Обрабатывает.",
            "🏁 <b>{title}</b> — прочитано. {n} ставит точку без комментариев.",
            "👀 Финальная глава <b>{title}</b> перевёрнута. Мнение {n} — тайна.",
            "📚 {n} прошёл <b>{title}</b> до конца. Оценка засекречена.",
            "🎌 <b>{title}</b> прочитана. {n} не спешит раскрываться.",
            "🤐 Дочитал и молчит. <b>{title}</b> явно оставила след.",
            "✅ {n} дочитал <b>{title}</b>. Оценку оставил при себе.",
            "🏁 <b>{title}</b> закрыта. {n} перевернул последнюю страницу без вердикта.",
            "📖 {n} добил <b>{title}</b>. Без оценки — бывает и так.",
            "📚 <b>{title}</b> прочитано. Оценку {n} приберёг, видимо."
        ],

        # ⭐ Оценка 1–3
        "completed_score_low": [
            "💩 Манга <b>{title}</b> — {score}/10. {n} дочитал из принципа. Терпеливый человек.",
            "😭 {score}/10 за <b>{title}</b>. Жертва времени принесена. Ради чего?",
            "🤮 <b>{title}</b> получает {score}/10. {n} явно не в восторге.",
            "⚰️ {score}/10 — <b>{title}</b> похоронена в памяти {n}.",
            "🧟 {n} пережил <b>{title}</b> ({score}/10). Медаль за стойкость.",
            "🔥 <b>{title}</b> — {score}/10. Сожжено, забыто, не рекомендуется.",
            "📉 <b>{title}</b> — {score}/10. {n} долистал из упрямства.",
            "🫠 {score}/10 за <b>{title}</b>. Главы кончились раньше, чем терпение. Но впритык.",
        ],

        # 😐 Оценка 4–6
        "completed_score_mid": [
            "😐 <b>{title}</b> — {score}/10. Среднячок. {n} не потрясён.",
            "🫤 {score}/10 за мангу <b>{title}</b>. Прочитал. Закрыл. Пошёл дальше.",
            "🤷 {n} поставил <b>{title}</b> {score}/10. Бывало лучше, бывало хуже.",
            "📊 <b>{title}</b> — {score}/10. В целом норм, но без огня.",
            "🌫️ {score}/10 — <b>{title}</b> прошла мимо сердца {n}.",
            "😶 Прочитал. Оценил. {score}/10. <b>{title}</b> не изменила мировоззрение.",
            "⚖️ {score}/10 за <b>{title}</b>. Прочитано, оценено, забыто к утру.",
        ],

        # 🌟 Оценка 7–9
        "completed_score_high": [
            "🌟 Манга <b>{title}</b> — {score}/10! {n} доволен. Художник постарался.",
            "🔥 {score}/10 за <b>{title}</b>! {n} явно не разочарован.",
            "👏 <b>{title}</b> — {score}/10 от {n}. Достойное чтиво.",
            "✨ {score}/10 — <b>{title}</b> зацепила {n} за живое.",
            "🎉 {score}/10 за <b>{title}</b>. Рекомендую всем любителям хорошей манги.",
            "💫 <b>{title}</b> — {score}/10. Редкий случай, когда {n} доволен.",
            "📈 <b>{title}</b> — {score}/10. {n} закрыл последнюю главу с уважением.",
            "🎯 {score}/10 за <b>{title}</b>. Крепко, до десятки чуть не дотянуло.",
        ],

        # 👑 Оценка 10
        "completed_score_perfect": [
            "👑 <b>{title}</b> — ДЕСЯТКА! {n} нашёл новый шедевр манги. Запишите.",
            "🏆 10/10! <b>{title}</b> — в пантеоне {n} навсегда.",
            "💎 {n} поставил манге <b>{title}</b> десятку. Художник может гордиться.",
            "🌌 10/10 за <b>{title}</b>. {n} дочитал и сидит в тишине. Это говорит всё.",
            "🎌 Максимум! <b>{title}</b> — теперь часть {n}. Прямо в душу.",
            "🔮 <b>{title}</b> получает священную десятку. {n} не шутит.",
            "🗿 <b>{title}</b> — 10/10. {n} дочитал и уставился в стену. Шедевр.",
            "🎆 10/10 за <b>{title}</b>. {n} такое раздаёт по большим праздникам.",
        ],

    },  # конец "manga"

    # ────────────────────────────────
    #  ОБЩИЕ — изменение оценки (для аниме и манги одинаково)
    # ────────────────────────────────
    "score_changed": [
        "🔄 {n} пересмотрел оценку <b>{title}</b>: было {old}, стало {new}. Что-то изменилось.",
        "🤔 <b>{title}</b> переоценено: {old} → {new}. {n} явно что-то переосмыслил.",
        "🏹 {old} → {new} за <b>{title}</b>. {n} дал второй шанс (или отобрал).",
        "⚖️ Весы справедливости скорректированы: <b>{title}</b> теперь {new}/10 вместо {old}.",
        "✏️ {n} исправил оценку <b>{title}</b> с {old} на {new}. Бывает, мнения меняются.",
        "📊 Обновление рейтинга: <b>{title}</b> {old} → {new}. {n} не стоит на месте.",
    ],  # конец "score_changed"

    # ────────────────────────────────
    #  ИЗБРАННОЕ — добавление в favourites
    # ────────────────────────────────
    "favourites": {

        "anime": [
            "⭐ {n} добавил <b>{title}</b> в избранное. Это не просто хорошее аниме — это особенное.",
            "💫 <b>{title}</b> теперь в избранном у {n}. Значит, зацепило по-настоящему.",
            "🏅 Особая отметка: <b>{title}</b> попало в избранное {n}. Это дорогого стоит.",
            "✨ {n} выделил <b>{title}</b> среди всех. Избранное — это серьёзно.",
            "🌟 <b>{title}</b> — в избранном. {n} не раздаёт такое направо и налево.",
            "⭐ {n} добавил <b>{title}</b> в избранное. Это уже не просто «понравилось».",
            "💫 <b>{title}</b> теперь в избранном у {n}. Зацепило так, что не отпускает.",
            "🏅 {n} выделил <b>{title}</b> среди всех. В избранное к нему попадает не каждый шедевр.",
            "🎖️ {n} отметил <b>{title}</b> как одно из любимых. Это говорит само за себя.",
            "❤️ <b>{title}</b> зацепило {n} по-настоящему — прямиком в избранное.",
            "🔮 <b>{title}</b> в избранном у {n}. Из тех, что остаются с тобой надолго.",
        ],

        "manga": [
            "⭐ {n} добавил мангу <b>{title}</b> в избранное. Художник может гордиться.",
            "💫 <b>{title}</b> теперь в избранном у {n}. Среди всей прочитанной манги — особняком.",
            "🏅 Особая отметка: манга <b>{title}</b> в избранном {n}. Это не просто хорошо.",
            "✨ {n} выделил <b>{title}</b> среди всей манги. Редкий знак уважения.",
            "🌟 <b>{title}</b> — в избранном. {n} знает толк в хорошей манге.",
            "🏅 {n} выделил <b>{title}</b> среди всей прочитанной манги. А прочитано немало.",
            "🌟 {n} занёс мангу <b>{title}</b> в избранное. Высшая полка, рядом с любимыми.",
            "❤️ <b>{title}</b> легла {n} на душу — прямиком в избранное.",
            "🖋️ {n} отметил <b>{title}</b> как одну из любимых. Художник может собой гордиться.",
        ],

        "character": [
            "❤️ {n} добавил персонажа <b>{title}</b> в избранное. Кто-то явно запал в душу.",
            "💙 <b>{title}</b> — в избранных персонажах {n}. Это симпатия серьёзная.",
            "🎭 {n} выделил <b>{title}</b> среди всех персонажей. Характер оценён.",
            "✨ <b>{title}</b> попал в избранное. {n} явно не равнодушен.",
            "🌟 Новый любимый персонаж {n} — <b>{title}</b>. Запоминаем.",
        ],

        "person": [
            "🎌 {n} добавил <b>{title}</b> в избранных людей индустрии. Уважение оказано.",
            "👏 <b>{title}</b> — в избранном у {n}. Талант замечен и отмечен.",
            "✨ {n} выделил <b>{title}</b> среди людей аниме-индустрии. Достойный выбор.",
            "🌟 <b>{title}</b> попал в избранное {n}. Вклад в аниме оценён по достоинству.",
        ],
    },  # конец "favourites"
}


# ═══════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════

def load_seen_ids() -> set[int]:
    """Загружаем уже виденные ID из JSON-файла."""
    path = Path(SEEN_IDS_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("seen_ids", []))
        except (json.JSONDecodeError, KeyError):
            log.warning("Не удалось прочитать %s, начинаем с нуля.", SEEN_IDS_FILE)
    return set()


def _atomic_write(path: "Path | str", data: str) -> None:
    """Атомарная запись файла: пишем во временный файл, затем rename.
    Защищает от повреждения данных при аварийном завершении процесса.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp  = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)  # атомарная операция на уровне ОС


def save_seen_ids(seen_ids: set[int]) -> None:
    """Сохраняем виденные ID в JSON-файл (атомарно)."""
    _atomic_write(
        SEEN_IDS_FILE,
        json.dumps({"seen_ids": list(seen_ids)}, ensure_ascii=False, indent=2),
    )


def load_subscribers() -> dict[int, str]:
    """
    Загружаем подписчиков из JSON.
    Формат хранилища: {"subscribers": {"123456": "Имя", "789012": "Имя2"}}
    Возвращаем dict[chat_id: int, name: str].
    """
    path = Path(SUBS_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {int(k): v for k, v in data.get("subscribers", {}).items()}
        except (json.JSONDecodeError, KeyError, ValueError):
            log.warning("Не удалось прочитать %s, начинаем с пустого списка.", SUBS_FILE)
    return {}


def save_subscribers(subs: dict[int, str]) -> None:
    """Сохраняем подписчиков в JSON (атомарно)."""
    _atomic_write(
        SUBS_FILE,
        json.dumps({"subscribers": {str(k): v for k, v in subs.items()}}, ensure_ascii=False, indent=2),
    )


def load_seen_favourites() -> set[str]:
    """
    Загружаем ID уже виденных записей избранного.
    Ключи хранятся как строки вида "anime_123" — категория + ID,
    чтобы избежать коллизий между разными категориями с одинаковыми ID.
    """
    path = Path(SEEN_FAVS_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("seen_favourites", []))
        except (json.JSONDecodeError, KeyError):
            log.warning("Не удалось прочитать %s, начинаем с нуля.", SEEN_FAVS_FILE)
    return set()


def save_seen_favourites(seen: set[str]) -> None:
    """Сохраняем виденные ID избранного в JSON (атомарно)."""
    _atomic_write(
        SEEN_FAVS_FILE,
        json.dumps({"seen_favourites": list(seen)}, ensure_ascii=False, indent=2),
    )


def get_media_info(entry: dict) -> tuple[str, str]:
    """
    Возвращает (media_type, kind) для записи истории.

    media_type: "anime" | "manga"
    kind:       строка из API, например "tv", "movie", "ova", "manga", "one_shot" и т.д.

    Shikimori кладёт в target.type  → "Anime" или "Manga"
                        в target.kind → "tv" / "movie" / "ova" / "manga" / "one_shot" / ...
    """
    target = entry.get("target") or {}

    raw_type = (target.get("type") or "").lower()   # "anime" / "manga" / ""
    kind     = (target.get("kind") or "").lower()   # "tv", "movie", "ova", "manga", ...

    # Манга — если явно указан тип Manga, либо kind из «мангового» набора
    if raw_type == "manga" or kind in MANGA_KINDS:
        return "manga", kind

    return "anime", kind


def is_relevant(media_type: str, kind: str) -> bool:
    """
    Проверяем, стоит ли вообще уведомлять об этой записи.

    Аниме: разрешаем только tv, movie, ova, ona.
           Спецвыпуски (special, tv_special), клипы (music, pv, cm) — пропускаем.
    Манга: запрещаем only one_shot и doujin, всё остальное разрешено.
           Если kind пустой (API не вернул) — пропускаем на всякий случай.
    """
    if not kind:
        # API не вернул kind — лучше пропустить, чем засорить чат
        log.debug("kind отсутствует для media_type=%s, запись пропущена.", media_type)
        return False

    if media_type == "anime":
        return kind in ANIME_ALLOWED_KINDS

    if media_type == "manga":
        return kind not in MANGA_BLOCKED_KINDS

    return False


def _strip_html(text: str) -> str:
    """Удаляем HTML-теги из строки.
    Shikimori может возвращать description с тегами вроде <b>7</b> —
    без очистки регулярки не найдут число.
    """
    return re.sub(r"<[^>]+>", "", text)


def extract_score_change(description: str) -> tuple[int, int] | None:
    """
    Парсим «изменена оценка с X на Y» → возвращаем (old, new).
    Если не распознали — None.
    """
    desc = _strip_html(description)
    match = re.search(
        r"изменена\s+оценка\s+с\s+(\d+)\s+на\s+(\d+)",
        desc, re.IGNORECASE
    )
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def extract_score(description: str) -> int | None:
    """
    Пытаемся вытащить оценку из строки описания.
    Реальные форматы Shikimori (судя по тестам):
      "оценено на 9"          <- основной русский формат
      "выставил оценку 8"     <- альтернативный
      "rated 7" / "scored 7"  <- английский
    """
    desc = _strip_html(description)
    # Основной русский формат: «оценено на 9» (число может быть в <b>9</b>)
    match = re.search(r"оценено\s+на\s+(\d+)", desc, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Альтернативный русский: «выставил/выставила оценку 9»
    match = re.search(r"(?:выставил|выставила)\s+оценку\s+(\d+)", desc, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Английский: «rated 7» или «scored 7»
    match = re.search(r"(?:rated?|score[d]?)\s+(\d+)", desc, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def classify_event(description: str) -> str:
    """
    Определяем тип события по полю description из API Shikimori.
    Возвращаем ключ из суб-словаря MESSAGES[media_type].

    Реальные значения description (проверено на живом API):
      "добавлено в список"       -> planned
      "просматриваю"             -> watching
      "изменена оценка с X на Y" -> score_changed
      "смотрю"                   -> watching
      "читаю"                    -> watching
      "пересматриваю"            -> rewatching
      "перечитываю"              -> rewatching
      "брошено"                  -> dropped
      "просмотрено"              -> completed  (без оценки)
      "прочитано"                -> completed  (без оценки)
      "оценено на 9"             -> completed  (с оценкой, парсим отдельно)
    """
    desc = _strip_html(description).lower()

    # Порядок важен: специфичные — выше, чтобы не поглотил более общий паттерн

    # Score change — проверяем первым, т.к. содержит «оценка» и может пересечься
    if any(w in desc for w in ["изменена оценка", "score changed"]):
        return "score_changed"

    # Dropped — проверяем первым, т.к. «брошено» короткое и не пересекается
    if any(w in desc for w in [
        "dropped", "брошено", "бросил", "бросила", "удалил из", "удалила из",
    ]):
        return "dropped"

    # Rewatching / re-reading (пере-)
    if any(w in desc for w in [
        "rewatching", "re-reading",
        "пересматриваю", "перечитываю",
        "перечитывает", "пересматривает",
    ]):
        return "rewatching"

    # Planned — "добавлено в список" это главный реальный формат
    if any(w in desc for w in [
        "добавлено в список", "добавлено",
        "planned", "планирует",
        "добавил в планируемое", "добавила в планируемое",
        "want to watch", "want to read",
    ]):
        return "planned"

    # Watching / reading — текущий просмотр
    if any(w in desc for w in [
        "смотрю", "просматриваю", "читаю",
        "watching", "reading", "смотрит", "читает",
        "начал смотреть", "начала смотреть",
        "начал читать", "начала читать",
    ]):
        return "watching"

    # Всё остальное: "просмотрено", "прочитано", "оценено на N" -> completed
    return "completed"


def build_message(entry: dict) -> str:
    """
    Формируем итоговое сообщение для одной записи истории.
    entry — объект из API /api/users/{user}/history.

    Название тайтла кликабельно (ссылка зашита в него), отдельной строки
    со ссылкой нет — единообразно с /favs и отчётами. Метку времени не
    добавляем: Telegram сам показывает время сообщения, а наличие новых записей 
    проверяется каждые 15 минут.
    """
    # Тип медиа и конкретный вид (kind) — нужны для выбора банка сообщений
    media_type, _kind = get_media_info(entry)
    bank = MESSAGES[media_type]

    # Название тайтла — предпочитаем русское, экранируем для HTML
    target = entry.get("target") or {}
    title_ru = target.get("russian") or ""
    title_en = target.get("name") or "???"
    title_text = h(title_ru if title_ru else title_en)

    # Зашиваем ссылку в название (если есть url) — кликабельно прямо в тексте
    target_url = _rel_url(target.get("url"))
    title = (f'<a href="{SHIKI_BASE_URL}{target_url}">{title_text}</a>'
             if target_url else title_text)

    description = entry.get("description", "") or ""
    event_type = classify_event(description)

    score = None

    if event_type == "score_changed":
        # Изменение оценки — берём шаблон из общего банка, не из anime/manga
        change = extract_score_change(description)
        old_score, new_score = change if change else (None, None)
        template = random.choice(MESSAGES["score_changed"])
        text = template.format(
            n=DISPLAY_NAME,
            title=title,
            old=old_score if old_score is not None else "?",
            new=new_score if new_score is not None else "?",
        )
    elif event_type == "completed":
        # Завершение — уточняем по оценке
        score = extract_score(description)
        if score is None:
            key = "completed_no_score"
        elif score <= 3:
            key = "completed_score_low"
        elif score <= 6:
            key = "completed_score_mid"
        elif score <= 9:
            key = "completed_score_high"
        else:
            key = "completed_score_perfect"
        template = random.choice(bank[key])
        text = template.format(
            n=DISPLAY_NAME,
            title=title,
            score=score if score is not None else "?",
        )
    else:
        key = event_type
        template = random.choice(bank[key])
        text = template.format(
            n=DISPLAY_NAME,
            title=title,
            score="?",
        )

    return text

# ═══════════════════════════════════════════════════════════════════
#  СТАТИСТИКА — КОНСТАНТЫ И GRAPHQL
# ═══════════════════════════════════════════════════════════════════

# Статусы Shikimori, которые мы учитываем
_STAT_STATUSES: frozenset[str] = frozenset({
    "planned", "watching", "rewatching", "completed", "on_hold", "dropped",
})

# Локализация origin (источник адаптации аниме)
_ORIGIN_RU: dict[str, str] = {
    "original":         "Оригинал",
    "manga":            "Манга",
    "manhwa":           "Манхва",
    "manhua":           "Маньхуа",
    "light_novel":      "Ранобэ",
    "novel":            "Новелла",
    "visual_novel":     "Визуальная новелла",
    "game":             "Игра",
    "card_game":        "Карточная игра",
    "music":            "Музыка",
    "book":             "Книга",
    "web_manga":        "Веб-манга",
    "web_novel":        "Веб-новелла",
    "four_koma_manga":  "Ёнкома",
    "picture_book":     "Иллюстрированная книга",
    "radio":            "Радио",
    "other":            "Другое",
    "unknown":          "Неизвестно",
}

# Локализация возрастного рейтинга
_RATING_RU: dict[str, str] = {
    "none":   "Без рейтинга",
    "g":      "G",
    "pg":     "PG",
    "pg_13":  "PG-13",
    "r":      "R-17",
    "r_plus": "R+",
    "rx":     "Rx",
}

# Локализация kind для разбивки в шапке статистики
_KIND_RU_ANIME: dict[str, str] = {
    "tv":    "Сериалы",
    "movie": "Фильмы",
    "ova":   "OVA",
    "ona":   "ONA",
}

_KIND_RU_MANGA: dict[str, str] = {
    "manga":  "Манга",
    "manhwa": "Манхва",
    "manhua": "Маньхуа",
    "novel":  "Новеллы",
    "ranobe": "Ранобэ",
}

# GraphQL: метаданные аниме по списку id.
# censored: false — обязательно, иначе теряются hentai/yaoi/yuri тайтлы.
_GQL_ANIME = """
query($ids: String!) {
  animes(ids: $ids, limit: 50, censored: false) {
    id
    url
    kind
    score
    rating
    origin
    duration
    episodes
    airedOn { year }
    studios { name }
    genres { russian name kind }
  }
}
"""

# GraphQL: метаданные манги по списку id.
_GQL_MANGA = """
query($ids: String!) {
  mangas(ids: $ids, limit: 50, censored: false) {
    id
    url
    kind
    score
    chapters
    volumes
    airedOn { year }
    publishers { name }
    genres { russian name kind }
  }
}
"""

# ─────────────────────────────────────────────
#  In-memory кэш stats_all для команд (TTL 5 минут).
#  stats_all меняется редко (раз в старт + раз в квартал),
#  поэтому короткого TTL достаточно, чтобы не читать файл на каждый /stats.
# ─────────────────────────────────────────────
_stats_all_cache: dict | None = None
_stats_all_cache_ts: float = 0.0
_STATS_ALL_CACHE_TTL: int = 300  # секунд


# ═══════════════════════════════════════════════════════════════════
#  ЗАГРУЗКА / ЧТЕНИЕ ИСТОЧНИКОВ ДАННЫХ
# ═══════════════════════════════════════════════════════════════════

async def fetch_list_export(session: aiohttp.ClientSession, media: str) -> list[dict] | None:
    """
    Скачиваем публичный экспорт списка пользователя.
    media: "anime" | "manga"
    Возвращает список записей или None при любой ошибке.

    Формат записи:
      {target_title, target_title_ru, target_id, target_type,
       score, status, rewatches, episodes|volumes|chapters, text}
    """
    url = LIST_EXPORT_ANIME if media == "anime" else LIST_EXPORT_MANGA
    try:
        async with session.get(
            url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                log.warning("fetch_list_export(%s): HTTP %d", media, resp.status)
                return None
            data = await resp.json(content_type=None)
            if not isinstance(data, list):
                log.warning("fetch_list_export(%s): ответ не список.", media)
                return None
            return data
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.error("fetch_list_export(%s): ошибка запроса: %s", media, e)
        return None
    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
        log.error("fetch_list_export(%s): не удалось разобрать ответ: %s", media, e)
        return None


async def _gql_request(
    session: aiohttp.ClientSession, query: str, variables: dict,
) -> dict | None:
    """
    Один GraphQL-запрос. Возвращает поле data или None при ошибке.
    Частичные данные (data + errors) возвращаются — пусть caller решает.
    """
    try:
        async with session.post(
            GRAPHQL_URL,
            headers={**HEADERS, "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                log.warning("_gql_request: HTTP %d", resp.status)
                return None
            try:
                payload = await resp.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
                log.warning("_gql_request: не удалось распарсить ответ: %s", e)
                return None
            if "errors" in payload:
                log.warning("_gql_request: GraphQL errors: %s", payload["errors"])
            return payload.get("data")
    except asyncio.TimeoutError:
        log.warning("_gql_request: таймаут (20 с)")
        return None
    except aiohttp.ClientError as e:
        log.error("_gql_request: ошибка клиента: %s", e)
        return None


def _parse_genres(genres_raw: list, kind_filter: str) -> list[str]:
    """Имена жанров заданного kind (genre|theme|demographic). Предпочитаем русское."""
    out = []
    for g in genres_raw or []:
        if isinstance(g, dict) and g.get("kind") == kind_filter:
            name = (g.get("russian") or g.get("name") or "").strip()
            if name:
                out.append(name)
    return out


async def fetch_meta_batch(media: str, ids: list[str]) -> dict[str, dict]:
    """
    Запрашиваем метаданные тайтлов через GraphQL батчами по 50.
    media: "anime" | "manga"
    Возвращает {str(id): meta_dict}. При сбое отдельного батча — пропускаем его,
    остальные данные сохраняем (частичный результат лучше пустого).
    """
    clean = list({str(i).strip() for i in ids if str(i).strip()})
    if not clean:
        return {}

    query = _GQL_ANIME if media == "anime" else _GQL_MANGA
    result: dict[str, dict] = {}

    async with aiohttp.ClientSession() as session:
        # Батчим по 50 (ограничение limit в GraphQL)
        for i in range(0, len(clean), 50):
            batch = clean[i:i + 50]
            data = await _gql_request(session, query, {"ids": ",".join(batch)})
            key = "animes" if media == "anime" else "mangas"
            for item in ((data or {}).get(key) or []):
                try:
                    item_id = str(item.get("id") or "")
                    if not item_id:
                        continue
                    genres_raw = item.get("genres") or []
                    meta = {
                        "url":         _rel_url(item.get("url")),
                        "kind":        (item.get("kind") or "").lower(),
                        "year":        (item.get("airedOn") or {}).get("year"),
                        "shiki_score": _safe_float(item.get("score")),
                        "genres":      _parse_genres(genres_raw, "genre"),
                        "themes":      _parse_genres(genres_raw, "theme"),
                        "demographic": _parse_genres(genres_raw, "demographic"),
                    }
                    if media == "anime":
                        origin_raw = (item.get("origin") or "").strip()
                        rating_raw = (item.get("rating") or "").strip()
                        meta.update({
                            "duration":       item.get("duration"),   # мин/эп
                            "episodes_total": item.get("episodes"),
                            "rating":         _RATING_RU.get(rating_raw, rating_raw or None),
                            "origin":         _ORIGIN_RU.get(origin_raw, origin_raw or None),
                            "studios":        [s["name"] for s in (item.get("studios") or []) if s.get("name")],
                        })
                    else:
                        meta.update({
                            "chapters_total": item.get("chapters"),
                            "volumes_total":  item.get("volumes"),
                            "publishers":     [p["name"] for p in (item.get("publishers") or []) if p.get("name")],
                        })
                    result[item_id] = meta
                except Exception as e:
                    log.warning("fetch_meta_batch(%s): ошибка парсинга id=%s: %s",
                                media, item.get("id"), e)

            # Пауза между батчами — не триггерим rate limit (5 req/sec)
            if i + 50 < len(clean):
                await asyncio.sleep(0.5)

    log.info("fetch_meta_batch(%s): получено %d/%d тайтлов.", media, len(result), len(clean))
    return result


# ═══════════════════════════════════════════════════════════════════
#  stats_all.json — ЗАГРУЗКА / СОХРАНЕНИЕ
# ═══════════════════════════════════════════════════════════════════

def _empty_stats_all() -> dict:
    """Пустая структура stats_all.json."""
    return {
        "updated_at": None,
        "anime": {"titles": {}, "aggregates": {}},
        "manga": {"titles": {}, "aggregates": {}},
        "favourites": {"anime": [], "manga": [], "ranobe": [],
                       "characters": [], "people": []},
    }


def load_stats_all(use_cache: bool = True) -> dict:
    """
    Загружаем stats_all.json (с коротким in-memory кэшем).
    При ошибке — пустая структура, бот не падает.
    """
    global _stats_all_cache, _stats_all_cache_ts

    if use_cache and _stats_all_cache is not None:
        age = _utcnow().timestamp() - _stats_all_cache_ts
        if age < _STATS_ALL_CACHE_TTL:
            return _stats_all_cache

    data = _empty_stats_all()
    try:
        if STATS_ALL_FILE.exists():
            raw = json.loads(STATS_ALL_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "anime" in raw and "manga" in raw:
                data = raw
            else:
                log.warning("load_stats_all: неожиданная структура, сбрасываем.")
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("load_stats_all: не удалось прочитать файл: %s", e)

    _stats_all_cache = data
    _stats_all_cache_ts = _utcnow().timestamp()
    return data


def save_stats_all(data: dict) -> None:
    """Сохраняем stats_all.json атомарно + обновляем кэш."""
    global _stats_all_cache, _stats_all_cache_ts
    try:
        data["updated_at"] = _utcnow().isoformat()
        _atomic_write(STATS_ALL_FILE, json.dumps(data, ensure_ascii=False, indent=2))
        _stats_all_cache = data
        _stats_all_cache_ts = _utcnow().timestamp()
    except Exception as e:
        log.error("save_stats_all: не удалось записать файл: %s", e)


# ═══════════════════════════════════════════════════════════════════
#  ПОСТРОЕНИЕ titles{} ИЗ list_export + МЕТАДАННЫХ
# ═══════════════════════════════════════════════════════════════════


def _merge_title_record(media: str, export_row: dict, meta: dict | None) -> dict:
    """
    Собираем одну запись titles{} из строки экспорта и метаданных GraphQL.
    meta может быть None — тогда метаданные пустые, но пользовательские данные есть.
    """
    meta = meta or {}
    record = {
        "title":    export_row.get("target_title_ru") or export_row.get("target_title") or "???",
        "title_en": export_row.get("target_title") or "",
        "score":    _safe_int(export_row.get("score")),       # 0 = без оценки
        "status":   (export_row.get("status") or "").lower(),
        "rewatches": _safe_int(export_row.get("rewatches")),
        "url":       meta.get("url") or "",
        "kind":      meta.get("kind") or "",
        "year":      meta.get("year"),
        "shiki_score": meta.get("shiki_score"),
        "genres":      meta.get("genres") or [],
        "themes":      meta.get("themes") or [],
        "demographic": meta.get("demographic") or [],
    }
    if media == "anime":
        record.update({
            "episodes_watched": _safe_int(export_row.get("episodes")),
            "episodes_total":   meta.get("episodes_total"),
            "duration":         meta.get("duration"),
            "rating":           meta.get("rating"),
            "origin":           meta.get("origin"),
            "studios":          meta.get("studios") or [],
        })
    else:
        record.update({
            "chapters_read":  _safe_int(export_row.get("chapters")),
            "volumes_read":   _safe_int(export_row.get("volumes")),
            "chapters_total": meta.get("chapters_total"),
            "volumes_total":  meta.get("volumes_total"),
            "publishers":     meta.get("publishers") or [],
        })
    return record


# ═══════════════════════════════════════════════════════════════════
#  ПЕРЕСЧЁТ АГРЕГАТОВ
# ═══════════════════════════════════════════════════════════════════

def _bump(counter: dict, key, n: int = 1) -> None:
    """counter[key] += n, с защитой от None/пустых ключей."""
    if key is None or key == "":
        return
    counter[str(key)] = counter.get(str(key), 0) + n


def recompute_aggregates(media: str, titles: dict, existing_by_quarter: dict | None = None) -> dict:
    """
    Полный пересчёт агрегатов из titles{}.
    by_quarter не вычисляется отсюда (он накапливается при ротации квартала) —
    передаём существующий, чтобы не потерять.

    Все жанрово-оценочные агрегаты считаются ТОЛЬКО по completed.
    Счётчики статусов (total_*) — по всем записям.
    """
    agg: dict = {
        "total_completed":  0,
        "total_dropped":    0,
        "total_watching":   0,
        "total_planned":    0,
        "total_on_hold":    0,
        "total_rewatching": 0,
        "score_dist":   {},
        "genres":       {},
        "themes":       {},
        "demographic":  {},
        "kinds":        {},
        "by_year":      {},
        "by_quarter":   existing_by_quarter or {},
        "avg_shiki_completed": None,  # средний рейтинг Shikimori по завершённым с оценкой
    }
    if media == "anime":
        agg.update({
            "studios": {}, "origins": {}, "ratings": {},
            "total_episodes_watched": 0,
            "total_hours_watched":    0.0,
        })
    else:
        agg.update({
            "publishers": {},
            "total_chapters_read": 0,
            "total_volumes_read":  0,
        })

    status_counter = {
        "completed":  "total_completed",
        "dropped":    "total_dropped",
        "watching":   "total_watching",
        "planned":    "total_planned",
        "on_hold":    "total_on_hold",
        "rewatching": "total_rewatching",
    }

    total_minutes = 0
    shiki_scores: list[float] = []   # рейтинги Shikimori по completed с личной оценкой

    for rec in titles.values():
        status = rec.get("status", "")
        # Счётчик статусов
        if status in status_counter:
            agg[status_counter[status]] += 1

        # Жанровые/оценочные агрегаты — только completed
        if status != "completed":
            continue

        _bump(agg["score_dist"], rec.get("score", 0))
        # Рейтинг Shikimori — собираем только если есть личная оценка (для честного сравнения)
        if _safe_int(rec.get("score")) > 0 and isinstance(rec.get("shiki_score"), (int, float)):
            shiki_scores.append(float(rec["shiki_score"]))
        for g in rec.get("genres", []):
            _bump(agg["genres"], g)
        for t in rec.get("themes", []):
            _bump(agg["themes"], t)
        for d in rec.get("demographic", []):
            _bump(agg["demographic"], d)
        _bump(agg["kinds"], rec.get("kind"))
        if rec.get("year"):
            _bump(agg["by_year"], rec.get("year"))

        if media == "anime":
            for s in rec.get("studios", []):
                _bump(agg["studios"], s)
            _bump(agg["origins"], rec.get("origin"))
            _bump(agg["ratings"], rec.get("rating"))

            eps = _safe_int(rec.get("episodes_watched"))
            agg["total_episodes_watched"] += eps
            dur = rec.get("duration")
            if isinstance(dur, int) and dur > 0 and eps > 0:
                total_minutes += dur * eps
        else:
            for p in rec.get("publishers", []):
                _bump(agg["publishers"], p)
            agg["total_chapters_read"] += _safe_int(rec.get("chapters_read"))
            agg["total_volumes_read"]  += _safe_int(rec.get("volumes_read"))

    if media == "anime":
        agg["total_hours_watched"] = round(total_minutes / 60, 1)

    if shiki_scores:
        agg["avg_shiki_completed"] = round(sum(shiki_scores) / len(shiki_scores), 2)

    return agg


# ═══════════════════════════════════════════════════════════════════
#  СИНХРОНИЗАЦИЯ stats_all С list_export
# ═══════════════════════════════════════════════════════════════════

async def _collect_favourites(
    session: "aiohttp.ClientSession | None",
    stats: dict,
    fav: dict | None = None,
) -> dict:
    """
    Собирает избранное в структуру stats["favourites"].

    fav: если передан готовый ответ API (например, уже скачанный в
    check_and_notify_favourites) — используем его и НЕ ходим в сеть повторно.
    Если None — фетчим сами через session.

    Для аниме/манги/ранобэ джойнит оценку и название из titles{} (если тайтл
    там есть); если нет — берёт название из ответа API. Персонажи/люди —
    имя+ссылка из API (в titles{} их нет, ссылки/оценки не будет — это ок).

    fetch_favourites возвращает None при сбое — тогда оставляем прежнее
    избранное (не затираем хорошие данные пустотой при ошибке сети).

    Категоризация Shikimori ненадёжна (режиссёры лежат в mangakas, и т.п.),
    поэтому people+mangakas+seyu+producers сливаем в один блок "people"
    («Люди индустрии»). Ранобэ — отдельный блок, но джойнит по namespace манги.
    """
    if fav is None:
        if session is None:
            # Защита от вызова с обоими None: fetch_favourites(None) упал бы
            # внутри на session.get(...). В норме не случается (sync_stats_all
            # передаёт session, check_and_notify_favourites — готовый fav).
            log.error("_collect_favourites: переданы и fav=None, и session=None — оставляем прежнее.")
            return stats
        fav = await fetch_favourites(session)
    if fav is None:
        log.info("_collect_favourites: запрос избранного не удался — оставляем прежнее.")
        return stats

    # API-категория → (выходной ключ stats, ключ titles для джойна или None).
    # ranobe джойнит по titles манги: id ранобэ лежат в namespace манги,
    # и если тайтл есть в списке пользователя — подтянем ссылку/оценку.
    cat_map = {
        "animes":     ("anime",      "anime"),
        "mangas":     ("manga",      "manga"),
        "ranobe":     ("ranobe",     "manga"),
        "characters": ("characters", None),
        "people":     ("people",     None),
        "mangakas":   ("people",     None),
        "seyu":       ("people",     None),
        "producers":  ("people",     None),
    }

    result: dict[str, list] = {
        "anime": [], "manga": [], "ranobe": [], "characters": [], "people": [],
    }
    # Защита от дублей в слитом блоке людей (на случай, если Shikimori положит
    # одного человека в несколько категорий — в норме не случается).
    seen_people: set[str] = set()

    for api_cat, (out_key, media_key) in cat_map.items():
        items = fav.get(api_cat) or []
        titles = stats.get(media_key, {}).get("titles", {}) if media_key else {}
        for item in items:
            iid = item.get("id")
            if iid is None:
                continue
            tid = str(iid)
            if out_key == "people":
                if tid in seen_people:
                    continue
                seen_people.add(tid)
            # russian бывает пустой строкой (не null) — фолбэк на name,
            # иначе получим пустую жирную строку.
            api_name = item.get("russian") or item.get("name") or "???"
            api_url = _rel_url(item.get("url"))

            if media_key and tid in titles:
                # Джойн с архивом: берём название и оценку оттуда
                rec = titles[tid]
                entry = {
                    "id": tid,
                    "title": rec.get("title") or api_name,
                    "url": _rel_url(rec.get("url")) or api_url,
                }
                score = _safe_int(rec.get("score"))
                if score > 0:
                    entry["score"] = score
            else:
                # Нет в архиве (или персонаж/человек) — только имя+ссылка
                entry = {"id": tid, "title": api_name, "url": api_url}
            result[out_key].append(entry)

    stats["favourites"] = result
    counts = {k: len(v) for k, v in result.items() if v}
    log.info("_collect_favourites: собрано избранное: %s", counts or "пусто")
    return stats


async def sync_stats_all() -> dict:
    """
    Главная функция актуализации stats_all.

    1. Скачиваем list_export для аниме и манги.
    2. Сверяем с titles{} в stats_all — находим новые/изменившиеся записи
       (новый id, либо изменился score/status/episodes/chapters).
    3. Для записей, у которых ещё нет метаданных (новый id) — батч GraphQL.
       Для существ, у которых поменялся только пользовательский стейт —
       обновляем поля из экспорта, метаданные не перезапрашиваем.
    4. Пересчитываем агрегаты, сохраняем.

    Вызывается при старте бота и периодически из цикла. Уведомлений не шлёт.
    Возвращает (stats_all, ok): ok=False, если ни один экспорт не скачался
    (тогда stats_all — прежний, нетронутый); ok=True при частичном/полном успехе.
    """
    stats = load_stats_all(use_cache=False)

    async with aiohttp.ClientSession() as session:
        export_anime = await fetch_list_export(session, "anime")
        export_manga = await fetch_list_export(session, "manga")

    if export_anime is None and export_manga is None:
        log.warning("sync_stats_all: оба экспорта недоступны — пропускаем синхронизацию.")
        return stats, False

    changed = False

    for media, export in (("anime", export_anime), ("manga", export_manga)):
        if export is None:
            log.info("sync_stats_all: экспорт %s недоступен, пропускаем эту половину.", media)
            continue

        titles = stats[media]["titles"]

        # Релевантные строки экспорта: с валидным id и известным статусом
        valid_rows: dict[str, dict] = {}
        for row in export:
            tid = str(row.get("target_id") or "")
            status = (row.get("status") or "").lower()
            if tid and status in _STAT_STATUSES:
                valid_rows[tid] = row

        # ID, которым нужны метаданные (отсутствуют в titles)
        new_ids = [tid for tid in valid_rows if tid not in titles]

        # Ремонт битой меты (Codacy / баг «ваншоты не фильтруются»):
        # записи с пустым kind — это тайтлы, у которых мета не доехала при
        # первом заносе (GraphQL не вернул элемент → ВСЕ мета-поля пусты разом).
        # Они навсегда оставались в titles{} (new_ids их не видит, самоочистка
        # пропускает пустой kind) и, если completed, врали в счётчике.
        # Дозапрашиваем их повторно. Анонсы (вид ещё неизвестен) вернут снова
        # пустой kind — это обрабатывается как no-op ниже, без записи на диск.
        retry_ids = [
            tid for tid in valid_rows
            if tid in titles and not (titles[tid].get("kind") or "")
        ]

        # Подтягиваем метаданные: для новых + для ремонта битых
        need_meta = new_ids + retry_ids
        meta_map: dict[str, dict] = {}
        if need_meta:
            log.info("sync_stats_all(%s): тайтлов для обогащения: %d (новых %d, ремонт %d)",
                     media, len(need_meta), len(new_ids), len(retry_ids))
            try:
                meta_map = await fetch_meta_batch(media, need_meta)
            except Exception as e:
                log.error("sync_stats_all(%s): fetch_meta_batch упал: %s", media, e)

        skipped_irrelevant = 0
        repaired = 0

        # Обновляем / создаём записи
        for tid, row in valid_rows.items():
            if tid in titles:
                rec = titles[tid]

                # Ремонт битой меты: запись с пустым kind, и сейчас GraphQL
                # вернул непустой kind → пересобираем ЦЕЛИКОМ (url/year/жанры/
                # kind — всё, что побилось вместе с kind). Дальнейшая
                # самоочистка по kind вынесет ставшие нерелевантными (ваншоты).
                # Если мета снова пустая (анонс) — не трогаем, no-op (без
                # changed), чтобы не сохранять файл каждые 6 часов впустую.
                if not (rec.get("kind") or ""):
                    fresh = meta_map.get(tid)
                    if fresh and (fresh.get("kind") or ""):
                        titles[tid] = _merge_title_record(media, row, fresh)
                        repaired += 1
                        changed = True
                        continue

                # Существующая запись — обновляем только пользовательский стейт,
                # метаданные (genres/studios/...) уже есть, не трогаем.
                new_score  = _safe_int(row.get("score"))
                new_status = (row.get("status") or "").lower()
                new_rew    = _safe_int(row.get("rewatches"))
                if media == "anime":
                    new_progress = _safe_int(row.get("episodes"))
                    if (rec.get("score") != new_score or rec.get("status") != new_status
                            or rec.get("episodes_watched") != new_progress
                            or rec.get("rewatches") != new_rew):
                        rec["score"] = new_score
                        rec["status"] = new_status
                        rec["episodes_watched"] = new_progress
                        rec["rewatches"] = new_rew
                        changed = True
                else:
                    new_ch = _safe_int(row.get("chapters"))
                    new_vol = _safe_int(row.get("volumes"))
                    if (rec.get("score") != new_score or rec.get("status") != new_status
                            or rec.get("chapters_read") != new_ch
                            or rec.get("volumes_read") != new_vol
                            or rec.get("rewatches") != new_rew):
                        rec["score"] = new_score
                        rec["status"] = new_status
                        rec["chapters_read"] = new_ch
                        rec["volumes_read"] = new_vol
                        rec["rewatches"] = new_rew
                        changed = True
            else:
                # Новая запись. Фильтруем по kind тем же критерием, что и
                # уведомления (is_relevant): спецвыпуски, клипы, PV и т.п.
                # не должны попадать в статистику.
                # kind берём только из метаданных — в list_export его нет.
                meta = meta_map.get(tid)
                kind = (meta or {}).get("kind", "")
                # Если метаданные пришли и kind явно нерелевантный — пропускаем.
                # Если метаданные НЕ пришли (kind пустой, сбой API) — заносим
                # запись, чтобы не потерять реальный тайтл; отфильтруется
                # при следующей синхронизации, когда метаданные подтянутся.
                if kind and not is_relevant(media, kind):
                    skipped_irrelevant += 1
                    continue
                titles[tid] = _merge_title_record(media, row, meta)
                changed = True

        if skipped_irrelevant:
            log.info("sync_stats_all(%s): пропущено нерелевантных по kind: %d",
                     media, skipped_irrelevant)
        if repaired:
            log.info("sync_stats_all(%s): дозапрошена битая мета (kind был пуст): %d",
                     media, repaired)

        # Чистка существующих записей, чей kind не проходит фильтр
        # (самоочистка при изменении критерия или после обновления метаданных).
        stale_kind = [
            tid for tid, rec in titles.items()
            if rec.get("kind") and not is_relevant(media, rec["kind"])
        ]
        for tid in stale_kind:
            del titles[tid]
            changed = True
        if stale_kind:
            log.info("sync_stats_all(%s): удалено нерелевантных по kind из titles: %d",
                     media, len(stale_kind))

        # Удаляем записи, которых больше нет в экспорте (тайтл убран из списка)
        removed = [tid for tid in titles if tid not in valid_rows]
        for tid in removed:
            del titles[tid]
            changed = True
        if removed:
            log.info("sync_stats_all(%s): удалено отсутствующих в экспорте: %d", media, len(removed))

        # Пересчитываем агрегаты (сохраняя by_quarter)
        existing_bq = stats[media].get("aggregates", {}).get("by_quarter")
        stats[media]["aggregates"] = recompute_aggregates(media, titles, existing_bq)

    # Собираем избранное (джойн с уже построенными titles)
    try:
        async with aiohttp.ClientSession() as session:
            before = json.dumps(stats.get("favourites"), ensure_ascii=False, sort_keys=True)
            stats = await _collect_favourites(session, stats)
            after = json.dumps(stats.get("favourites"), ensure_ascii=False, sort_keys=True)
            if before != after:
                changed = True
    except Exception as e:
        log.error("sync_stats_all: сбор избранного упал: %s", e)

    if changed:
        save_stats_all(stats)
        log.info("sync_stats_all: stats_all.json обновлён.")
    else:
        log.info("sync_stats_all: изменений нет.")

    return stats, True


# ═══════════════════════════════════════════════════════════════════
#  stats_current.json — СОБЫТИЯ ТЕКУЩЕГО КВАРТАЛА
# ═══════════════════════════════════════════════════════════════════

def _empty_stats_current(period: str, tracking_since: str | None = None) -> dict:
    """
    Пустая структура текущего квартала.
    period_start — календарное начало квартала (для метки периода).
    tracking_since — реальная дата, с которой бот начал собирать события.
      При ротации = начало квартала (полные данные).
      При первом запуске в середине квартала = дата запуска (данные неполные).
      Если None — берётся календарное начало квартала.
    """
    qs = quarter_start().isoformat()
    return {
        "period": period,
        "period_start": qs,
        "tracking_since": tracking_since or qs,
        "last_report_sent": None,
        "last_backup_at": None,   # время последнего авто-бэкапа (для еженедельной отправки)
        "events": [],   # [{id, media, event, score, recorded_at}]
    }


def load_stats_current() -> dict:
    """
    Загружаем события текущего квартала. При ошибке/отсутствии — пустой квартал.

    Если файла ещё нет (истинно первый запуск), фиксируем tracking_since = max(
    начало квартала, сейчас). Это даёт честную дату «статистика собирается с …»,
    когда бота впервые запустили в середине квартала. Дата сразу сохраняется,
    чтобы не сбрасывалась при последующих перезапусках.
    """
    try:
        if STATS_CURRENT_FILE.exists():
            data = json.loads(STATS_CURRENT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "period" in data and "events" in data:
                # Бэкофилл для файлов, созданных до появления поля tracking_since
                if "tracking_since" not in data:
                    data["tracking_since"] = data.get("period_start") or quarter_start().isoformat()
                # Бэкофилл для файлов до появления last_backup_at (еженедельный авто-бэкап)
                data.setdefault("last_backup_at", None)
                return data
            log.warning("load_stats_current: неожиданная структура, сбрасываем.")
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("load_stats_current: %s", e)

    # Истинно первый запуск (или сброс) — фиксируем фактическую дату старта
    now = _utcnow()
    qs = quarter_start(now)
    tracking_since = (now if now > qs else qs).isoformat()
    fresh = _empty_stats_current(current_quarter(now), tracking_since=tracking_since)
    save_stats_current(fresh)
    log.info("load_stats_current: создан новый stats_current, отслеживание с %s.", tracking_since)
    return fresh


def save_stats_current(data: dict) -> None:
    try:
        _atomic_write(STATS_CURRENT_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        log.error("save_stats_current: %s", e)


def record_current_event(
    cur: dict, entry: dict, event_type: str, media_type: str, score: int | None,
) -> dict:
    """
    Фиксируем событие истории в stats_current (для хронологии квартала).
    Учитываем только значимые для статистики типы.
    Дедупликация: один (id, event_type) на квартал.
    """
    # Смена оценки внутри квартала: обновляем score уже записанного
    # completed-события того же тайтла. Если completed-события в этом квартале
    # нет (тайтл завершён в прошлом квартале/до старта отслеживания) —
    # игнорируем: в текущем отчёте его всё равно нет.
    if event_type == "score_changed":
        if score is None:
            return cur
        try:
            tid = str((entry.get("target") or {}).get("id") or "")
            if not tid:
                return cur
            for ev in cur.get("events", []):
                if ev.get("id") == tid and ev.get("event") == "completed":
                    if ev.get("score") != score:
                        ev["score"] = score
                        log.info("Обновлена оценка в квартале: id=%s → %s", tid, score)
                    break
        except Exception as e:
            log.error("record_current_event(score_changed): %s", e)
        return cur

    if event_type not in ("completed", "dropped", "planned", "rewatching"):
        return cur
    try:
        target = entry.get("target") or {}
        tid = str(target.get("id") or "")
        if not tid:
            return cur
        # Дедуп
        for ev in cur.get("events", []):
            if ev.get("id") == tid and ev.get("event") == event_type:
                return cur
        cur.setdefault("events", []).append({
            "id":          tid,
            "media":       media_type,
            "event":       event_type,
            "score":       score,
            "recorded_at": _utcnow().isoformat(),
        })
    except Exception as e:
        log.error("record_current_event: %s", e)
    return cur


# ═══════════════════════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ — ВСПОМОГАТЕЛЬНОЕ
# ═══════════════════════════════════════════════════════════════════

def _top_dict(counter: dict, n: int) -> list[tuple[str, int]]:
    """Топ-N пар (ключ, count) по убыванию."""
    return sorted(counter.items(), key=lambda x: x[1], reverse=True)[:n]


def _fmt_counter(counter: dict, n: int, sep: str = "  ·  ") -> str:
    """'Экшен (34) · Драма (28)'. Оставлено для совместимости/коротких строк."""
    return sep.join(f"{h(k)} ({v})" for k, v in _top_dict(counter, n))


def _section_header(emoji: str, title: str) -> str:
    """Акцентированный заголовок архиблока: '━━━━━ 🎬 АНИМЕ ━━━━━' (жирный)."""
    line = "━" * 5
    return f"<b>{line} {emoji} {h(title)} {line}</b>"


def _fmt_mono_rows(pairs: list[tuple[str, int]], show_percent: bool = False,
                   total: int = 0) -> str:
    """
    Моноширинный блок с выровненными колонками и точками-лидерами:
        Экшен ······· 66  46%
        Триллер ····· 45  31%
    pairs — [(имя, число), ...] (уже отсортированные, обрезанные).
    show_percent — добавить долю от total (только если total > 0).
    Возвращает строку в <code>...</code> или '' если pairs пуст.

    Кириллица и латиница в моноширинном Telegram занимают 1 знак,
    поэтому выравнивание по len() корректно.
    """
    if not pairs:
        return ""
    name_w = max(len(name) for name, _ in pairs)
    num_w  = max(len(str(c)) for _, c in pairs)
    rows = []
    for name, count in pairs:
        dots = "·" * (name_w - len(name) + 1)
        num_str = str(count).rjust(num_w)
        line = f"{name} {dots} {num_str}"
        if show_percent and total > 0:
            line += f"  {round(count / total * 100)}%"
        rows.append(line)
    return f"<code>{h(chr(10).join(rows))}</code>"


def _top_block(emoji: str, title: str, counter: dict, n: int,
               show_percent: bool = False, total: int = 0) -> list[str]:
    """
    Полный блок топа: заголовок-строка + моноширинные колонки.
    Возвращает список строк (для extend) или [] если counter пуст.
    """
    pairs = _top_dict(counter, n)
    if not pairs:
        return []
    body = _fmt_mono_rows(pairs, show_percent=show_percent, total=total)
    if not body:
        return []
    return [f"{emoji} <b>{h(title)}</b>", body]


def _fmt_kinds(kinds: dict, labels: dict) -> str:
    """Разбивка по типам: 'Сериалы 95 · Фильмы 12 · OVA 8'.
    Порядок — как в labels (tv/movie/ova/ona), неизвестные kind в конце.
    Возвращает '' если данных нет.
    """
    if not kinds:
        return ""
    parts = []
    # Сначала известные типы в порядке labels
    for key, name in labels.items():
        cnt = kinds.get(key, 0)
        if cnt:
            parts.append(f"{name} {cnt}")
    # Затем неизвестные (на случай если API подкинет новый kind)
    for key, cnt in kinds.items():
        if key not in labels and cnt:
            parts.append(f"{h(key)} {cnt}")
    return "  ·  ".join(parts)


def _fmt_score_dist(dist: dict) -> str:
    """Распределение оценок без нулей (0 = без оценки): '10×8 · 9×15'.
    Оставлено для обратной совместимости; в отчётах теперь используется
    вертикальный блок _score_dist_block.
    """
    pairs = [(int(s), c) for s, c in dist.items() if _safe_int(s) > 0]
    if not pairs:
        return "нет оценок"
    return "  ·  ".join(f"{s}×{c}" for s, c in sorted(pairs, reverse=True))


def _score_dist_block(dist: dict) -> list[str]:
    """
    Вертикальный блок распределения оценок:
        📊 Оценки
        ★10 ·· 5
         ★9 ·· 8
         ★8 · 19
    Оценка помечена ★, точки — лидеры к количеству (как в остальных блоках).
    Порядок — по убыванию оценки (10 → 1), не по количеству.
    Возвращает [] если оценок нет.
    """
    pairs = [(_safe_int(s), c) for s, c in dist.items() if _safe_int(s) > 0]
    if not pairs:
        return []
    pairs.sort(key=lambda x: x[0], reverse=True)
    # Ключ — '★N', выровняем по ширине самой длинной метки (★10 шире ★9)
    rows = [(f"★{score}", count) for score, count in pairs]
    body = _fmt_mono_rows(rows)
    return ["📊 <b>Оценки</b>", body] if body else []


def _status_block_anime(agg: dict) -> list[str]:
    """Вертикальный блок статусов для аниме."""
    rows = [
        ("Завершено", agg.get("total_completed", 0)),
        ("Брошено",   agg.get("total_dropped", 0)),
        ("Смотрю",    agg.get("total_watching", 0)),
        ("В планах",  agg.get("total_planned", 0)),
        ("Отложено",  agg.get("total_on_hold", 0)),
    ]
    rows = [(n, c) for n, c in rows if c]  # скрываем нулевые
    body = _fmt_mono_rows(rows)
    return ["📦 <b>Статусы</b>", body] if body else []


def _status_block_manga(agg: dict) -> list[str]:
    """Вертикальный блок статусов для манги."""
    rows = [
        ("Прочитано", agg.get("total_completed", 0)),
        ("Брошено",   agg.get("total_dropped", 0)),
        ("Читаю",     agg.get("total_watching", 0)),
        ("В планах",  agg.get("total_planned", 0)),
        ("Отложено",  agg.get("total_on_hold", 0)),
    ]
    rows = [(n, c) for n, c in rows if c]
    body = _fmt_mono_rows(rows)
    return ["📦 <b>Статусы</b>", body] if body else []


def _kinds_block(kinds: dict, labels: dict) -> list[str]:
    """Вертикальный блок типов (Сериалы/Фильмы/OVA или Манга/Манхва/...)."""
    if not kinds:
        return []
    pairs = []
    for key, name in labels.items():
        cnt = kinds.get(key, 0)
        if cnt:
            pairs.append((name, cnt))
    for key, cnt in kinds.items():
        if key not in labels and cnt:
            pairs.append((str(key), cnt))
    body = _fmt_mono_rows(pairs)
    return ["🎞 <b>Типы</b>", body] if body else []


def _avg_score_from_dist(dist: dict) -> float | None:
    """Средняя оценка из распределения (игнорируя 0 = без оценки)."""
    total = count = 0
    for s, c in dist.items():
        sv = _safe_int(s)
        if sv > 0:
            total += sv * c
            count += c
    return round(total / count, 2) if count else None


def _title_link_from_rec(tid: str, rec: dict) -> str:
    """HTML-ссылка из записи titles{}."""
    title = h(rec.get("title") or "???")
    url = _rel_url(rec.get("url"))
    return f'<a href="{SHIKI_BASE_URL}{url}">{title}</a>' if url else title


def _pct_diff(curr: int, prev: int) -> str:
    """'↑ 25% (9 → 12)'."""
    if prev == 0:
        return f"+{curr}" if curr else "~"
    delta = curr - prev
    if delta == 0:
        return f"→ без изменений ({curr})"
    pct = round(abs(delta) / prev * 100)
    return f"{'↑' if delta > 0 else '↓'} {pct}% ({prev} → {curr})"


# ═══════════════════════════════════════════════════════════════════
#  /favs — ИЗБРАННОЕ (ЛЮБИМОЕ)
# ═══════════════════════════════════════════════════════════════════

def _fav_lines(items: list[dict]) -> list[str]:
    """Строки одного блока избранного: '• <ссылка> — ⭐9' (оценка опц.)."""
    lines = []
    for it in items:
        title = h(it.get("title") or "???")
        url = _rel_url(it.get("url"))
        name = f'<a href="{SHIKI_BASE_URL}{url}">{title}</a>' if url else title
        score = it.get("score")
        if isinstance(score, int) and score > 0:
            lines.append(f"  • {name} — ⭐{score}")
        else:
            lines.append(f"  • {name}")
    return lines


def build_favourites_messages(stats: dict) -> list[str]:
    """
    Сообщение со списком любимого: аниме, манга, персонажи, люди.
    Пустые категории не показываются. Если избранного нет совсем —
    короткое сообщение-заглушка.
    """
    fav = stats.get("favourites") or {}
    blocks = [
        ("🎬", "Аниме",          fav.get("anime") or []),
        ("📚", "Манга",          fav.get("manga") or []),
        ("📖", "Ранобэ",         fav.get("ranobe") or []),
        ("👤", "Персонажи",      fav.get("characters") or []),
        ("🎨", "Люди индустрии", fav.get("people") or []),
    ]

    if not any(items for _, _, items in blocks):
        return ["❤️ <b>ЛЮБИМОЕ</b>\n\n<i>Список любимого пока пуст.</i>"]

    out: list[str] = ["❤️ <b>ЛЮБИМОЕ</b>"]
    for emoji, title, items in blocks:
        if not items:
            continue
        out.append("")
        out.append(f"{emoji} <b>{title}</b> ({len(items)})")
        out.extend(_fav_lines(items))

    return ["\n".join(out)]


# ═══════════════════════════════════════════════════════════════════
#  /stats all — АГРЕГИРОВАННАЯ СТАТИСТИКА ЗА ВСЁ ВРЕМЯ
# ═══════════════════════════════════════════════════════════════════

def build_stats_all_messages(stats: dict) -> list[str]:
    """
    Список сообщений для /stats all, разбитый по темам: [аниме], [манга].
    Каждое самостоятельно проходит лимит Telegram.
    """
    a_agg = (stats.get("anime") or {}).get("aggregates") or {}
    m_agg = (stats.get("manga") or {}).get("aggregates") or {}

    updated = stats.get("updated_at") or ""
    upd_str = ""
    if updated:
        try:
            upd_str = datetime.fromisoformat(updated).strftime("%d.%m.%Y")
        except ValueError:
            pass

    # Пустая статистика — одно короткое сообщение
    if a_agg.get("total_completed", 0) == 0 and m_agg.get("total_completed", 0) == 0:
        return ["📊 <b>СТАТИСТИКА ЗА ВСЁ ВРЕМЯ</b>\n\n"
                "<i>Статистика ещё не собрана. Дай боту немного времени.</i>"]

    a_total = a_agg.get("total_completed", 0)
    m_total = m_agg.get("total_completed", 0)

    # ── Аниме ───────────────────────────────────
    a: list[str] = ["📊 <b>СТАТИСТИКА ЗА ВСЁ ВРЕМЯ</b>"]
    if upd_str:
        a.append(f"<i>актуально на {upd_str}</i>")
    a.append("")
    a.append(_section_header("🎬", "АНИМЕ"))
    a.append("")

    # Акцент сверху: сколько посмотрено · эпизоды/время, средняя оценка
    eps = a_agg.get("total_episodes_watched", 0)
    hrs = a_agg.get("total_hours_watched", 0)
    top_line = f"✅ Завершено: <b>{a_total}</b>"
    if eps:
        top_line += f"   ·   📺 {eps} эп (~{hrs} ч)"
    a.append(top_line)
    avg_a = _avg_score_from_dist(a_agg.get("score_dist", {}))
    if avg_a is not None:
        line = f"⭐ Средняя: <b>{avg_a}</b>"
        avg_shiki_a = a_agg.get("avg_shiki_completed")
        if isinstance(avg_shiki_a, (int, float)):
            diff = round(avg_a - avg_shiki_a, 1)
            sign = "+" if diff >= 0 else ""
            line += f"   <i>Shikimori: {round(avg_shiki_a, 1)} ({sign}{diff})</i>"
        a.append(line)

    # Детализация блоками
    for block in (
        _status_block_anime(a_agg),
        _kinds_block(a_agg.get("kinds", {}), _KIND_RU_ANIME),
        _score_dist_block(a_agg.get("score_dist", {})),
        _top_block("🎭", "Жанры",      a_agg.get("genres", {}),      8, show_percent=True, total=a_total),
        _top_block("🏷", "Темы",       a_agg.get("themes", {}),      8, show_percent=True, total=a_total),
        _top_block("👥", "Аудитория",  a_agg.get("demographic", {}), 99, show_percent=True, total=a_total),
        _top_block("🎨", "Студии",     a_agg.get("studios", {}),     6),
        _top_block("📚", "Источники",  a_agg.get("origins", {}),     99),
        _top_block("🔞", "Рейтинги",   a_agg.get("ratings", {}),     99),
    ):
        if block:
            a.append("")
            a.extend(block)

    # ── Манга ───────────────────────────────────
    m: list[str] = [_section_header("📚", "МАНГА"), ""]

    ch = m_agg.get("total_chapters_read", 0)
    vol = m_agg.get("total_volumes_read", 0)
    top_line = f"✅ Прочитано: <b>{m_total}</b>"
    if ch:
        top_line += f"   ·   📖 {ch} гл · {vol} томов"
    m.append(top_line)
    avg_m = _avg_score_from_dist(m_agg.get("score_dist", {}))
    if avg_m is not None:
        line = f"⭐ Средняя: <b>{avg_m}</b>"
        avg_shiki_m = m_agg.get("avg_shiki_completed")
        if isinstance(avg_shiki_m, (int, float)):
            diff = round(avg_m - avg_shiki_m, 1)
            sign = "+" if diff >= 0 else ""
            line += f"   <i>Shikimori: {round(avg_shiki_m, 1)} ({sign}{diff})</i>"
        m.append(line)

    for block in (
        _status_block_manga(m_agg),
        _kinds_block(m_agg.get("kinds", {}), _KIND_RU_MANGA),
        _score_dist_block(m_agg.get("score_dist", {})),
        _top_block("🎭", "Жанры",     m_agg.get("genres", {}),      8, show_percent=True, total=m_total),
        _top_block("🏷", "Темы",      m_agg.get("themes", {}),      8, show_percent=True, total=m_total),
        _top_block("👥", "Аудитория", m_agg.get("demographic", {}), 99, show_percent=True, total=m_total),
        _top_block("🏢", "Издатели",  m_agg.get("publishers", {}),  6),
    ):
        if block:
            m.append("")
            m.extend(block)

    return ["\n".join(a), "\n".join(m)]


# ═══════════════════════════════════════════════════════════════════
#  КВАРТАЛЬНЫЙ ОТЧЁТ И /stats (ТЕКУЩИЙ КВАРТАЛ)
# ═══════════════════════════════════════════════════════════════════

def _quarter_titles(cur: dict, stats_all: dict, media: str, event: str) -> list[dict]:
    """
    Возвращает записи titles{} для тайтлов, у которых в текущем квартале
    было событие event ("completed"|"dropped"), джойня события с stats_all.
    Для completed подставляем score из события (на момент завершения).
    """
    titles = (stats_all.get(media) or {}).get("titles") or {}
    out = []
    seen = set()
    for ev in cur.get("events", []):
        if ev.get("media") != media or ev.get("event") != event:
            continue
        tid = ev.get("id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        rec = titles.get(tid)
        if rec:
            merged = dict(rec)
            # score события приоритетнее (актуально на момент завершения квартала)
            if event == "completed" and ev.get("score") is not None:
                merged["score"] = ev["score"]
            out.append(merged)
        else:
            # Метаданных нет (тайтл не успел попасть в stats_all) — минимальная запись
            out.append({
                "title": "???", "url": "", "score": ev.get("score") or 0,
                "genres": [], "themes": [], "demographic": [],
            })
    return out


def _build_quarter_section(records: list[dict], media: str) -> list[str]:
    """Строки секции (аниме/манга) для отчётов на основе titles-записей квартала."""
    lines: list[str] = []
    if not records:
        return lines

    # Оценки
    scores = [r["score"] for r in records if _safe_int(r.get("score")) > 0]
    if scores:
        avg_personal = round(sum(scores) / len(scores), 1)
        # Средний рейтинг Shikimori по тем же тайтлам (у которых есть оценка)
        shiki = [r["shiki_score"] for r in records
                 if _safe_int(r.get("score")) > 0 and isinstance(r.get("shiki_score"), (int, float))]
        score_line = f"⭐ Средняя оценка: <b>{avg_personal}</b>"
        if shiki:
            avg_shiki = round(sum(shiki) / len(shiki), 1)
            diff = round(avg_personal - avg_shiki, 1)
            sign = "+" if diff >= 0 else ""
            score_line += f"  <i>(Shikimori: {avg_shiki}, {sign}{diff})</i>"
        lines.append(score_line)
        dist: dict = {}
        for s in scores:
            _bump(dist, s)
        block = _score_dist_block(dist)
        if block:
            lines.append("")
            lines.extend(block)

    # Топ по оценке
    top = sorted(
        [r for r in records if _safe_int(r.get("score")) > 0],
        key=lambda r: r["score"], reverse=True,
    )[:3]
    if top:
        lines.append("")
        lines.append("🏆 <b>Топ по оценке:</b>")
        for i, r in enumerate(top, 1):
            title = h(r.get("title") or "???")
            url = _rel_url(r.get("url"))
            link = f'<a href="{SHIKI_BASE_URL}{url}">{title}</a>' if url else title
            lines.append(f"  {i}. {link} — ⭐{r['score']}")

    # Хронология по году
    years = [(r["year"], r.get("title") or "???") for r in records
             if isinstance(r.get("year"), int) and r["year"] > 1900]
    if years:
        oldest = min(years, key=lambda x: x[0])
        newest = max(years, key=lambda x: x[0])
        avg_y = round(sum(y for y, _ in years) / len(years))
        lines.append("")
        if oldest[0] == newest[0]:
            lines.append(f"🗓️ Год выпуска: <b>{oldest[0]}</b>")
        else:
            lines.append(
                f"🗓️ Хронология: <b>{oldest[0]}</b> ({h(oldest[1])}) → "
                f"<b>{newest[0]}</b> ({h(newest[1])}),  ср. {avg_y}"
            )

    # Жанры/темы/аудитория из записей квартала
    genres: dict = {}
    themes: dict = {}
    demographic: dict = {}
    for r in records:
        for g in r.get("genres", []):
            _bump(genres, g)
        for t in r.get("themes", []):
            _bump(themes, t)
        for d in r.get("demographic", []):
            _bump(demographic, d)

    n_comp = len(records)  # база для процентов

    if media == "anime":
        studios: dict = {}
        origins: dict = {}
        total_eps = 0
        total_min = 0
        for r in records:
            for s in r.get("studios", []):
                _bump(studios, s)
            _bump(origins, r.get("origin"))
            eps = _safe_int(r.get("episodes_watched"))
            total_eps += eps
            dur = r.get("duration")
            if isinstance(dur, int) and dur > 0 and eps > 0:
                total_min += dur * eps
        if total_eps:
            lines.append(f"📺 Эпизодов: <b>{total_eps}</b>  (~{round(total_min / 60, 1)} ч.)")

        for block in (
            _top_block("🎭", "Жанры",     genres,      8, show_percent=True, total=n_comp),
            _top_block("🏷", "Темы",      themes,      8, show_percent=True, total=n_comp),
            _top_block("👥", "Аудитория", demographic, 99, show_percent=True, total=n_comp),
            _top_block("🎨", "Студии",    studios,     6),
            _top_block("📚", "Источники", origins,     99),
        ):
            if block:
                lines.append("")
                lines.extend(block)
    else:
        publishers: dict = {}
        total_ch = 0
        for r in records:
            for p in r.get("publishers", []):
                _bump(publishers, p)
            total_ch += _safe_int(r.get("chapters_read"))
        if total_ch:
            lines.append(f"📖 Глав прочитано: <b>{total_ch}</b>")

        for block in (
            _top_block("🎭", "Жанры",     genres,      8, show_percent=True, total=n_comp),
            _top_block("🏷", "Темы",      themes,      8, show_percent=True, total=n_comp),
            _top_block("👥", "Аудитория", demographic, 99, show_percent=True, total=n_comp),
            _top_block("🏢", "Издатели",  publishers,  6),
        ):
            if block:
                lines.append("")
                lines.extend(block)

    return lines


def _anime_block(cur: dict, comp: list[dict], drop: list[dict], plan: int, header: str) -> str:
    """Готовый текст блока АНИМЕ (одно сообщение)."""
    lines: list[str] = [header, ""]
    lines.append(f"✅ Завершено: <b>{len(comp)}</b>")
    if drop:
        lines.append(f"🗑 Брошено: {len(drop)}")
    if plan:
        lines.append(f"📋 В планируемое: {plan}")
    section = _build_quarter_section(comp, "anime")
    if section:
        lines.extend(section)
    elif not drop and not plan:
        lines.append("<i>Пока ничего не завершено.</i>")
    return "\n".join(lines)


def _manga_block(cur: dict, comp: list[dict], drop: list[dict], plan: int, header: str) -> str:
    """Готовый текст блока МАНГА (одно сообщение)."""
    lines: list[str] = [header, ""]
    lines.append(f"✅ Прочитано: <b>{len(comp)}</b>")
    if drop:
        lines.append(f"🗑 Брошено: {len(drop)}")
    if plan:
        lines.append(f"📋 В планируемое: {plan}")
    section = _build_quarter_section(comp, "manga")
    if section:
        lines.extend(section)
    elif not drop and not plan:
        lines.append("<i>Пока ничего не завершено.</i>")
    return "\n".join(lines)


def build_current_stats_messages(cur: dict, stats_all: dict) -> list[str]:
    """
    Список сообщений для /stats (текущий квартал), разбитый по темам:
      [0] аниме, [1] манга.
    Каждое сообщение самостоятельное и проходит лимит Telegram отдельно.
    """
    title_label = tracking_period_label(cur)

    comp_a = _quarter_titles(cur, stats_all, "anime", "completed")
    drop_a = _quarter_titles(cur, stats_all, "anime", "dropped")
    comp_m = _quarter_titles(cur, stats_all, "manga", "completed")
    drop_m = _quarter_titles(cur, stats_all, "manga", "dropped")

    plan_a = sum(1 for e in cur.get("events", []) if e["media"] == "anime" and e["event"] == "planned")
    plan_m = sum(1 for e in cur.get("events", []) if e["media"] == "manga" and e["event"] == "planned")

    header = f"📊 <b>Статистика {h(title_label)}</b>"
    if _is_partial_quarter(cur):
        header += "\n<i>⚠️ Квартал отслеживается не с самого начала — данные неполные.</i>"

    msgs: list[str] = []
    msgs.append(header + "\n\n" + _anime_block(cur, comp_a, drop_a, plan_a, _section_header("🎬", "АНИМЕ")))
    msgs.append(_manga_block(cur, comp_m, drop_m, plan_m, _section_header("📚", "МАНГА")))
    return msgs


def build_quarterly_report_messages(cur: dict, stats_all: dict, prev_quarter: dict | None) -> list[str]:
    """
    Список сообщений квартального отчёта для владельца, по темам:
      [0] заголовок + аниме, [1] манга, [2] сравнение + достижения.
    """
    title_label = tracking_period_label(cur)

    comp_a = _quarter_titles(cur, stats_all, "anime", "completed")
    drop_a = _quarter_titles(cur, stats_all, "anime", "dropped")
    comp_m = _quarter_titles(cur, stats_all, "manga", "completed")
    drop_m = _quarter_titles(cur, stats_all, "manga", "dropped")

    plan_a = sum(1 for e in cur.get("events", []) if e["media"] == "anime" and e["event"] == "planned")
    plan_m = sum(1 for e in cur.get("events", []) if e["media"] == "manga" and e["event"] == "planned")

    header = f"📊 <b>КВАРТАЛЬНЫЙ ОТЧЁТ</b>\n<b>{h(title_label)}</b>"
    if _is_partial_quarter(cur):
        header += "\n<i>⚠️ Квартал отслеживался не с самого начала — данные неполные.</i>"

    msgs: list[str] = []

    # Сообщение 1: заголовок + аниме
    msgs.append(header + "\n\n" + _anime_block(cur, comp_a, drop_a, plan_a, _section_header("🎬", "АНИМЕ")))

    # Сообщение 2: манга
    msgs.append(_manga_block(cur, comp_m, drop_m, plan_m, _section_header("📚", "МАНГА")))

    # Сообщение 3: сравнение + достижения
    extra: list[str] = []
    if prev_quarter:
        prev_a = prev_quarter.get("anime_completed", 0)
        prev_m = prev_quarter.get("manga_completed", 0)
        prev_label = quarter_label(prev_quarter.get("period") or "прошлый квартал")
        extra.append(f"📈 <b>Сравнение с {h(prev_label)}:</b>")
        extra.append(f"🎬 Аниме: {_pct_diff(len(comp_a), prev_a)}")
        extra.append(f"📚 Манга: {_pct_diff(len(comp_m), prev_m)}")

    all_comp = comp_a + comp_m
    ach: list[str] = []
    tens = [r for r in all_comp if r.get("score") == 10]
    if len(tens) >= 3:
        ach.append(f"💎 Десятку поставил {len(tens)} раза — строгий критик!")
    elif len(tens) == 1:
        ach.append("💎 Один безоговорочный шедевр за квартал.")
    total_drops = len(drop_a) + len(drop_m)
    if total_drops == 0 and all_comp:
        ach.append("🎯 Ни одного дропа — железная воля или идеальный вкус!")
    elif total_drops >= 5:
        ach.append(f"🗑️ {total_drops} дропов — знает, чего не хочет.")
    low = [r for r in all_comp if 0 < _safe_int(r.get("score")) <= 3]
    if low:
        n = len(low)
        ach.append(f"🧟 Домучил {n} тайтл{'а' if n < 5 else 'ов'} с оценкой ≤3 — стойкость.")

    if ach:
        if extra:
            extra.append("")
        extra.append("🏆 <b>Достижения:</b>")
        extra.extend(f"• {a}" for a in ach)

    if extra:
        msgs.append("\n".join(extra))

    return msgs


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


def _load_prev_quarter_summary(period: str) -> dict | None:
    """Краткая сводка предыдущего квартала из снапшота для сравнения."""
    try:
        path = QUARTERS_DIR / f"{period}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                "period": data.get("period"),
                "anime_completed": data.get("anime_completed", 0),
                "manga_completed": data.get("manga_completed", 0),
            }
    except Exception as e:
        log.warning("_load_prev_quarter_summary(%s): %s", period, e)
    return None


def _save_quarter_snapshot(period: str, cur: dict, stats_all: dict) -> None:
    """Сохраняем замороженный снапшот квартала в quarters/<period>.json."""
    try:
        QUARTERS_DIR.mkdir(parents=True, exist_ok=True)
        comp_a = _quarter_titles(cur, stats_all, "anime", "completed")
        comp_m = _quarter_titles(cur, stats_all, "manga", "completed")
        snapshot = {
            "period": period,
            "anime_completed": len(comp_a),
            "manga_completed": len(comp_m),
            "events": cur.get("events", []),
            "anime_titles": comp_a,
            "manga_titles": comp_m,
        }
        _atomic_write(QUARTERS_DIR / f"{period}.json",
                      json.dumps(snapshot, ensure_ascii=False, indent=2))
        log.info("Снапшот квартала %s сохранён.", period)
    except Exception as e:
        log.error("_save_quarter_snapshot(%s): %s", period, e)


def _update_by_quarter(stats_all: dict, period: str, cur: dict) -> None:
    """Добавляем сводку квартала в aggregates.by_quarter для аниме и манги."""
    for media in ("anime", "manga"):
        comp = _quarter_titles(cur, stats_all, media, "completed")
        scores = [r["score"] for r in comp if _safe_int(r.get("score")) > 0]
        avg = round(sum(scores) / len(scores), 2) if scores else None
        bq = stats_all[media].setdefault("aggregates", {}).setdefault("by_quarter", {})
        entry = {"completed": len(comp), "avg_score": avg}
        if media == "anime":
            entry["episodes_watched"] = sum(_safe_int(r.get("episodes_watched")) for r in comp)
        else:
            entry["chapters_read"] = sum(_safe_int(r.get("chapters_read")) for r in comp)
        bq[period] = entry


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


# ═══════════════════════════════════════════════════════════════════
#  КОМАНДА /stats  [all]
# ═══════════════════════════════════════════════════════════════════

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

async def fetch_history(session: aiohttp.ClientSession) -> list[dict] | None:
    """Запрашиваем историю с API Shikimori.
    Возвращает список записей при успехе или None при любой ошибке.
    """
    try:
        async with session.get(
            HISTORY_URL,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                log.warning("fetch_history: API вернул статус %d", resp.status)
                return None
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.error("fetch_history: ошибка запроса: %s", e)
        return None
    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
        log.error("fetch_history: не удалось разобрать ответ: %s", e)
        return None


async def fetch_favourites(session: aiohttp.ClientSession) -> dict | None:
    """
    Запрашиваем избранное с API Shikimori.
    Возвращает словарь вида:
      {"animes": [...], "mangas": [...], "characters": [...], "people": [...], ...}
    Каждый элемент содержит хотя бы "id", "name", "russian", "url".
    Возвращает None при любой ошибке.
    """
    try:
        async with session.get(
            FAVOURITES_URL,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                log.warning("fetch_favourites: API вернул статус %d", resp.status)
                return None
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.error("fetch_favourites: ошибка запроса: %s", e)
        return None
    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
        log.error("fetch_favourites: не удалось разобрать ответ: %s", e)
        return None


def build_favourite_message(category: str, item: dict) -> str:
    """
    Формируем сообщение об добавлении в избранное.
    category: одна из _FAV_CATEGORIES (animes/mangas/ranobe/characters/
              people/mangakas/seyu/producers)
    item:     объект из API с полями id, name, russian, url и др.
              url может быть подставлен из titles{} вызывающей стороной
              (Favourites API сам отдаёт url=null).
    """
    # Категория API → ключ банка сообщений.
    # ranobe переиспользует банк манги; вся индустрия — банк person.
    cat_map = {
        "animes":     "anime",
        "mangas":     "manga",
        "ranobe":     "manga",
        "characters": "character",
        "people":     "person",
        "mangakas":   "person",
        "seyu":       "person",
        "producers":  "person",
    }
    bank_key = cat_map.get(category, "anime")
    templates = MESSAGES["favourites"].get(bank_key, MESSAGES["favourites"]["anime"])

    title_ru = item.get("russian") or ""
    title_en = item.get("name") or "???"
    title_text = h(title_ru if title_ru else title_en)

    # Ссылку зашиваем в название — единообразно с /favs и событиями
    url = _rel_url(item.get("url"))
    title = (f'<a href="{SHIKI_BASE_URL}{url}">{title_text}</a>'
             if url else title_text)

    text = random.choice(templates).format(n=DISPLAY_NAME, title=title)

    return text


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
            err = str(e).lower()
            if "bot was blocked" in err or "user is deactivated" in err or "chat not found" in err:
                log.warning("  ✗ %s (chat_id=%d) заблокировал бота — отписываем.", name, chat_id)
                to_remove.append(chat_id)
            else:
                log.error("  ✗ Не удалось отправить %s (chat_id=%d): %s", name, chat_id, e)
        # Небольшая пауза между отправками — не триггерим flood control
        await asyncio.sleep(0.3)

    # Удаляем заблокировавших — сохраняем актуальный список
    if to_remove:
        for cid in to_remove:
            subs.pop(cid, None)
        save_subscribers(subs)
        log.info("Отписано %d пользователей, заблокировавших бота.", len(to_remove))


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

    if not seen_ids:
        log.info("Первый запуск — инициализируем историю без отправки сообщений.")
        async with aiohttp.ClientSession() as session:
            entries = await fetch_history(session)
        if entries is None:
            log.warning("Не удалось получить историю при инициализации — пропускаем, повторим на следующем цикле.")
        else:
            seen_ids = {e["id"] for e in entries}
            save_seen_ids(seen_ids)
            log.info("Инициализировано %d ID истории.", len(seen_ids))

    if not seen_favs:
        log.info("Инициализируем избранное без отправки сообщений.")
        async with aiohttp.ClientSession() as session:
            favourites = await fetch_favourites(session)
        if favourites is None:
            log.warning("Не удалось получить избранное при инициализации — пропускаем, повторим на следующем цикле.")
        else:
            for category in _FAV_CATEGORIES:
                for item in (favourites.get(category) or []):
                    if item.get("id") is not None:
                        seen_favs.add(f"{category}_{item['id']}")
            save_seen_favourites(seen_favs)
            log.info("Инициализировано %d записей избранного.", len(seen_favs))

    # Актуализируем полную статистику из list_export (не зависит от seen_ids,
    # строится сразу даже на первом запуске — данные берутся не из history).
    log.info("Синхронизируем статистику за всё время (stats_all)...")
    try:
        stats_all, synced_ok = await sync_stats_all()
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

BACKUP_TAG = "#backup"

# Бэкап при остановке (SIGTERM-триггер): дополняет событийные бэкапы, ловит
# «последнюю милю» перед смертью контейнера на редеплое. Дебаунс — не слать,
# если только что уже бэкапили; короткий таймаут — лучше не успеть, чем зависнуть.
SHUTDOWN_BACKUP_DEBOUNCE = 60   # с: не дублировать shutdown-бэкап после свежего
SHUTDOWN_BACKUP_TIMEOUT  = 8    # с: жёсткий потолок отправки в окне graceful-shutdown
_last_backup_sent_at: float | None = None   # monotonic-метка последнего успешного бэкапа

# Файлы DATA_DIR, которые восстанавливаем при импорте (см. асимметрию выше).
_IMPORT_ALLOWED_FILES: frozenset[str] = frozenset({
    "subscribers.json", "stats_current.json",
})
# Каталог снапшотов кварталов: разрешаем quarters/<period>.json.
_IMPORT_ALLOWED_DIR = "quarters"


class BackupStates(StatesGroup):
    waiting_import_file = State()   # ждём .zip-архив от владельца


def _subscriber_link(chat_id: int, name: str) -> str:
    """Имя, обёрнутое в ссылку на профиль (tg://user?id=...).
    Telegram открывает карточку пользователя по такой ссылке — владельцу
    удобно сразу перейти к тому, кто подписался/отписался."""
    return f'<a href="tg://user?id={chat_id}">{h(name)}</a>'


def _backup_filename() -> str:
    """Имя архива с меткой времени UTC — чтобы файлы не перезатирались в чате."""
    return f"shikibot-backup-{_utcnow().strftime('%Y%m%d-%H%M%S')}.zip"


def _build_backup_zip() -> bytes:
    """Зипуем весь DATA_DIR в память. Исключаем *.tmp (недописанные хвосты
    _atomic_write). arcname — путь относительно DATA_DIR, чтобы структура
    (включая quarters/) восстановилась один-в-один. Возвращаем bytes —
    готовый архив для BufferedInputFile, без временных файлов на диске."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DATA_DIR.rglob("*")):
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            zf.write(path, path.relative_to(DATA_DIR).as_posix())
    return buf.getvalue()


async def send_backup(bot: Bot, caption: str) -> bool:
    """Собрать архив DATA_DIR и отправить владельцу. caption уже содержит
    #backup. Любой сбой глушим и логируем: бэкап — фоновая страховка, он не
    должен ронять вызывающий флоу (подписку, ротацию, цикл)."""
    global _last_backup_sent_at
    try:
        data = _build_backup_zip()
    except Exception as e:
        log.error("send_backup: не удалось собрать архив: %s", e)
        return False
    try:
        await bot.send_document(
            OWNER_ID,
            document=BufferedInputFile(data, filename=_backup_filename()),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        log.info("send_backup: архив отправлен владельцу (%d байт).", len(data))
        _last_backup_sent_at = time.monotonic()
        return True
    except Exception as e:
        log.error("send_backup: не удалось отправить владельцу: %s", e)
        return False


async def _shutdown_backup(bot: Bot) -> None:
    """Финальный бэкап при остановке. aiogram сам ловит SIGTERM/SIGINT и эмитит
    событие shutdown, к которому мы цепляемся (dp.shutdown.register). SIGTERM от
    хостинга = плановый редеплой/рестарт. Это ДОПОЛНЕНИЕ к событийным авто-бэкапам,
    а не замена: ловит «последнюю милю» перед смертью контейнера. Дебаунс — если
    бэкап уходил только что, второй не шлём. Короткий таймаут — лучше не успеть,
    чем зависнуть и быть убитым жёстко на полпути. SIGKILL/OOM/слишком короткий
    grace этим не покрыть by design — на то и событийные бэкапы (две сети внахлёст).
    Бонус: само сообщение — сигнал владельцу «бот гасится», на проде нетипично."""
    if (_last_backup_sent_at is not None
            and time.monotonic() - _last_backup_sent_at < SHUTDOWN_BACKUP_DEBOUNCE):
        log.info("_shutdown_backup: недавний бэкап свежий, на shutdown не дублирую.")
        return
    caption = (f"🔻 Бот завершает работу (SIGTERM). Финальный снапшот состояния.\n\n"
               f"{BACKUP_TAG}")
    try:
        await asyncio.wait_for(send_backup(bot, caption), timeout=SHUTDOWN_BACKUP_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("_shutdown_backup: отправка не уложилась в %d с — выходим без бэкапа.",
                    SHUTDOWN_BACKUP_TIMEOUT)


def _is_allowed_import_member(name: str) -> bool:
    """Разрешено ли имя из архива к восстановлению?
    Бел.список: subscribers.json, stats_current.json, quarters/<имя>.json.
    Глушим zip-slip: '..'-сегменты, абсолютные пути и бэкслеши отвергаем."""
    if not name or name.endswith("/"):
        return False
    if name.startswith("/") or "\\" in name or ".." in name.split("/"):
        return False
    if name in _IMPORT_ALLOWED_FILES:
        return True
    parts = name.split("/")
    return (
        len(parts) == 2
        and parts[0] == _IMPORT_ALLOWED_DIR
        and parts[1].endswith(".json")
    )


def _valid_import_payload(name: str, obj) -> bool:
    """Грубая проверка структуры восстанавливаемого файла: чтобы синтаксически
    валидный, но мусорный по смыслу JSON не затёр рабочее состояние. Проверяем
    ровно ту форму, которую ждут загрузчики (load_subscribers/load_stats_current
    и чтение снапшотов), не строже — иначе отвергли бы легитимные старые файлы."""
    if name == "subscribers.json":
        subs = obj.get("subscribers") if isinstance(obj, dict) else None
        if not isinstance(subs, dict):
            return False
        try:                       # ключи — chat_id, должны приводиться к int
            for k in subs:
                int(k)
        except (TypeError, ValueError):
            return False
        return True
    if name == "stats_current.json":
        return (isinstance(obj, dict) and "period" in obj
                and isinstance(obj.get("events"), list))
    if name.startswith(_IMPORT_ALLOWED_DIR + "/"):
        return isinstance(obj, dict) and "period" in obj
    return False


def restore_backup_zip(raw: bytes) -> dict:
    """Восстанавливаем состояние из архива по белому списку.
    Каждый разрешённый член сперва валидируем как JSON и только потом пишем
    атомарно (_atomic_write) в DATA_DIR — частичное/битое состояние не льём.
    Возвращаем {'restored': [...], 'skipped': [...]}.
    Бросаем ValueError, если архив битый или в нём нет ни одного валидного
    файла из белого списка."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise ValueError(f"битый zip-архив: {e}") from e

    restored: list[str] = []
    skipped: list[str] = []
    pending: dict[str, str] = {}
    with zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            if not _is_allowed_import_member(name):
                skipped.append(name)
                continue
            try:
                payload = zf.read(name).decode("utf-8")
                obj = json.loads(payload)   # синтаксически валидный JSON?
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                log.warning("restore_backup_zip: пропускаю битый %s: %s", name, e)
                skipped.append(name)
                continue
            if not _valid_import_payload(name, obj):   # и похож на ожидаемую структуру?
                log.warning("restore_backup_zip: %s не похож на ожидаемый формат — пропускаю.", name)
                skipped.append(name)
                continue
            pending[name] = payload

    if not pending:
        raise ValueError("в архиве нет валидных файлов из белого списка")

    for name, payload in pending.items():
        _atomic_write(DATA_DIR / name, payload)
        restored.append(name)
    log.info("restore_backup_zip: восстановлено %d, пропущено %d.",
             len(restored), len(skipped))
    return {"restored": restored, "skipped": skipped}


async def _backup_after_subscription(
    bot: Bot, chat_id: int, name: str, subscribed: bool,
) -> None:
    """Авто-бэкап на (от)подписку: владельцу уходит свежий архив состояния,
    в подписи — кто и что сделал (имя кликабельно, ведёт в профиль) и сколько
    подписчиков осталось. «Два в одном»: индикация события + страховка списка."""
    subs = load_subscribers()
    head = (f"➕ Новый подписчик: {_subscriber_link(chat_id, name)}"
            if subscribed else
            f"➖ Отписался: {_subscriber_link(chat_id, name)}")
    caption = f"{head}\nВсего подписчиков: <b>{len(subs)}</b>\n\n{BACKUP_TAG}"
    await send_backup(bot, caption)


async def _weekly_backup_if_due(bot: Bot, cur: dict) -> dict:
    """Еженедельный авто-бэкап состояния по метке last_backup_at в stats_current.
    Первый раз (метки нет) — только проставляем время, не шлём: иначе на каждом
    рестарте эфемерного хоста улетал бы бэкап. Первый плановый уйдёт через
    WEEKLY_BACKUP_INTERVAL аптайма; под/отписки и ротация бэкапят независимо."""
    now = time.time()
    last = cur.get("last_backup_at")
    if last is None:
        cur["last_backup_at"] = now
        save_stats_current(cur)
        return cur
    if (now - last) < WEEKLY_BACKUP_INTERVAL:
        return cur
    caption = (f"🗓️ Еженедельный бэкап состояния.\n"
               f"Подписчиков: <b>{len(load_subscribers())}</b>\n\n{BACKUP_TAG}")
    if await send_backup(bot, caption):
        cur["last_backup_at"] = now
        save_stats_current(cur)
    return cur


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
    """Подписаться на уведомления."""
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
            err = str(e).lower()
            if "bot was blocked" in err or "user is deactivated" in err or "chat not found" in err:
                log.warning("  broadcast ✗ %s (chat_id=%d) заблокировал бота.", name, cid)
                to_remove.append(cid)
            else:
                log.error("  broadcast ✗ %s (chat_id=%d): %s", name, cid, e)
            failed += 1
        await asyncio.sleep(0.3)

    if to_remove:
        for cid in to_remove:
            subs.pop(cid, None)
        save_subscribers(subs)
        log.info("Отписано %d заблокировавших бота.", len(to_remove))

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


async def fetch_current_rates(media: str, statuses: list[str]) -> list[dict] | None:
    """
    Запрашивает тайтлы в указанных статусах.
    media:    "anime" или "manga"
    statuses: ["watching", "rewatching"] — одинаково для аниме и манги
    Возвращает объединённый список записей при успехе или None при любой ошибке.
    """
    results = []
    async with aiohttp.ClientSession() as session:
        for status in statuses:
            url = f"{SHIKI_BASE_URL}/api/users/{SHIKI_USER}/{media}_rates?status={status}&limit=50"
            try:
                async with session.get(
                    url,
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Добавляем поле status в каждую запись, чтобы знать откуда она
                        for item in data:
                            item["_status"] = status
                        results.extend(data)
                    else:
                        log.warning("fetch_current_rates: статус %d для %s/%s", resp.status, media, status)
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.error("fetch_current_rates ошибка (%s/%s): %s", media, status, e)
                return None
            except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
                log.error("fetch_current_rates: не удалось разобрать ответ (%s/%s): %s", media, status, e)
                return None
    return results


def format_rate_entry(item: dict, media: str) -> str:
    """Форматирует одну запись из rates API в строку для сообщения."""
    # Название тайтла — в rates API вложено в item["anime"] или item["manga"]
    target = item.get(media) or {}
    title_ru = target.get("russian") or ""
    title_en = target.get("name") or "???"
    title = h(title_ru if title_ru else title_en)

    status = item.get("_status", "")
    # Иконка в зависимости от статуса
    icon = {
        "watching":   "▶️",
        "rewatching": "🔁",
    }.get(status, "•")

    url = target.get("url", "")
    if url:
        return f'{icon} <a href="{SHIKI_BASE_URL}{url}">{title}</a>'
    return f"{icon} {title}"


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


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())

    # Регистрируем команды
    dp.message.register(cmd_start,     Command("start"))
    dp.message.register(cmd_stop,      Command("stop"))
    dp.message.register(cmd_subs,      Command("subs"))
    dp.message.register(cmd_backup,    Command("backup"))
    dp.message.register(cmd_status,    Command("status"))
    dp.message.register(cmd_broadcast, Command("broadcast"))
    dp.message.register(cmd_cancel,    Command("cancel"))
    dp.message.register(cmd_stats,     Command("stats"))
    dp.message.register(cmd_favs,      Command("favs"))

    # FSM-обработчики для /broadcast
    dp.message.register(broadcast_receive, BroadcastStates.waiting_content)
    dp.callback_query.register(broadcast_confirm_cb, F.data == "broadcast_send",   BroadcastStates.waiting_confirm)
    dp.callback_query.register(broadcast_cancel_cb,  F.data == "broadcast_cancel", BroadcastStates.waiting_confirm)

    # FSM-обработчик и кнопки для /backup
    dp.message.register(backup_receive, BackupStates.waiting_import_file)
    dp.callback_query.register(backup_export_cb, F.data == "backup:export")
    dp.callback_query.register(backup_import_cb, F.data == "backup:import")
    dp.callback_query.register(backup_close_cb,  F.data == "backup:close")

    # Кнопки меню /stats (callback_data вида "stats:<ключ>")
    dp.callback_query.register(stats_menu_cb, F.data.startswith("stats:"))

    # Публичные команды в меню "/" — команды владельца не показываем
    await bot.set_my_commands([
        BotCommand(command="start",  description="Подписаться на уведомления 🥳"),
        BotCommand(command="status", description=f"Что сейчас смотрит и читает {DISPLAY_NAME} 👀"),
        BotCommand(command="stats",  description="Статистика: квартал или всё время 📊"),
        BotCommand(command="favs",   description="Избранное ❤️"),
        BotCommand(command="stop",   description="Отписаться 😢"),
    ])

    # polling_loop работает параллельно как фоновая задача
    _polling_task = asyncio.create_task(polling_loop(bot))

    def _on_polling_done(task: asyncio.Task) -> None:
        """Логируем если polling_loop завершился неожиданно."""
        if task.cancelled():
            log.warning("polling_loop: задача отменена.")
        elif exc := task.exception():
            log.critical(
                "polling_loop завершился с необработанной ошибкой: %s",
                exc,
                exc_info=exc,
            )

    _polling_task.add_done_callback(_on_polling_done)

    # Healthcheck-сервер (для хостингов с обязательным портом + watchdog)
    await start_health_server(check_interval=CHECK_INTERVAL)

    # Финальный бэкап при остановке (aiogram ловит SIGTERM/SIGINT → emit_shutdown)
    dp.shutdown.register(_shutdown_backup)

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
