"""
Shikimori History Watcher Bot
Следит за историей пользователя на Shikimori и отправляет весёлые сообщения в Telegram.

Зависимости:
    pip install aiogram aiohttp

Запуск:
    1. Создать бота через @BotFather, получить BOT_TOKEN
    2. Задать переменные окружения и запустить:
           export BOT_TOKEN='токен_от_BotFather'
           export OWNER_ID='твой_telegram_id'
           python shikimori_bot.py
    3. Написать боту /start — бот запомнит тебя и начнёт слать уведомления
    4. Поделиться ссылкой на бота с друзьями — они тоже пишут /start

Команды бота:
    /start — подписаться на уведомления
    /stop  — отписаться
    /subs  — посмотреть список подписчиков (только для владельца)
"""

import asyncio
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
from aiogram.types import Message, BotCommand

# ─────────────────────────────────────────────
#  НАСТРОЙКИ — заполни перед запуском
# ─────────────────────────────────────────────
# Токен читается из переменной окружения BOT_TOKEN — не храни его в коде!
# Задать: export BOT_TOKEN="токен_от_BotFather"
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Твой личный Telegram ID — узнать можно у @userinfobot.
# Нужен для команды /subs (только владелец видит список подписчиков).
# Твой Telegram ID — узнать у @userinfobot
# Задать: export OWNER_ID="123456789"
OWNER_ID = int(os.environ["OWNER_ID"])

SHIKI_USER     = "WNR"              # ник на Shikimori (для API)
SHIKI_BASE_URL = "https://shikimori.io"  # домен — меняй здесь при смене зеркала
DISPLAY_NAME   = "Ворга"           # отображаемое имя в сообщениях
CHECK_INTERVAL = 15 * 60           # интервал проверки в секундах (15 минут)
SEEN_IDS_FILE  = "seen_ids.json"   # файл для хранения виденных ID
SUBS_FILE      = "subscribers.json" # файл для хранения подписчиков

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

