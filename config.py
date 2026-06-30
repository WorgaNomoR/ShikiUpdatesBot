# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Конфигурация ShikiUpdatesBot.

Нижний уровень: грузит локальный .env, читает окружение, задаёт пути к данным
и настраивает логирование на импорте. Ничего не импортирует из проекта —
зависимости строго односторонние (как healthcheck.py): остальные модули тянут
константы и логгер отсюда.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Грузим локальный .env (если есть) ДО любого чтения окружения. override=False:
# на docker-compose / хостингах переменные уже в окружении — их не перетираем.
load_dotenv()


def _required_env(name: str, hint: str = "") -> str:
    """Обязательная переменная окружения — внятный fast-fail вместо голого KeyError."""
    value = (os.environ.get(name) or "").strip()
    if not value:
        suffix = f" — {hint}" if hint else ""
        raise RuntimeError(f"Не задана обязательная переменная окружения {name}{suffix}.")
    return value


def _int_env(name: str, default: int) -> int:
    """Целочисленная переменная окружения: дефолт при отсутствии, понятная ошибка при мусоре."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(
            f"Переменная окружения {name}={raw!r} должна быть целым числом."
        ) from e


# ─────────────────────────────────────────────
#  НАСТРОЙКИ (из окружения / .env)
# ─────────────────────────────────────────────
BOT_TOKEN = _required_env("BOT_TOKEN", "токен от @BotFather")
OWNER_ID  = int(_required_env("OWNER_ID", "твой Telegram ID от @userinfobot"))

SHIKI_USER     = _required_env("SHIKI_USER", "ник на Shikimori, напр. WNR")
SHIKI_BASE_URL = (os.environ.get("SHIKI_BASE_URL") or "https://shikimori.io").strip()

# Отображаемое имя в сообщениях. Опционально через env DISPLAY_NAME;
# по умолчанию — ник профиля (SHIKI_USER). Пустая строка/пробелы → фолбэк.
DISPLAY_NAME   = os.environ.get("DISPLAY_NAME", "").strip() or SHIKI_USER

CHECK_INTERVAL        = _int_env("CHECK_INTERVAL", 15 * 60)          # проверка истории, сек (15 мин)
ERROR_NOTIFY_INTERVAL = _int_env("ERROR_NOTIFY_INTERVAL", 30 * 60)   # антиспам уведомлений об ошибке
FULL_SYNC_INTERVAL    = _int_env("FULL_SYNC_INTERVAL", 6 * 60 * 60)  # пересинк stats_all в цикле (6 ч)
WEEKLY_BACKUP_INTERVAL = 7 * 24 * 60 * 60  # еженедельный авто-бэкап состояния (по last_backup_at)

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
