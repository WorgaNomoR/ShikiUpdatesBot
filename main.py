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
import html
import json
import os
import logging
import re
import random
import time
from pathlib import Path
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, BotCommand, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)
from healthcheck import heartbeat, start_health_server

# ─────────────────────────────────────────────
#  НАСТРОЙКИ — заполни перед запуском
# ─────────────────────────────────────────────
# Токен читается из переменной окружения BOT_TOKEN — не храни его в коде!
# Задать: export BOT_TOKEN="токен_от_BotFather"
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Твой Telegram ID — узнать у @userinfobot.
# Нужен для команд только для владельца (/subs, /export, /import, /broadcast).
# Задать: export OWNER_ID="123456789"
OWNER_ID = int(os.environ["OWNER_ID"])

SHIKI_USER     = "WNR"                   # ник на Shikimori (для API)
SHIKI_BASE_URL = "https://shikimori.io"  # домен — меняй здесь при смене зеркала
DISPLAY_NAME   = "Ворга"                 # отображаемое имя в сообщениях
CHECK_INTERVAL = 15 * 60                 # интервал проверки в секундах (15 минут)
ERROR_NOTIFY_INTERVAL = 30 * 60          # не чаще одного уведомления об ошибке в 30 минут

# ─────────────────────────────────────────────
#  ПУТИ К ФАЙЛАМ ДАННЫХ
#  По умолчанию всё создаётся в /data.
#  Чтобы хранить в другом месте — задай переменную окружения
#  DATA_DIR=/путь/к/папке.
# ─────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

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


async def _send_broadcast_message(bot: Bot, chat_id: int, data: dict) -> None:
    """
    Отправляет одно сообщение рассылки в указанный чат.
    data — словарь, сохранённый в FSM:
      msg_type  : "text" | "photo" | "video" | "animation" | "document" | "voice" | "sticker"
      file_id   : str | None
      user_text : str  (текст сообщения или caption от пользователя)
    """
    msg_type  = data["msg_type"]
    user_text = data.get("user_text", "")
    file_id   = data.get("file_id")

    if msg_type == "text":
        # Текст оборачиваем в цитату
        body = f"\n<blockquote>{h(user_text)}</blockquote>" if user_text else ""
        await bot.send_message(
            chat_id=chat_id,
            text=f"{BROADCAST_HEADER}{body}",
            parse_mode=ParseMode.HTML,
        )

    elif msg_type == "sticker":
        # Стикеры не поддерживают caption — шапка отдельным сообщением
        await bot.send_message(chat_id=chat_id, text=BROADCAST_HEADER, parse_mode=ParseMode.HTML)
        await bot.send_sticker(chat_id=chat_id, sticker=file_id)

    else:
        # Фото, видео, GIF, документ, голосовое — шапка + текст пользователя в caption
        if user_text:
            caption = f"{BROADCAST_HEADER}\n\n{h(user_text)}"
        else:
            caption = BROADCAST_HEADER

        common = dict(chat_id=chat_id, caption=caption, parse_mode=ParseMode.HTML)

        if msg_type == "photo":
            await bot.send_photo(
                photo=file_id, show_caption_above_media=True, **common,
            )
        elif msg_type == "video":
            await bot.send_video(
                video=file_id, show_caption_above_media=True, **common,
            )
        elif msg_type == "animation":
            await bot.send_animation(
                animation=file_id, show_caption_above_media=True, **common,
            )
        elif msg_type == "document":
            await bot.send_document(document=file_id, **common)
        elif msg_type == "voice":
            await bot.send_voice(voice=file_id, **common)


