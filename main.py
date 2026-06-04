"""
Shikimori History Watcher Bot
Следит за историей и избранным пользователя на Shikimori и отправляет весёлые сообщения в Telegram.

Зависимости:
    pip install -r requirements.txt
    pip install -r requirements-dev.txt

Запуск:
    1. Создать бота через @BotFather, получить BOT_TOKEN
    2. Задать переменные окружения и запустить:
           export BOT_TOKEN='токен_от_BotFather'
           export OWNER_ID='твой_telegram_id'
           python main.py
    3. Написать боту /start — бот запомнит тебя и начнёт слать уведомления
    4. Поделиться ссылкой на бота с друзьями — они тоже пишут /start

Команды бота:
    /start      — подписаться на уведомления
    /stop       — отписаться
    /status     — что сейчас смотрит/читает пользователь
    /broadcast  — написать подписчикам (только для владельца)
    /cancel     — отменить текущую операцию
    /subs       — список подписчиков (только для владельца)
    /export     — выгрузить subscribers.json (только для владельца)
    /import     — загрузить subscribers.json из файла (только для владельца)

Отслеживаемые события:
    История: добавил в список, начал смотреть/читать, пересматривает,
             бросил, завершил, поставил оценку, изменил оценку
    Избранное: добавил аниме, мангу, персонажа или человека индустрии
"""

import asyncio
import html
import json
import os
import logging
import re
import random
from pathlib import Path
from datetime import datetime

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

SHIKI_USER     = "WNR"              # ник на Shikimori (для API)
SHIKI_BASE_URL = "https://shikimori.io"  # домен — меняй здесь при смене зеркала
DISPLAY_NAME   = "Ворга"           # отображаемое имя в сообщениях
CHECK_INTERVAL = 15 * 60           # интервал проверки в секундах (15 минут)
# Пути к файлам данных.
# По умолчанию создаются в рабочей директории.
# Чтобы хранить в другом месте — задай переменную окружения DATA_DIR=/путь/к/папке
_DATA_DIR      = os.environ.get("DATA_DIR", ".")
SEEN_IDS_FILE  = f"{_DATA_DIR}/seen_ids.json"        # ID обработанных событий
SUBS_FILE      = f"{_DATA_DIR}/subscribers.json"     # список подписчиков
SEEN_FAVS_FILE = f"{_DATA_DIR}/seen_favourites.json" # ID избранного

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


# ═══════════════════════════════════════════════════════════════
#  ОСНОВНАЯ ЛОГИКА
# ═══════════════════════════════════════════════════════════════

async def fetch_history(session: aiohttp.ClientSession) -> list[dict]:
    """Запрашиваем историю с API Shikimori."""
    try:
        async with session.get(
            HISTORY_URL,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                log.warning("API вернул статус %d", resp.status)
                return []
            return await resp.json()
    except aiohttp.ClientError as e:
        log.error("Ошибка запроса к Shikimori: %s", e)
        return []


async def fetch_favourites(session: aiohttp.ClientSession) -> dict:
    """
    Запрашиваем избранное с API Shikimori.
    Возвращает словарь вида:
      {"animes": [...], "mangas": [...], "characters": [...], "people": [...], ...}
    Каждый элемент содержит хотя бы "id", "name", "russian", "url".
    """
    try:
        async with session.get(
            FAVOURITES_URL,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                log.warning("fetch_favourites: API вернул статус %d", resp.status)
                return {}
            return await resp.json()
    except aiohttp.ClientError as e:
        log.error("Ошибка запроса избранного к Shikimori: %s", e)
        return {}


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

    if not favourites:
        log.info("Избранное пусто или запрос не удался.")
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


async def check_and_notify(bot: Bot, seen_ids: set[int]) -> set[int]:
    """
    Главная функция проверки:
    1. Загружаем историю с Shikimori
    2. Фильтруем новые записи (которых нет в seen_ids)
    3. Для каждой новой — формируем сообщение и шлём во все чаты
    4. Обновляем seen_ids и возвращаем его
    """
    async with aiohttp.ClientSession() as session:
        entries = await fetch_history(session)

    if not entries:
        log.info("История пуста или запрос не удался.")
        return seen_ids

    new_entries = [e for e in entries if e["id"] not in seen_ids]

    if not new_entries:
        log.info("Новых записей нет.")
        return seen_ids

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
        text = build_message(entry)
        await send_to_all_chats(bot, text)

        # Пауза между разными событиями — не спамим Telegram
        await asyncio.sleep(1)

    save_seen_ids(seen_ids)
    return seen_ids


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
    log.info(
        "Бот запущен. Отображаемое имя: %s | Подписчиков: %d | Виденных ID: %d | Интервал: %d сек.",
        DISPLAY_NAME, len(load_subscribers()), len(seen_ids), CHECK_INTERVAL,
    )

    if not seen_ids:
        log.info("Первый запуск — инициализируем историю без отправки сообщений.")
        async with aiohttp.ClientSession() as session:
            entries = await fetch_history(session)
        seen_ids = {e["id"] for e in entries}
        save_seen_ids(seen_ids)
        log.info("Инициализировано %d ID истории.", len(seen_ids))

    if not seen_favs:
        log.info("Инициализируем избранное без отправки сообщений.")
        async with aiohttp.ClientSession() as session:
            favourites = await fetch_favourites(session)
        for category in ("animes", "mangas", "characters", "people"):
            for item in (favourites.get(category) or []):
                if item.get("id") is not None:
                    seen_favs.add(f"{category}_{item['id']}")
        save_seen_favourites(seen_favs)
        log.info("Инициализировано %d записей избранного.", len(seen_favs))

    while True:
        log.info("Проверяем историю и избранное...")
        seen_ids  = await check_and_notify(bot, seen_ids)
        seen_favs = await check_and_notify_favourites(bot, seen_favs)
        log.info("Следующая проверка через %d мин.", CHECK_INTERVAL // 60)
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

    tmp_path = SUBS_FILE + ".import_tmp"
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


async def fetch_current_rates(media: str, statuses: list[str]) -> list[dict]:
    """
    Запрашивает тайтлы в указанных статусах.
    media:    "anime" или "manga"
    statuses: ["watching", "rewatching"] — одинаково для аниме и манги
    Возвращает объединённый список записей со всех статусов.
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
            except aiohttp.ClientError as e:
                log.error("fetch_current_rates ошибка (%s/%s): %s", media, status, e)
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
    anime_task = asyncio.create_task(fetch_current_rates("anime", ["watching", "rewatching"]))
    manga_task = asyncio.create_task(fetch_current_rates("manga", ["watching", "rewatching"]))
    anime_list, manga_list = await asyncio.gather(anime_task, manga_task)

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

    # FSM-обработчики для /broadcast
    dp.message.register(broadcast_receive, BroadcastStates.waiting_content)
    dp.callback_query.register(broadcast_confirm_cb, F.data == "broadcast_send",   BroadcastStates.waiting_confirm)
    dp.callback_query.register(broadcast_cancel_cb,  F.data == "broadcast_cancel", BroadcastStates.waiting_confirm)

    # Публичные команды в меню "/" — команды владельца не показываем
    await bot.set_my_commands([
        BotCommand(command="start",  description="Подписаться на уведомления 🥳"),
        BotCommand(command="stop",   description="Отписаться 😢"),
        BotCommand(command="status", description=f"Что сейчас смотрит и читает {DISPLAY_NAME} 👀"),
    ])

    # polling_loop работает параллельно как фоновая задача
    asyncio.create_task(polling_loop(bot))

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