# ─────────────────────────────────────────────
#  API
# ─────────────────────────────────────────────
HISTORY_URL = f"{SHIKI_BASE_URL}/api/users/{SHIKI_USER}/history?limit=50"
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
#  БАНК СООБЩЕНИЙ
#
#  Переменные в шаблонах:
#    {n}     — отображаемое имя пользователя (DISPLAY_NAME)
#    {title} — название аниме или манги
#    {score} — оценка (только в completed_score_*)
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
            "📋 {n} закинул *{title}* в бесконечный список «посмотрю когда-нибудь». Ждём.",
            "🗂️ *{title}* занял своё место в очереди на годы. Дождётся ли?",
            "📌 {n} запланировал *{title}*. Статистика говорит: 80% таких тайтлов умирают непросмотренными.",
            "🧠 Судьба *{title}* решена — оно теперь в списке. Просмотр — под вопросом.",
            "🔖 *{title}* добавлено в коллекцию намерений {n}. Осталось только посмотреть.",
            "📥 Хоп — и *{title}* в planned. Как будто кто-то собирается это смотреть 👀",
        ],

        # ▶️ Начал смотреть
        "watching": [
            "▶️ {n} начал смотреть *{title}*. Запасаемся попкорном.",
            "🎬 Поехали! *{title}* запущено. Возврата нет.",
            "👁️ {n} открыл *{title}* и пропал. Ждём отчёта.",
            "🍿 *{title}* в плеере, {n} у экрана. Классика.",
            "🚀 Старт! *{title}* вышло на орбиту просмотра.",
            "😤 {n} не выдержал и таки начал *{title}*. Посмотрим, чем это закончится.",
        ],

        # 🔁 Пересматривает
        "rewatching": [
            "🔁 {n} пересматривает *{title}*. Не надоело — значит шедевр (или мазохизм).",
            "♻️ *{title}* снова в деле. {n} возвращается к проверенному.",
            "🌀 Повторный заход на *{title}*. Уважаю.",
            "📺 {n} включил *{title}* ещё раз. Некоторые вещи просто не отпускают.",
            "🔂 *{title}* на втором (третьем? десятом?) круге у {n}. Это уже традиция.",
            "👏 Решился на ремастер впечатлений — *{title}* снова смотрит {n}.",
        ],

        # 💀 Бросил (dropped)
        "dropped": [
            "🗑️ *{title}* — в мусор. {n} не пощадил.",
            "💀 Dropped. *{title}* не пережило встречи с {n}.",
            "🚪 {n} покинул *{title}* без объяснений. Бывает.",
            "❌ *{title}* — дропнуто. Минус одно аниме в этом жестоком мире.",
            "😤 {n} посмотрел на *{title}* и сказал «нет». Твёрдая позиция.",
            "🏳️ *{title}* не справилось с испытанием {n}. Позор или избавление — решай сам.",
        ],

        # ✅ Завершил без оценки
        "completed_no_score": [
            "✅ {n} досмотрел *{title}*. Оценку зажал — интригует.",
            "🏁 *{title}* завершено. Впечатления {n} покрыты тайной.",
            "👀 Конец *{title}*. Молчание {n} красноречивее слов.",
            "📺 {n} прошёл путь *{title}* до конца. Без комментариев.",
            "🎌 *{title}* — пройдено. Оценка — не для слабонервных, видимо.",
            "🤐 Закончил *{title}* и молчит. Либо шедевр, либо травма.",
        ],

        # ⭐ Оценка 1–3
        "completed_score_low": [
            "💩 *{title}* — {score}/10. {n} страдал, но добил. Настоящий герой.",
            "😭 {score}/10 за *{title}*. Боль реальна. Зачем вообще?",
            "🤮 *{title}* получает {score}/10 от {n}. Это приговор.",
            "⚰️ {score}/10 — *{title}* мертво и похоронено в памяти {n}.",
            "🧟 {n} выжил после *{title}* ({score}/10). Это уже достижение.",
            "🔥 *{title}* — {score}/10. Сожжено дотла заслуженно.",
        ],

        # 😐 Оценка 4–6
        "completed_score_mid": [
            "😐 *{title}* — {score}/10. Ни рыба ни мясо, говорит {n}.",
            "🫤 {score}/10 за *{title}*. Не плохо, не хорошо. Просто... было.",
            "🤷 {n} поставил *{title}* {score}/10. Среднячок прожил и умер.",
            "📊 *{title}* — твёрдый {score}/10. {n} явно ожидал большего.",
            "🌫️ {score}/10 — *{title}* оставило {n} в тумане безразличия.",
            "😶 Посмотрел. Оценил. {score}/10. *{title}* не потрясло мир {n}.",
        ],

        # 🌟 Оценка 7–9
        "completed_score_high": [
            "🌟 *{title}* — {score}/10! {n} доволен. Хороший вкус подтверждён.",
            "🔥 {score}/10 за *{title}*! {n} в восторге, и это заслужено.",
            "👏 *{title}* получает {score}/10 от {n}. Браво, студия!",
            "✨ {score}/10 — *{title}* попало в сердечко {n}.",
            "🎉 Вот это да! {score}/10 за *{title}*. Рекомендую к просмотру всем.",
            "💫 *{title}* — {score}/10. {n} явно не разочарован. Редкий случай.",
        ],

        # 👑 Оценка 10
        "completed_score_perfect": [
            "👑 *{title}* — ДЕСЯТКА! {n} нашёл новый фаворит. Занесите в анналы.",
            "🏆 10/10! *{title}* вошло в пантеон {n}. Это серьёзно.",
            "💎 {n} раздаёт десятки! *{title}* — абсолютный шедевр по его версии.",
            "🌌 10/10 за *{title}*. {n} разрушен и счастлив одновременно.",
            "🎌 Максимум! *{title}* — теперь часть души {n}. Трогательно.",
            "🔮 *{title}* получает священную десятку. {n} преклоняется.",
        ],
    },

    # ────────────────────────────────
    #  МАНГА (свои тексты — читает, а не смотрит)
    # ────────────────────────────────

    "manga": {

        # 📋 Добавил в «Запланированное»
        "planned": [
            "📚 {n} добавил мангу *{title}* в список «прочитаю как-нибудь». Не факт.",
            "🗂️ *{title}* записана в очередь. Полки ломятся, {n} не останавливается.",
            "📌 {n} запланировал *{title}*. Главы сами себя не прочитают.",
            "🧠 Манга *{title}* теперь в списке {n}. До прочтения — бесконечность.",
            "🔖 *{title}* зафиксирована. {n} снова расширяет свои непрочитанные владения.",
            "📥 Хоп — *{title}* в planned. Сколько глав? Неважно. Прочитаю. Когда-нибудь.",
        ],

        # ▶️ Начал читать
        "watching": [
            "📖 {n} открыл мангу *{title}*. Поехали, глава за главой.",
            "🎌 {n} приступил к чтению *{title}*. Спать, видимо, не скоро.",
            "👁️ *{title}* в руках {n}. Ждём отчёта с полей.",
            "📜 {n} начал читать *{title}*. Надеемся, глав там хватит.",
            "🚀 Старт! *{title}* — новая манга в арсенале {n}.",
            "😤 {n} не устоял и взялся за *{title}*. Конца и края не видно, но кого это останавливало.",
        ],

        # 🔁 Перечитывает
        "rewatching": [
            "🔁 {n} перечитывает *{title}*. Значит, оно того стоило.",
            "♻️ *{title}* снова открыта. {n} возвращается за второй дозой.",
            "🌀 Повторный заход на мангу *{title}*. Хороший знак.",
            "📚 {n} листает *{title}* по второму кругу. Некоторые детали проявляются только так.",
            "🔂 *{title}* на перечитке у {n}. Привязанность подтверждена.",
            "👏 {n} снова с *{title}* в руках. Уважаю преданность.",
        ],

        # 💀 Бросил
        "dropped": [
            "🗑️ Манга *{title}* — дропнута. {n} не пощадил.",
            "💀 {n} закрыл *{title}* и больше не открывал. Всё.",
            "🚪 *{title}* осталась недочитанной. {n} ушёл без объяснений.",
            "❌ *{title}* — в архив. Минус одна манга в этом суровом мире.",
            "😤 {n} дал *{title}* шанс. Манга не оценила. Итог — дроп.",
            "🏳️ *{title}* не выдержала испытания {n}. Бывает с лучшими.",
        ],

        # ✅ Завершил без оценки
        "completed_no_score": [
            "✅ {n} дочитал мангу *{title}*. Молчит. Обрабатывает.",
            "🏁 *{title}* — прочитано. {n} ставит точку без комментариев.",
            "👀 Финальная глава *{title}* перевёрнута. Мнение {n} — тайна.",
            "📚 {n} прошёл *{title}* до конца. Оценка засекречена.",
            "🎌 *{title}* прочитана. {n} не спешит раскрываться.",
            "🤐 Дочитал и молчит. *{title}* явно оставила след.",
        ],

        # ⭐ Оценка 1–3
        "completed_score_low": [
            "💩 Манга *{title}* — {score}/10. {n} дочитал из принципа. Терпеливый человек.",
            "😭 {score}/10 за *{title}*. Жертва времени принесена. Ради чего?",
            "🤮 *{title}* получает {score}/10. {n} явно не в восторге.",
            "⚰️ {score}/10 — *{title}* похоронена в памяти {n}.",
            "🧟 {n} пережил *{title}* ({score}/10). Медаль за стойкость.",
            "🔥 *{title}* — {score}/10. Сожжено, забыто, не рекомендуется.",
        ],

        # 😐 Оценка 4–6
        "completed_score_mid": [
            "😐 *{title}* — {score}/10. Среднячок. {n} не потрясён.",
            "🫤 {score}/10 за мангу *{title}*. Прочитал. Закрыл. Пошёл дальше.",
            "🤷 {n} поставил *{title}* {score}/10. Бывало лучше, бывало хуже.",
            "📊 *{title}* — {score}/10. В целом норм, но без огня.",
            "🌫️ {score}/10 — *{title}* прошла мимо сердца {n}.",
            "😶 Прочитал. Оценил. {score}/10. *{title}* не изменила мировоззрение.",
        ],

        # 🌟 Оценка 7–9
        "completed_score_high": [
            "🌟 Манга *{title}* — {score}/10! {n} доволен. Художник постарался.",
            "🔥 {score}/10 за *{title}*! {n} явно не разочарован.",
            "👏 *{title}* — {score}/10 от {n}. Достойное чтиво.",
            "✨ {score}/10 — *{title}* зацепила {n} за живое.",
            "🎉 {score}/10 за *{title}*. Рекомендую всем любителям хорошей манги.",
            "💫 *{title}* — {score}/10. Редкий случай, когда {n} доволен.",
        ],

        # 👑 Оценка 10
        "completed_score_perfect": [
            "👑 *{title}* — ДЕСЯТКА! {n} нашёл новый шедевр манги. Запишите.",
            "🏆 10/10! *{title}* — в пантеоне {n} навсегда.",
            "💎 {n} поставил манге *{title}* десятку. Художник может гордиться.",
            "🌌 10/10 за *{title}*. {n} дочитал и сидит в тишине. Это говорит всё.",
            "🎌 Максимум! *{title}* — теперь часть {n}. Прямо в душу.",
            "🔮 *{title}* получает священную десятку. {n} не шутит.",
        ],

    },  # конец "manga"

    # ────────────────────────────────
    #  ОБЩИЕ — изменение оценки (для аниме и манги одинаково)
    # ────────────────────────────────
    "score_changed": [
        "🔄 {n} пересмотрел оценку *{title}*: было {old}, стало {new}. Что-то изменилось.",
        "🤔 *{title}* переоценено: {old} → {new}. {n} явно что-то переосмыслил.",
        "🏹 {old} → {new} за *{title}*. {n} дал второй шанс (или отобрал).",
        "⚖️ Весы справедливости скорректированы: *{title}* теперь {new}/10 вместо {old}.",
        "✏️ {n} исправил оценку *{title}* с {old} на {new}. Бывает, мнения меняются.",
        "📊 Обновление рейтинга: *{title}* {old} → {new}. {n} не стоит на месте.",
    ],
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


