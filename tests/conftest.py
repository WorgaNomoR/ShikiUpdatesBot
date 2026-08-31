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
os.environ.pop("DISPLAY_NAME", None)
os.environ.pop("DISPLAY_NAME_GENDER", None)

# Уникальная папка данных на тестовую сессию: внешнее DATA_DIR не должно
# направить тесты в реальное состояние бота. Объект живёт до завершения Python
# и затем удаляет созданный временный каталог.
_test_data_dir = tempfile.TemporaryDirectory(prefix="shikibot_test_data_")
os.environ["DATA_DIR"] = _test_data_dir.name



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
    from request_budget import RollingBudget
    monkeypatch.setattr(shiki_api, "_MIN_GAP", 0)
    shiki_api._throttle_lock = None
    shiki_api._last_request_at = 0.0
    shiki_api._request_attempt_budget = RollingBudget(
        shiki_api._REQUEST_ATTEMPT_LIMIT,
        shiki_api._REQUEST_ATTEMPT_PERIOD,
    )


# Общая фикстура редиректа состояния в tmp_path: используют и
# test_backup.py (ядро), и test_handlers_backup.py (хендлеры /backup).
@pytest.fixture
def backup_env(tmp_path, monkeypatch):
    """Редиректим пути состояния в tmp_path, чтобы тесты не трогали /data."""
    import fact_bank
    import stats
    import storage
    data = tmp_path / "data"
    quarters = data / "quarters"
    quarters.mkdir(parents=True)
    monkeypatch.setattr("backup.DATA_DIR", data)
    original_facts_file = fact_bank.FACTS_FILE
    monkeypatch.setattr(fact_bank, "FACTS_FILE", data / "facts.json")
    monkeypatch.setattr(storage, "SUBS_FILE", data / "subscribers.json")
    monkeypatch.setattr(storage, "BLOCKED_USERS_FILE", data / "blocked_users.json")
    monkeypatch.setattr(storage, "STATS_CURRENT_FILE", data / "stats_current.json")
    monkeypatch.setattr(storage, "STATS_ALL_FILE", data / "stats_all.json")
    monkeypatch.setattr(storage, "SEEN_IDS_FILE", data / "seen_ids.json")
    monkeypatch.setattr(storage, "SEEN_FAVS_FILE", data / "seen_favourites.json")
    monkeypatch.setattr(storage, "UPDATE_STATE_FILE", data / "update_state.json")
    monkeypatch.setattr(stats, "QUARTERS_DIR", quarters)
    monkeypatch.setattr("handlers.OWNER_ID", 999)
    monkeypatch.setattr("backup.OWNER_ID", 999)
    monkeypatch.setattr(storage, "OWNER_ID", 999)
    fact_bank.reload_fact_bank()
    yield data
    monkeypatch.setattr(fact_bank, "FACTS_FILE", original_facts_file)
    fact_bank.reload_fact_bank()


@pytest.fixture
def fact_bank_env(tmp_path, monkeypatch):
    """Изолировать facts.json и process-local snapshot для focused-тестов."""
    import fact_bank

    original_facts_file = fact_bank.FACTS_FILE
    facts_file = tmp_path / "facts.json"
    monkeypatch.setattr(fact_bank, "FACTS_FILE", facts_file)
    fact_bank.reload_fact_bank()
    yield facts_file
    monkeypatch.setattr(fact_bank, "FACTS_FILE", original_facts_file)
    fact_bank.reload_fact_bank()
