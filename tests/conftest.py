# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import os
import tempfile

import dotenv
import pytest


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
def _isolated_history_state(tmp_path, monkeypatch):
    """Новые авторитетные файлы не должны переходить между тестами."""
    monkeypatch.setattr("storage.EVENT_JOURNAL_FILE", tmp_path / "event_journal.json")
    monkeypatch.setattr("storage.SEEN_IDS_FILE", tmp_path / "seen_ids.json")
    monkeypatch.setattr("storage.STATS_CURRENT_FILE", tmp_path / "stats_current.json")


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
    monkeypatch.setattr(storage, "KNOWN_USERS_FILE", data / "known_users.json")
    monkeypatch.setattr(storage, "USER_ALERTS_FILE", data / "user_alerts.json")
    monkeypatch.setattr(storage, "STATS_CURRENT_FILE", data / "stats_current.json")
    monkeypatch.setattr(storage, "STATS_ALL_FILE", data / "stats_all.json")
    monkeypatch.setattr(storage, "SEEN_IDS_FILE", data / "seen_ids.json")
    monkeypatch.setattr(storage, "EVENT_JOURNAL_FILE", data / "event_journal.json")
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


@pytest.fixture
def journal_factory():
    """Валидный recovery-набор; матрица нормализации остаётся в test_messages."""
    from messages import normalize_history_event

    def factory(count=1, processed=0):
        events = []
        for seq in range(1, count + 1):
            event = normalize_history_event({
                "id": seq + 1,
                "created_at": "2026-04-01T02:00:00+03:00",
                "description": "Просмотрено и оценено на 8",
                "target": {"id": seq + 10, "kind": "tv", "name": f"Title {seq}"},
            }, "2026-04-02T00:00:00+00:00")
            event["seq"] = seq
            events.append(event)
        return {
            "version": 1, "journal_id": "a" * 32, "profile": "WNR",
            "normalization_version": 1, "baseline_initialized": True,
            "baseline_ids": [1], "events": events, "processed_seq": processed,
        }

    return factory


@pytest.fixture
def outbox_capacity_factory(journal_factory):
    """Повторяемый контроль ёмкости штатным logical serializer, без I/O."""
    from event_journal_schema import journal_json
    from notification_outbox import (
        enqueue,
        migrate_outbox,
    )
    from notification_progress_schema import compact_json

    def factory(audience=7000, count=1):
        journal = journal_factory(count=count)
        journal["profile"] = "capacity-probe"
        for event in journal["events"]:
            padding = 494 - len(compact_json(event).encode("utf-8"))
            assert padding >= 0
            event["description"] += "x" * padding
            assert len(compact_json(event).encode("utf-8")) == 494
        journal = migrate_outbox(journal, 0)
        for event in journal["events"]:
            journal = enqueue(
                journal, event, "x" * 500,
                {100000000 + i: "b" * 32 for i in range(audience)}, 1000,
            )
        journal_json(journal)
        return journal

    return factory


@pytest.fixture
def source_history_factory(journal_factory):
    """Реальный reducer и full outbox: общий recovery-набор для storage/import."""
    from event_time_stats import (
        ensure_event_time,
        index_event_periods,
        project_event,
    )
    from notification_outbox import (
        enqueue,
        migrate_outbox,
        retain_outbox,
    )

    def factory(count=8, pending_from=None, padding=2000, duplicate_titles=True):
        journal = migrate_outbox(journal_factory(count=count), 0)
        cur = {
            "period": "2026-Q2", "events": [], "pending_quarter_delivery": None,
            "tracking_since": "2026-04-01T00:00:00",
            "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0},
        }
        ensure_event_time(cur)
        index = index_event_periods(cur, journal)
        for event in journal["events"]:
            event["description"] += "x" * padding
            if duplicate_titles:
                event["target_id"] = "11"
            project_event(cur, journal, event["seq"], period_events=index)
            cur["event_projection"]["applied_seq"] = event["seq"]
            memberships = {10: "b" * 32} if pending_from is not None and event["seq"] >= pending_from else {}
            journal = enqueue(journal, event, "frozen payload", memberships, 1000)
        journal = retain_outbox(journal, limit=count)
        return journal, cur

    return factory


@pytest.fixture
def acquisition_factory(journal_factory):
    """Recovery-набор с отдельными staged seq и неизменной принятой baseline."""
    def factory():
        journal = journal_factory(count=0)
        journal.update(version=2, catchup={
            "phase": "tail", "page": 2, "frontier": [2, 3], "head_ids": [2, 3],
            "staged": journal_factory(count=2)["events"], "spanning": True,
        })
        return journal

    return factory
