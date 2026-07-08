# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import os
import sys
import tempfile
from pathlib import Path

import dotenv
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Тесты не читают локальный .env разработчика — иначе config.load_dotenv()
# подтянет его переменные (напр. DISPLAY_NAME) и сделает тесты недетерминированными.
# CI без .env этим не страдал, локальная разработка — да.
def _no_dotenv(*args, **kwargs):
    return False


dotenv.load_dotenv = _no_dotenv

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("SHIKI_USER", "WNR")

# Изолированная папка данных — чтобы тесты не лезли в реальный /data
_test_data_dir = Path(tempfile.gettempdir()) / "shikibot_test_data"
_test_data_dir.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATA_DIR", str(_test_data_dir))



@pytest.fixture(autouse=True)
def _fast_boot(monkeypatch):
    """boot-throttle: обнуляем стартовые паузы, чтобы тесты не ждали реальные секунды."""
    import handlers
    monkeypatch.setattr(handlers, "BOOT_PHASE_DELAY", 0)


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """shiki_api throttle: min-gap→0 + сброс лока/метки на каждый тест, чтобы
    (1) тесты не спали реальные 0.25 с между запросами и (2) asyncio.Lock не
    утекал между функциональными event-loop'ами pytest-asyncio. Выделенные
    тесты троттла сами возвращают _MIN_GAP и гоняют фейковые часы."""
    import shiki_api
    monkeypatch.setattr(shiki_api, "_MIN_GAP", 0)
    shiki_api._throttle_lock = None
    shiki_api._last_request_at = 0.0


# Общая фикстура редиректа состояния в tmp_path: используют и
# test_backup.py (ядро), и test_handlers_backup.py (хендлеры /backup).
@pytest.fixture
def backup_env(tmp_path, monkeypatch):
    """Редиректим пути состояния в tmp_path, чтобы тесты не трогали /data."""
    import stats
    import storage
    data = tmp_path / "data"
    quarters = data / "quarters"
    quarters.mkdir(parents=True)
    monkeypatch.setattr("backup.DATA_DIR", data)
    monkeypatch.setattr(storage, "SUBS_FILE", data / "subscribers.json")
    monkeypatch.setattr(storage, "STATS_CURRENT_FILE", data / "stats_current.json")
    monkeypatch.setattr(storage, "STATS_ALL_FILE", data / "stats_all.json")
    monkeypatch.setattr(storage, "SEEN_IDS_FILE", data / "seen_ids.json")
    monkeypatch.setattr(storage, "SEEN_FAVS_FILE", data / "seen_favourites.json")
    monkeypatch.setattr(stats, "QUARTERS_DIR", quarters)
    monkeypatch.setattr("handlers.OWNER_ID", 999)
    monkeypatch.setattr("backup.OWNER_ID", 999)
    return data