def h(text: str) -> str:
    """Экранируем спецсимволы HTML — защита от поломки разметки в Telegram.
    Экранирует: & → &amp;  < → &lt;  > → &gt;
    Применять ко всем пользовательским данным из API перед вставкой в сообщение.
    """
    return html.escape(str(text))


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
            "🗂️ <b>{title}</b> занял своё место в очереди на годы. Дождётся ли?",
            "📌 {n} запланировал <b>{title}</b>. Статистика говорит: 80% таких тайтлов умирают непросмотренными.",
            "🧠 Судьба <b>{title}</b> решена — оно теперь в списке. Просмотр — под вопросом.",
            "🔖 <b>{title}</b> добавлено в коллекцию намерений {n}. Осталось только посмотреть.",
            "📥 Хоп — и <b>{title}</b> в planned. Как будто кто-то собирается это смотреть 👀",
        ],

        # ▶️ Начал смотреть
        "watching": [
            "▶️ {n} начал смотреть <b>{title}</b>. Запасаемся попкорном.",
            "🎬 Поехали! <b>{title}</b> запущено. Возврата нет.",
            "👁️ {n} открыл <b>{title}</b> и пропал. Ждём отчёта.",
            "🍿 <b>{title}</b> в плеере, {n} у экрана. Классика.",
            "🚀 Старт! <b>{title}</b> вышло на орбиту просмотра.",
            "😤 {n} не выдержал и таки начал <b>{title}</b>. Посмотрим, чем это закончится.",
        ],

        # 🔁 Пересматривает
        "rewatching": [
            "🔁 {n} пересматривает <b>{title}</b>. Не надоело — значит шедевр (или мазохизм).",
            "♻️ <b>{title}</b> снова в деле. {n} возвращается к проверенному.",
            "🌀 Повторный заход на <b>{title}</b>. Уважаю.",
            "📺 {n} включил <b>{title}</b> ещё раз. Некоторые вещи просто не отпускают.",
            "🔂 <b>{title}</b> на втором (третьем? десятом?) круге у {n}. Это уже традиция.",
            "👏 Решился на ремастер впечатлений — <b>{title}</b> снова смотрит {n}.",
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
        ],

        # ⭐ Оценка 1–3
        "completed_score_low": [
            "💩 <b>{title}</b> — {score}/10. {n} страдал, но добил. Настоящий герой.",
            "😭 {score}/10 за <b>{title}</b>. Боль реальна. Зачем вообще?",
            "🤮 <b>{title}</b> получает {score}/10 от {n}. Это приговор.",
            "⚰️ {score}/10 — <b>{title}</b> мертво и похоронено в памяти {n}.",
            "🧟 {n} выжил после <b>{title}</b> ({score}/10). Это уже достижение.",
            "🔥 <b>{title}</b> — {score}/10. Сожжено дотла заслуженно.",
        ],

        # 😐 Оценка 4–6
        "completed_score_mid": [
            "😐 <b>{title}</b> — {score}/10. Ни рыба ни мясо, говорит {n}.",
            "🫤 {score}/10 за <b>{title}</b>. Не плохо, не хорошо. Просто... было.",
            "🤷 {n} поставил <b>{title}</b> {score}/10. Среднячок прожил и умер.",
            "📊 <b>{title}</b> — твёрдый {score}/10. {n} явно ожидал большего.",
            "🌫️ {score}/10 — <b>{title}</b> оставило {n} в тумане безразличия.",
            "😶 Посмотрел. Оценил. {score}/10. <b>{title}</b> не потрясло мир {n}.",
        ],

        # 🌟 Оценка 7–9
        "completed_score_high": [
            "🌟 <b>{title}</b> — {score}/10! {n} доволен. Хороший вкус подтверждён.",
            "🔥 {score}/10 за <b>{title}</b>! {n} в восторге, и это заслужено.",
            "👏 <b>{title}</b> получает {score}/10 от {n}. Браво, студия!",
            "✨ {score}/10 — <b>{title}</b> попало в сердечко {n}.",
            "🎉 Вот это да! {score}/10 за <b>{title}</b>. Рекомендую к просмотру всем.",
            "💫 <b>{title}</b> — {score}/10. {n} явно не разочарован. Редкий случай.",
        ],

        # 👑 Оценка 10
        "completed_score_perfect": [
            "👑 <b>{title}</b> — ДЕСЯТКА! {n} нашёл новый фаворит. Занесите в анналы.",
            "🏆 10/10! <b>{title}</b> вошло в пантеон {n}. Это серьёзно.",
            "💎 {n} раздаёт десятки! <b>{title}</b> — абсолютный шедевр по его версии.",
            "🌌 10/10 за <b>{title}</b>. {n} разрушен и счастлив одновременно.",
            "🎌 Максимум! <b>{title}</b> — теперь часть души {n}. Трогательно.",
            "🔮 <b>{title}</b> получает священную десятку. {n} преклоняется.",
        ],
    },  # конец "anime"

    # ────────────────────────────────
    #  МАНГА (свои тексты — читает, а не смотрит)
    # ────────────────────────────────

    "manga": {

        # 📋 Добавил в «Запланированное»
        "planned": [
            "📚 {n} добавил мангу <b>{title}</b> в список «прочитаю как-нибудь». Не факт.",
            "🗂️ <b>{title}</b> записана в очередь. Полки ломятся, {n} не останавливается.",
            "📌 {n} запланировал <b>{title}</b>. Главы сами себя не прочитают.",
            "🧠 Манга <b>{title}</b> теперь в списке {n}. До прочтения — бесконечность.",
            "🔖 <b>{title}</b> зафиксирована. {n} снова расширяет свои непрочитанные владения.",
            "📥 Хоп — <b>{title}</b> в planned. Сколько глав? Неважно. Прочитаю. Когда-нибудь.",
        ],

        # ▶️ Начал читать
        "watching": [
            "📖 {n} открыл мангу <b>{title}</b>. Поехали, глава за главой.",
            "🎌 {n} приступил к чтению <b>{title}</b>. Спать, видимо, не скоро.",
            "👁️ <b>{title}</b> в руках {n}. Ждём отчёта с полей.",
            "📜 {n} начал читать <b>{title}</b>. Надеемся, глав там хватит.",
            "🚀 Старт! <b>{title}</b> — новая манга в арсенале {n}.",
            "😤 {n} не устоял и взялся за <b>{title}</b>. Конца и края не видно, но кого это останавливало.",
        ],

        # 🔁 Перечитывает
        "rewatching": [
            "🔁 {n} перечитывает <b>{title}</b>. Значит, оно того стоило.",
            "♻️ <b>{title}</b> снова открыта. {n} возвращается за второй дозой.",
            "🌀 Повторный заход на мангу <b>{title}</b>. Хороший знак.",
            "📚 {n} листает <b>{title}</b> по второму кругу. Некоторые детали проявляются только так.",
            "🔂 <b>{title}</b> на перечитке у {n}. Привязанность подтверждена.",
            "👏 {n} снова с <b>{title}</b> в руках. Уважаю преданность.",
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
        ],

        # ⭐ Оценка 1–3
        "completed_score_low": [
            "💩 Манга <b>{title}</b> — {score}/10. {n} дочитал из принципа. Терпеливый человек.",
            "😭 {score}/10 за <b>{title}</b>. Жертва времени принесена. Ради чего?",
            "🤮 <b>{title}</b> получает {score}/10. {n} явно не в восторге.",
            "⚰️ {score}/10 — <b>{title}</b> похоронена в памяти {n}.",
            "🧟 {n} пережил <b>{title}</b> ({score}/10). Медаль за стойкость.",
            "🔥 <b>{title}</b> — {score}/10. Сожжено, забыто, не рекомендуется.",
        ],

        # 😐 Оценка 4–6
        "completed_score_mid": [
            "😐 <b>{title}</b> — {score}/10. Среднячок. {n} не потрясён.",
            "🫤 {score}/10 за мангу <b>{title}</b>. Прочитал. Закрыл. Пошёл дальше.",
            "🤷 {n} поставил <b>{title}</b> {score}/10. Бывало лучше, бывало хуже.",
            "📊 <b>{title}</b> — {score}/10. В целом норм, но без огня.",
            "🌫️ {score}/10 — <b>{title}</b> прошла мимо сердца {n}.",
            "😶 Прочитал. Оценил. {score}/10. <b>{title}</b> не изменила мировоззрение.",
        ],

        # 🌟 Оценка 7–9
        "completed_score_high": [
            "🌟 Манга <b>{title}</b> — {score}/10! {n} доволен. Художник постарался.",
            "🔥 {score}/10 за <b>{title}</b>! {n} явно не разочарован.",
            "👏 <b>{title}</b> — {score}/10 от {n}. Достойное чтиво.",
            "✨ {score}/10 — <b>{title}</b> зацепила {n} за живое.",
            "🎉 {score}/10 за <b>{title}</b>. Рекомендую всем любителям хорошей манги.",
            "💫 <b>{title}</b> — {score}/10. Редкий случай, когда {n} доволен.",
        ],

        # 👑 Оценка 10
        "completed_score_perfect": [
            "👑 <b>{title}</b> — ДЕСЯТКА! {n} нашёл новый шедевр манги. Запишите.",
            "🏆 10/10! <b>{title}</b> — в пантеоне {n} навсегда.",
            "💎 {n} поставил манге <b>{title}</b> десятку. Художник может гордиться.",
            "🌌 10/10 за <b>{title}</b>. {n} дочитал и сидит в тишине. Это говорит всё.",
            "🎌 Максимум! <b>{title}</b> — теперь часть {n}. Прямо в душу.",
            "🔮 <b>{title}</b> получает священную десятку. {n} не шутит.",
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
        ],

        "manga": [
            "⭐ {n} добавил мангу <b>{title}</b> в избранное. Художник может гордиться.",
            "💫 <b>{title}</b> теперь в избранном у {n}. Среди всей прочитанной манги — особняком.",
            "🏅 Особая отметка: манга <b>{title}</b> в избранном {n}. Это не просто хорошо.",
            "✨ {n} выделил <b>{title}</b> среди всей манги. Редкий знак уважения.",
            "🌟 <b>{title}</b> — в избранном. {n} знает толк в хорошей манге.",
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
      "добавлено в список"  -> planned
      "просматриваю"        -> watching
      "изменена оценка с X на Y" -> score_changed
      "смотрю"               -> watching
      "читаю"               -> watching
      "пересматриваю"       -> rewatching
      "перечитываю"         -> rewatching
      "брошено"             -> dropped
      "просмотрено"         -> completed  (без оценки)
      "прочитано"           -> completed  (без оценки)
      "оценено на 9"        -> completed  (с оценкой, парсим отдельно)
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
    """
    # Тип медиа и конкретный вид (kind) — нужны для выбора банка сообщений
    media_type, _kind = get_media_info(entry)
    bank = MESSAGES[media_type]

    # Название тайтла — предпочитаем русское, экранируем для HTML
    target = entry.get("target") or {}
    title_ru = target.get("russian") or ""
    title_en = target.get("name") or "???"
    title = h(title_ru if title_ru else title_en)

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

    # Ссылка на тайтл — target.url приходит как "/animes/123-name" или "/mangas/456-name"
    target_url = (target.get("url") or "").strip()
    if target_url:
        full_url = f"{SHIKI_BASE_URL}{target_url}"
        text += f'\n🔗 <a href="{full_url}">Открыть на Shikimori</a>'

    # Временна́я метка события
    created_at = entry.get("created_at", "")
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            text += f"\n<i>🕐 {dt.strftime('%d.%m.%Y %H:%M')} UTC</i>"
        except ValueError:
            pass

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
    "original":      "Оригинал",
    "manga":         "Манга",
    "manhwa":        "Манхва",
    "manhua":        "Маньхуа",
    "light_novel":   "Ранобэ",
    "novel":         "Новелла",
    "visual_novel":  "Визуальная новелла",
    "game":          "Игра",
    "card_game":     "Карточная игра",
    "music":         "Музыка",
    "book":          "Книга",
    "web_manga":     "Веб-манга",
    "web_novel":     "Веб-новелла",
    "4_koma_manga":  "Ёнкома",
    "picture_book":  "Иллюстрированная книга",
    "radio":         "Радио",
    "other":         "Другое",
    "unknown":       "Неизвестно",
}

# Локализация возрастного рейтинга
_RATING_RU: dict[str, str] = {
    "none":   "Без рейтинга",
    "g":      "G",
    "pg":     "PG",
    "pg_13":  "PG-13",
    "r":      "R-17",
    "r_plus": "R+",
    "rx":     "Rx (Hentai)",
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
#  УТИЛИТЫ — КВАРТАЛ
# ═══════════════════════════════════════════════════════════════════

def current_quarter(dt: datetime | None = None) -> str:
    """'2026-Q2' для UTC-даты (по умолчанию — сейчас)."""
    if dt is None:
        dt = datetime.utcnow()
    q = (dt.month - 1) // 3 + 1
    return f"{dt.year}-Q{q}"


def quarter_start(dt: datetime | None = None) -> datetime:
    """Первый день текущего (или переданного) квартала, UTC."""
    if dt is None:
        dt = datetime.utcnow()
    q = (dt.month - 1) // 3 + 1
    return datetime(dt.year, (q - 1) * 3 + 1, 1)


def quarter_label(period: str) -> str:
    """'2026-Q2' → 'апрель — июнь 2026'. При ошибке возвращает исходную строку."""
    _names = {
        "Q1": "январь — март",
        "Q2": "апрель — июнь",
        "Q3": "июль — сентябрь",
        "Q4": "октябрь — декабрь",
    }
    try:
        year, q = period.split("-", 1)
        return f"{_names.get(q, q)} {year}"
    except Exception:
        return period


def _quarter_end(period: str) -> datetime | None:
    """Последний день квартала по строке '2026-Q2' (для отображения диапазона)."""
    try:
        year_s, q_s = period.split("-Q", 1)
        year, q = int(year_s), int(q_s)
        # Первый месяц следующего квартала минус один день
        end_month = q * 3  # последний месяц квартала (3,6,9,12)
        if end_month == 12:
            return datetime(year, 12, 31)
        return datetime(year, end_month + 1, 1) - timedelta(days=1)
    except Exception:
        return None


def tracking_period_label(cur: dict) -> str:
    """
    Человекочитаемый диапазон фактического отслеживания текущего квартала.
    'с 25.04.2026 по 30.06.2026' если бот стартовал в середине,
    'с 01.04.2026 по 30.06.2026' если с начала.
    При проблемах с датами — деградирует до quarter_label.
    """
    period = cur.get("period") or current_quarter()
    try:
        ts_raw = cur.get("tracking_since") or cur.get("period_start")
        start = datetime.fromisoformat(ts_raw) if ts_raw else None
        end = _quarter_end(period)
        if start and end:
            return f"с {start.strftime('%d.%m.%Y')} по {end.strftime('%d.%m.%Y')}"
    except Exception:
        pass
    return quarter_label(period)


def _is_partial_quarter(cur: dict) -> bool:
    """True, если отслеживание началось позже календарного начала квартала."""
    try:
        ts = cur.get("tracking_since")
        ps = cur.get("period_start")
        if ts and ps:
            return datetime.fromisoformat(ts) > datetime.fromisoformat(ps)
    except Exception:
        pass
    return False


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
                        "url":         (item.get("url") or "").strip(),
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
    }


def load_stats_all(use_cache: bool = True) -> dict:
    """
    Загружаем stats_all.json (с коротким in-memory кэшем).
    При ошибке — пустая структура, бот не падает.
    """
    global _stats_all_cache, _stats_all_cache_ts

    if use_cache and _stats_all_cache is not None:
        age = datetime.utcnow().timestamp() - _stats_all_cache_ts
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
    _stats_all_cache_ts = datetime.utcnow().timestamp()
    return data


def save_stats_all(data: dict) -> None:
    """Сохраняем stats_all.json атомарно + обновляем кэш."""
    global _stats_all_cache, _stats_all_cache_ts
    try:
        data["updated_at"] = datetime.utcnow().isoformat()
        _atomic_write(STATS_ALL_FILE, json.dumps(data, ensure_ascii=False, indent=2))
        _stats_all_cache = data
        _stats_all_cache_ts = datetime.utcnow().timestamp()
    except Exception as e:
        log.error("save_stats_all: не удалось записать файл: %s", e)


# ═══════════════════════════════════════════════════════════════════
#  ПОСТРОЕНИЕ titles{} ИЗ list_export + МЕТАДАННЫХ
# ═══════════════════════════════════════════════════════════════════

def _safe_int(value, default: int = 0) -> int:
    """Аккуратно приводим к int — score/episodes из экспорта бывают строками."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float | None = None) -> float | None:
    """Приводим к float — GraphQL score приходит как строка '8.73'. None если не вышло."""
    try:
        f = float(value)
        return f if f > 0 else default
    except (TypeError, ValueError):
        return default


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

    Вызывается при старте бота. Никаких уведомлений не шлёт.
    Возвращает обновлённый stats_all (или текущий при сбое экспорта).
    """
    stats = load_stats_all(use_cache=False)

    async with aiohttp.ClientSession() as session:
        export_anime = await fetch_list_export(session, "anime")
        export_manga = await fetch_list_export(session, "manga")

    if export_anime is None and export_manga is None:
        log.warning("sync_stats_all: оба экспорта недоступны — пропускаем синхронизацию.")
        return stats

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

        # Подтягиваем метаданные для новых
        meta_map: dict[str, dict] = {}
        if new_ids:
            log.info("sync_stats_all(%s): новых тайтлов для обогащения: %d", media, len(new_ids))
            try:
                meta_map = await fetch_meta_batch(media, new_ids)
            except Exception as e:
                log.error("sync_stats_all(%s): fetch_meta_batch упал: %s", media, e)

        # Обновляем / создаём записи
        for tid, row in valid_rows.items():
            if tid in titles:
                # Существующая запись — обновляем только пользовательский стейт,
                # метаданные (genres/studios/...) уже есть, не трогаем.
                rec = titles[tid]
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
                # Новая запись
                titles[tid] = _merge_title_record(media, row, meta_map.get(tid))
                changed = True

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

    if changed:
        save_stats_all(stats)
        log.info("sync_stats_all: stats_all.json обновлён.")
    else:
        log.info("sync_stats_all: изменений нет.")

    return stats


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
                return data
            log.warning("load_stats_current: неожиданная структура, сбрасываем.")
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("load_stats_current: %s", e)

    # Истинно первый запуск (или сброс) — фиксируем фактическую дату старта
    now = datetime.utcnow()
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
            "recorded_at": datetime.utcnow().isoformat(),
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
    """'Экшен (34) · Драма (28)'."""
    return sep.join(f"{h(k)} ({v})" for k, v in _top_dict(counter, n))


def _fmt_score_dist(dist: dict) -> str:
    """Распределение оценок без нулей (0 = без оценки): '10×8 · 9×15'."""
    pairs = [(int(s), c) for s, c in dist.items() if _safe_int(s) > 0]
    if not pairs:
        return "нет оценок"
    return "  ·  ".join(f"{s}×{c}" for s, c in sorted(pairs, reverse=True))


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
    url = (rec.get("url") or "").strip()
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

    # ── Аниме ───────────────────────────────────
    a: list[str] = ["📊 <b>СТАТИСТИКА ЗА ВСЁ ВРЕМЯ</b>"]
    if upd_str:
        a.append(f"<i>актуально на {upd_str}</i>")
    a.append("")
    a.append("🎬 <b>━━━ АНИМЕ ━━━</b>")
    a.append(f"✅ Завершено: <b>{a_agg.get('total_completed', 0)}</b>")
    a.append(
        f"🗑️ Брошено: {a_agg.get('total_dropped', 0)}  ·  "
        f"▶️ Смотрит: {a_agg.get('total_watching', 0)}  ·  "
        f"📋 В планах: {a_agg.get('total_planned', 0)}"
    )
    avg_a = _avg_score_from_dist(a_agg.get("score_dist", {}))
    if avg_a is not None:
        line = f"⭐ Средняя оценка: <b>{avg_a}</b>"
        avg_shiki_a = a_agg.get("avg_shiki_completed")
        if isinstance(avg_shiki_a, (int, float)):
            diff = round(avg_a - avg_shiki_a, 1)
            sign = "+" if diff >= 0 else ""
            line += f"  <i>(Shikimori: {round(avg_shiki_a, 1)}, {sign}{diff})</i>"
        a.append(line)
    sd = _fmt_score_dist(a_agg.get("score_dist", {}))
    if sd != "нет оценок":
        a.append(f"📊 {sd}")
    eps = a_agg.get("total_episodes_watched", 0)
    hrs = a_agg.get("total_hours_watched", 0)
    if eps:
        a.append(f"📺 Эпизодов просмотрено: <b>{eps}</b>  (~{hrs} ч.)")
    if a_agg.get("genres"):
        a.append(f"🎭 Жанры: {_fmt_counter(a_agg['genres'], 5)}")
    if a_agg.get("themes"):
        a.append(f"🏷️ Темы: {_fmt_counter(a_agg['themes'], 5)}")
    if a_agg.get("studios"):
        a.append(f"🎨 Студии: {_fmt_counter(a_agg['studios'], 5)}")
    if a_agg.get("origins"):
        a.append(f"📺 Источники: {_fmt_counter(a_agg['origins'], 4)}")
    if a_agg.get("ratings"):
        a.append(f"🔞 Рейтинги: {_fmt_counter(a_agg['ratings'], 4)}")

    # ── Манга ───────────────────────────────────
    m: list[str] = ["📚 <b>━━━ МАНГА ━━━</b>"]
    m.append(f"✅ Прочитано: <b>{m_agg.get('total_completed', 0)}</b>")
    m.append(
        f"🗑️ Брошено: {m_agg.get('total_dropped', 0)}  ·  "
        f"📖 Читает: {m_agg.get('total_watching', 0)}  ·  "
        f"📋 В планах: {m_agg.get('total_planned', 0)}"
    )
    avg_m = _avg_score_from_dist(m_agg.get("score_dist", {}))
    if avg_m is not None:
        line = f"⭐ Средняя оценка: <b>{avg_m}</b>"
        avg_shiki_m = m_agg.get("avg_shiki_completed")
        if isinstance(avg_shiki_m, (int, float)):
            diff = round(avg_m - avg_shiki_m, 1)
            sign = "+" if diff >= 0 else ""
            line += f"  <i>(Shikimori: {round(avg_shiki_m, 1)}, {sign}{diff})</i>"
        m.append(line)
    sd_m = _fmt_score_dist(m_agg.get("score_dist", {}))
    if sd_m != "нет оценок":
        m.append(f"📊 {sd_m}")
    ch = m_agg.get("total_chapters_read", 0)
    vol = m_agg.get("total_volumes_read", 0)
    if ch:
        m.append(f"📖 Глав прочитано: <b>{ch}</b>  ·  томов: {vol}")
    if m_agg.get("genres"):
        m.append(f"🎭 Жанры: {_fmt_counter(m_agg['genres'], 5)}")
    if m_agg.get("themes"):
        m.append(f"🏷️ Темы: {_fmt_counter(m_agg['themes'], 5)}")
    if m_agg.get("publishers"):
        m.append(f"🏢 Издатели: {_fmt_counter(m_agg['publishers'], 5)}")

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
        lines.append(f"📊 {_fmt_score_dist(dist)}")

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
            url = (r.get("url") or "").strip()
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

    # Жанры/темы из записей квартала
    genres: dict = {}
    themes: dict = {}
    for r in records:
        for g in r.get("genres", []):
            _bump(genres, g)
        for t in r.get("themes", []):
            _bump(themes, t)

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
        if studios:
            lines.append(f"🎨 Студии: {_fmt_counter(studios, 3)}")
        if origins:
            lines.append(f"📺 Источники: {_fmt_counter(origins, 3)}")
    else:
        publishers: dict = {}
        total_ch = 0
        for r in records:
            for p in r.get("publishers", []):
                _bump(publishers, p)
            total_ch += _safe_int(r.get("chapters_read"))
        if total_ch:
            lines.append(f"📖 Глав прочитано: <b>{total_ch}</b>")
        if publishers:
            lines.append(f"🏢 Издатели: {_fmt_counter(publishers, 3)}")

    if genres:
        lines.append("")
        lines.append(f"🎭 Жанры: {_fmt_counter(genres, 5)}")
    if themes:
        lines.append(f"🏷️ Темы: {_fmt_counter(themes, 3)}")

    return lines


def _anime_block(cur: dict, comp: list[dict], drop: list[dict], plan: int, header: str) -> str:
    """Готовый текст блока АНИМЕ (одно сообщение)."""
    lines: list[str] = [header]
    lines.append(f"✅ Завершено: <b>{len(comp)}</b>")
    if drop:
        lines.append(f"🗑️ Брошено: {len(drop)}")
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
    lines: list[str] = [header]
    lines.append(f"✅ Прочитано: <b>{len(comp)}</b>")
    if drop:
        lines.append(f"🗑️ Брошено: {len(drop)}")
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
      [0] аниме, [1] манга, [2] подвал с подсказкой про /stats all.
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
    msgs.append(header + "\n\n" + _anime_block(cur, comp_a, drop_a, plan_a, "🎬 <b>━━━ АНИМЕ ━━━</b>"))
    msgs.append(_manga_block(cur, comp_m, drop_m, plan_m, "📚 <b>━━━ МАНГА ━━━</b>"))
    msgs.append("<i>Полная статистика за всё время: /stats all</i>")
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
    msgs.append(header + "\n\n" + _anime_block(cur, comp_a, drop_a, plan_a, "🎬 <b>━━━ АНИМЕ ━━━</b>"))

    # Сообщение 2: манга
    msgs.append(_manga_block(cur, comp_m, drop_m, plan_m, "📚 <b>━━━ МАНГА ━━━</b>"))

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
        stats_all = await sync_stats_all()
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

async def _send_long(bot: Bot, chat_id: int, text: str) -> None:
    """Отправка с разбивкой по строкам если > 4000 символов (не рвём HTML-теги)."""
    MAX = 4000
    try:
        if len(text) <= MAX:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
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
            await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.5)
    except Exception as e:
        log.error("_send_long: не удалось отправить (chat_id=%d): %s", chat_id, e)


# ═══════════════════════════════════════════════════════════════════
#  КОМАНДА /stats  [all]
# ═══════════════════════════════════════════════════════════════════

async def cmd_stats(message: Message) -> None:
    """
    /stats      — статистика за текущий квартал.
    /stats all  — агрегированная статистика за всё время.
    Доступна всем подписчикам. Не делает сетевых запросов (читает файлы) —
    мгновенно и не может упасть из-за недоступности API.
    """
    arg = ""
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1].strip().lower()

    try:
        stats_all = load_stats_all()
    except Exception as e:
        log.error("cmd_stats: load_stats_all: %s", e)
        await message.answer("⚠️ Не удалось загрузить статистику, попробуй позже.")
        return

    try:
        if arg in ("all", "всё", "все"):
            msgs = build_stats_all_messages(stats_all)
        else:
            cur = load_stats_current()
            msgs = build_current_stats_messages(cur, stats_all)
    except Exception as e:
        log.error("cmd_stats: ошибка формирования: %s", e)
        await message.answer("⚠️ Не удалось сформировать статистику, попробуй позже.")
        return

    # Отправляем по сообщению на тему (аниме / манга / подвал)
    for msg in msgs:
        if not msg or not msg.strip():
            continue
        await _send_long(message.bot, message.chat.id, msg)
        await asyncio.sleep(0.3)

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
    category: "animes" | "mangas" | "characters" | "people"
    item:     объект из API с полями id, name, russian, url и др.
    """
    # Категория API → ключ банка сообщений
    cat_map = {
        "animes":     "anime",
        "mangas":     "manga",
        "characters": "character",
        "people":     "person",
    }
    bank_key = cat_map.get(category, "anime")
    templates = MESSAGES["favourites"].get(bank_key, MESSAGES["favourites"]["anime"])

    title_ru = item.get("russian") or ""
    title_en = item.get("name") or "???"
    title = h(title_ru if title_ru else title_en)

    text = random.choice(templates).format(n=DISPLAY_NAME, title=title)

    url = (item.get("url") or "").strip()
    if url:
        text += f'\n🔗 <a href="{SHIKI_BASE_URL}{url}">Открыть на Shikimori</a>'

    return text


async def check_and_notify_favourites(bot: Bot, seen: set[str]) -> set[str]:
    """
    Проверяем избранное:
    1. Загружаем текущий список с Shikimori
    2. Находим новые элементы (которых нет в seen)
    3. Отправляем уведомления и обновляем seen
    Ключ в seen: "{category}_{id}", например "animes_5114".
    """
    async with aiohttp.ClientSession() as session:
        favourites = await fetch_favourites(session)

    if favourites is None:
        log.info("Запрос избранного не удался — пропускаем цикл.")
        return seen

    # Категории которые отслеживаем
    tracked = ("animes", "mangas", "characters", "people")
    found_new = False

    for category in tracked:
        items = favourites.get(category) or []
        for item in items:
            item_id = item.get("id")
            if item_id is None:
                continue
            key = f"{category}_{item_id}"
            if key in seen:
                continue

            # Новый элемент в избранном
            seen.add(key)
            found_new = True
            log.info("Новое в избранном: %s (id=%s)", category, item_id)

            text = build_favourite_message(category, item)
            await send_to_all_chats(bot, text)
            await asyncio.sleep(1)

    if not found_new:
        log.info("Изменений в избранном нет.")

    save_seen_favourites(seen)
    return seen


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
        score       = extract_score(description) if event_type == "completed" else None
        cur = record_current_event(cur, entry, event_type, media_type, score)

        text = build_message(entry)
        await send_to_all_chats(bot, text)

        # Пауза между разными событиями — не спамим Telegram
        await asyncio.sleep(1)

    save_seen_ids(seen_ids)
    save_stats_current(cur)
    return seen_ids, cur


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
            for category in ("animes", "mangas", "characters", "people"):
                for item in (favourites.get(category) or []):
                    if item.get("id") is not None:
                        seen_favs.add(f"{category}_{item['id']}")
            save_seen_favourites(seen_favs)
            log.info("Инициализировано %d записей избранного.", len(seen_favs))

    # Актуализируем полную статистику из list_export (не зависит от seen_ids,
    # строится сразу даже на первом запуске — данные берутся не из history).
    log.info("Синхронизируем статистику за всё время (stats_all)...")
    try:
        stats_all = await sync_stats_all()
    except Exception as e:
        log.exception("Не удалось синхронизировать stats_all при старте: %s", e)
        stats_all = load_stats_all()

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
            seen_favs     = await check_and_notify_favourites(bot, seen_favs)

            # Проверяем смену квартала (раз в цикл, дёшево).
            # Внутри — защита last_report_sent от повторной отправки.
            cur = await rotate_quarter_if_needed(bot, cur, load_stats_all())

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


async def cmd_export(message: Message) -> None:
    """Отправить subscribers.json владельцу."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer("🚫 Эта команда только для владельца бота.")
        return

    path = Path(SUBS_FILE)
    if not path.exists() or path.stat().st_size == 0:
        await message.answer("📭 Файл подписчиков пуст или не существует.")
        return

    subs = load_subscribers()
    await message.answer_document(
        document=FSInputFile(path, filename="subscribers.json"),
        caption=f"📤 Экспорт подписчиков — {len(subs)} чел.",
    )
    log.info("Экспорт subscribers.json отправлен владельцу.")


async def cmd_import(message: Message) -> None:
    """
    Загрузить subscribers.json из присланного файла.
    Использование: отправить файл боту с подписью /import
    (или переслать команду отдельным сообщением — бот попросит прислать файл).
    """
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer("🚫 Эта команда только для владельца бота.")
        return

    if not message.document:
        await message.answer(
            "📎 Пришли файл <code>subscribers.json</code> боту с подписью <code>/import</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not (message.document.file_name or "").endswith(".json"):
        await message.answer("❌ Ожидается .json файл.")
        return

    tmp_path = SUBS_FILE.with_name(SUBS_FILE.name + ".import_tmp")
    try:
        await message.bot.download(message.document, destination=tmp_path)
        raw = Path(tmp_path).read_text(encoding="utf-8")
        data = json.loads(raw)
        subs = {int(k): v for k, v in data.get("subscribers", {}).items()}
    except Exception as e:
        await message.answer(f"❌ Не удалось прочитать файл: {e}")
        return
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    save_subscribers(subs)
    log.info("Импортировано %d подписчиков от владельца.", len(subs))
    await message.answer(f"✅ Импортировано подписчиков: {len(subs)}")


async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    """Начать рассылку сообщения подписчикам."""
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.answer("🚫 Эта команда только для владельца бота.")
        return
    await state.set_state(BroadcastStates.waiting_content)
    await message.answer(
        "✍️ Пришли сообщение для рассылки.\n"
        "Поддерживаются: текст, фото, видео, GIF, стикер, документ, голосовое.\n\n"
        "/cancel — передумал",
    )


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Отменить текущую операцию (работает в любом FSM-состоянии)."""
    if await state.get_state() is None:
        await message.answer("🤷 Нечего отменять.")
        return
    await state.clear()
    await message.answer("❌ Отменено.")


async def broadcast_receive(message: Message, state: FSMContext) -> None:
    """Получаем сообщение от владельца, сохраняем в FSM и показываем превью."""
    # Определяем тип и извлекаем нужные данные
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

    await state.update_data(**data)
    await state.set_state(BroadcastStates.waiting_confirm)

    # Превью — отправляем владельцу как будет выглядеть
    await message.answer("👀 Вот как увидят подписчики:")
    await _send_broadcast_message(message.bot, message.chat.id, data)
    subs_count = len(load_subscribers())
    await message.answer(
        f"Отправить {subs_count} подписчик(ам)?",
        reply_markup=_confirm_kb(),
    )


async def broadcast_confirm_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение рассылки — отправляем всем подписчикам."""
    data = await state.get_data()
    await state.clear()
    # Убираем кнопки с превью-сообщения
    await callback.message.edit_reply_markup(reply_markup=None)

    subs = load_subscribers()
    if not subs:
        await callback.answer()
        await callback.message.answer("📭 Подписчиков нет — некому отправлять.")
        return

    await callback.answer("Отправляю...")
    sent, failed = 0, 0
    to_remove: list[int] = []

    for chat_id, name in subs.items():
        try:
            await _send_broadcast_message(callback.message.bot, chat_id, data)
            sent += 1
            log.info("  broadcast → %s (chat_id=%d)", name, chat_id)
        except Exception as e:
            err = str(e).lower()
            if "bot was blocked" in err or "user is deactivated" in err or "chat not found" in err:
                log.warning("  broadcast ✗ %s (chat_id=%d) заблокировал бота.", name, chat_id)
                to_remove.append(chat_id)
            else:
                log.error("  broadcast ✗ %s (chat_id=%d): %s", name, chat_id, e)
            failed += 1
        await asyncio.sleep(0.3)

    if to_remove:
        for cid in to_remove:
            subs.pop(cid, None)
        save_subscribers(subs)
        log.info("Отписано %d заблокировавших бота.", len(to_remove))

    await callback.message.answer(
        f"✅ Отправлено: {sent}" + (f", ошибок: {failed}" if failed else "") + "."
    )


async def broadcast_cancel_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена рассылки через кнопку."""
    await state.clear()
    await callback.answer("Отменено.")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("❌ Рассылка отменена.")


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
    dp.message.register(cmd_export,    Command("export"))
    dp.message.register(cmd_import,    Command("import"))
    dp.message.register(cmd_status,    Command("status"))
    dp.message.register(cmd_broadcast, Command("broadcast"))
    dp.message.register(cmd_cancel,    Command("cancel"))
    dp.message.register(cmd_stats,     Command("stats"))

    # FSM-обработчики для /broadcast
    dp.message.register(broadcast_receive, BroadcastStates.waiting_content)
    dp.callback_query.register(broadcast_confirm_cb, F.data == "broadcast_send",   BroadcastStates.waiting_confirm)
    dp.callback_query.register(broadcast_cancel_cb,  F.data == "broadcast_cancel", BroadcastStates.waiting_confirm)

    # Публичные команды в меню "/" — команды владельца не показываем
    await bot.set_my_commands([
        BotCommand(command="start",  description="Подписаться на уведомления 🥳"),
        BotCommand(command="stop",   description="Отписаться 😢"),
        BotCommand(command="status", description=f"Что сейчас смотрит и читает {DISPLAY_NAME} 👀"),
        BotCommand(command="stats",  description="Статистика за квартал 📊"),
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

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