def save_seen_ids(seen_ids: set[int]) -> None:
    """Сохраняем виденные ID в JSON-файл."""
    Path(SEEN_IDS_FILE).write_text(
        json.dumps({"seen_ids": list(seen_ids)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
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
    """Сохраняем подписчиков в JSON."""
    Path(SUBS_FILE).write_text(
        json.dumps({"subscribers": {str(k): v for k, v in subs.items()}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
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
    MANGA_KINDS = {"manga", "manhwa", "manhua", "novel", "ranobe", "one_shot", "doujin"}
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




def extract_score_change(description: str) -> tuple[int, int] | None:
    """
    Парсим «изменена оценка с X на Y» → возвращаем (old, new).
    Если не распознали — None.
    """
    match = re.search(
        r"изменена\s+оценка\s+с\s+(\d+)\s+на\s+(\d+)",
        description, re.IGNORECASE
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
    # Основной русский формат: «оценено на 9»
    match = re.search(r"оценено\s+на\s+(\d+)", description, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Альтернативный русский: «выставил/выставила оценку 9»
    match = re.search(r"(?:выставил|выставила)\s+оценку\s+(\d+)", description, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Английский: «rated 7» или «scored 7»
    match = re.search(r"(?:rated?|score[d]?)\s+(\d+)", description, re.IGNORECASE)
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
    desc = description.lower()

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

    # Название тайтла — предпочитаем русское
    target = entry.get("target") or {}
    title_ru = target.get("russian") or ""
    title_en = target.get("name") or "???"
    title = title_ru if title_ru else title_en

    description = entry.get("description", "") or ""
    event_type = classify_event(description)

    score = None
    old_score = None
    new_score = None

    if event_type == "score_changed":
        # Изменение оценки — берём шаблон из общего банка, не из anime/manga
        change = extract_score_change(description)
        old_score, new_score = change if change else (None, None)
        key = "score_changed"
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

    # Временна́я метка события
    # Ссылка на тайтл — target.url приходит как "/animes/123-name" или "/mangas/456-name"
    target_url = (target.get("url") or "").strip()
    if target_url:
        full_url = f"{SHIKI_BASE_URL}{target_url}"
        text += f"\n🔗 [Открыть на Shikimori]({full_url})"

    created_at = entry.get("created_at", "")
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            text += f"\n_🕐 {dt.strftime('%d.%m.%Y %H:%M')} UTC_"
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
                parse_mode=ParseMode.MARKDOWN,
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
      — бот молча запоминает все текущие ID из истории
      — сообщения НЕ отправляются (не спамим историей за последние месяцы)
      — с этого момента бот следит только за НОВЫМИ событиями
    """
    seen_ids = load_seen_ids()
    log.info(
        "Бот запущен. Отображаемое имя: %s | Подписчиков: %d | Виденных ID: %d | Интервал: %d сек.",
        DISPLAY_NAME, len(load_subscribers()), len(seen_ids), CHECK_INTERVAL,
    )

    if not seen_ids:
        log.info("Первый запуск — инициализируем список ID без отправки сообщений.")
        async with aiohttp.ClientSession() as session:
            entries = await fetch_history(session)
        seen_ids = {e["id"] for e in entries}
        save_seen_ids(seen_ids)
        log.info(
            "Инициализировано %d ID. Следим только за новыми событиями.",
            len(seen_ids),
        )

    while True:
        log.info("Проверяем историю...")
        seen_ids = await check_and_notify(bot, seen_ids)
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
    lines = [f"👥 Подписчиков: *{count}*", ""]
    for i, (cid, uname) in enumerate(subs.items(), 1):
        lines.append(f"{i}. {uname} (`{cid}`)")
    sep = "\n"
    await message.answer(sep.join(lines), parse_mode=ParseMode.MARKDOWN)




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
    title = title_ru if title_ru else title_en

    status = item.get("_status", "")
    # Иконка в зависимости от статуса
    icon = {
        "watching":   "▶️",
        "rewatching": "🔁",
    }.get(status, "•")

    url = target.get("url", "")
    if url:
        return f"{icon} [{title}]({SHIKI_BASE_URL}{url})"
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
        lines.append("🎌 *Сейчас смотрит:*")
        for item in anime_list:
            lines.append(format_rate_entry(item, "anime"))

    if manga_list:
        if lines:
            lines.append("")  # пустая строка-разделитель
        lines.append("📚 *Сейчас читает:*")
        for item in manga_list:
            lines.append(format_rate_entry(item, "manga"))

    if not lines:
        await message.answer(
            f"😴 {DISPLAY_NAME} сейчас ничего не смотрит и не читает. Подозрительно."
        )
        return

    sep = "\n"
    await message.answer(sep.join(lines), parse_mode=ParseMode.MARKDOWN)

async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher()

    # Регистрируем команды
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_stop,  Command("stop"))
    dp.message.register(cmd_subs,   Command("subs"))
    dp.message.register(cmd_status, Command("status"))

    # Устанавливаем описания команд — появятся в меню "/" в Telegram
    await bot.set_my_commands([
        BotCommand(command="start",  description="Подписаться на уведомления 🥳"),
        BotCommand(command="stop",   description="Отписаться 😢"),
        BotCommand(command="status", description="Что сейчас смотрит и читает Костя 👀"),
    ])

    # polling_loop работает параллельно как фоновая задача
    asyncio.create_task(polling_loop(bot))

    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
