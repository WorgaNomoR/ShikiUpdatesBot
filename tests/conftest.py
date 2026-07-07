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
