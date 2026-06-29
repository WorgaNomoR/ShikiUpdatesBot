"""
Конфигурация ShikiUpdatesBot.

Нижний уровень: читает окружение, задаёт пути к данным и настраивает
логирование на импорте. Ничего не импортирует из проекта — зависимости
строго односторонние (как healthcheck.py): остальные модули тянут константы
и логгер отсюда.
"""

import logging
import os
from pathlib import Path

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
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
