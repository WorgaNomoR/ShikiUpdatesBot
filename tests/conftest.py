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
def coalescing_journal_factory(journal_factory):
    """Новая порция с соседней парой на границе секунды и квартала."""
    def factory(count=2, *, long_title=False):
        journal = journal_factory(count=count)
        first, last = journal["events"][:2]
        first.update(event_type="planned", score=None, created_at="2026-03-31T23:59:59.990+00:00", event_at="2026-03-31T23:59:59.990000+00:00")
        last.update(target_id=first["target_id"], created_at="2026-04-01T00:00:00.010+00:00", event_at="2026-04-01T00:00:00.010000+00:00")
        if long_title:
            last["title"].update(name="😀<&>" * 2500, url="/animes/11")
        return journal

    return factory


@pytest.fixture
def coalescing_factory(coalescing_journal_factory):
    """Plan v3 с явно связанными источниками, включая много частей completion."""
    from unittest.mock import patch

    from catchup_digest import (
        render_digest,
        render_ordinary_entries,
    )
    from messages import (
        build_history_digest_heading,
        build_message,
        history_entry_from_event,
    )
    from notification_outbox import (
        enqueue,
        migrate_outbox,
        notification_entries,
        prepare_digest,
    )

    def factory(*, ready=True, audience=1, long_title=False, count=10):
        journal = migrate_outbox(coalescing_journal_factory(count=count, long_title=long_title), 0)
        entries = notification_entries(journal["events"])
        def ordinary(event):
            return build_message(history_entry_from_event(event), normalized=event)
        presentation = "ordinary" if len(entries) == 1 else "digest"
        with patch("messages.random.choice", side_effect=lambda bank: bank[0]):
            parts = render_ordinary_entries(entries, ordinary=ordinary) if presentation == "ordinary" else render_digest(
                journal["events"], ordinary=ordinary, heading=build_history_digest_heading(), entries=entries,
            )
        journal = prepare_digest(journal, parts, {10 + i: "b" * 32 for i in range(audience)}, 1800000000.0, entries=entries, presentation=presentation)
        if ready:
            for event in journal["events"]:
                journal = enqueue(journal, event, None, {}, 1800000000.0)
        return journal

    return factory


@pytest.fixture(params=["digest", "coalesced"])
def frozen_history_factory(request, digest_factory, coalescing_factory):
    """Одна матрица archive/reserve contract для старого и нового frozen plan."""
    return coalescing_factory if request.param == "coalesced" else digest_factory


@pytest.fixture(params=["ordinary", "coalesced"])
def notification_batch_factory(request, journal_factory, coalescing_journal_factory):
    """Одна матрица crash/delivery для независимых записей и связанной пары."""
    return coalescing_journal_factory if request.param == "coalesced" else journal_factory


@pytest.fixture
def digest_factory(journal_factory):
    """Штатный prepared/ready plan; I/O и projection проверяются у их владельцев."""
    from catchup_digest import render_digest
    from messages import (
        build_history_digest_heading,
        build_message,
        history_entry_from_event,
    )
    from notification_outbox import (
        enqueue,
        migrate_outbox,
        prepare_digest,
    )

    def factory(*, ready=True, audience=1, long_title=False, count=10):
        journal = migrate_outbox(journal_factory(count=count), 0)
        if long_title:
            journal["events"][0]["title"].update(name="😀<&>" * 2500, url="/animes/11")
        from unittest.mock import patch

        with patch("messages.random.choice", side_effect=lambda bank: bank[0]):
            parts = render_digest(journal["events"], ordinary=lambda event: build_message(history_entry_from_event(event), normalized=event), heading=build_history_digest_heading())
        journal = prepare_digest(journal, parts, {10 + i: "b" * 32 for i in range(audience)}, 1800000000.0)
        if ready:
            for event in journal["events"]:
                journal = enqueue(journal, event, None, {}, 1800000000.0)
        return journal

    return factory


@pytest.fixture
def legacy_digest_factory(digest_factory):
    """Опубликованный v1: десять известных событий и отдельный unknown между ними."""
    from copy import deepcopy

    from notification_outbox import validate_outbox

    def factory(*, ready=True):
        journal = digest_factory(ready=ready, count=11)
        journal["events"][4]["event_type"] = "unknown"
        plan = journal["outbox"]["plans"][0]
        template = plan["units"][0]
        plan["version"] = 1
        plan["units"] = []
        for index, refs, kind, text in (
            (0, plan["events"][:4], "digest", "<b>История профиля</b>\nСтарый текст: первые четыре события."),
            (1, plan["events"][4:5], "ordinary", "🤔 Старое неизвестное действие &amp; пояснение."),
            (2, plan["events"][5:], "digest", "<b>История профиля</b>\nСтарый текст: последние шесть событий."),
        ):
            unit = deepcopy(template)
            unit.update(unit_id=f"{plan['plan_id']}:{index}", seq=refs[0][0], kind=kind, events=refs)
            unit["payload"]["text"] = text
            plan["units"].append(unit)
        validate_outbox(journal)
        return journal

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
def source_index_factory(journal_factory):
    """Большой v1 индекс и небольшой suffix: реальный reducer без O(n²)."""
    from event_time_stats import (
        compact_source_history,
        ensure_event_time,
        project_event,
    )
    from notification_outbox import (
        enqueue,
        migrate_outbox,
    )
    from source_history import (
        content_hash,
        semantic_hash,
    )

    def factory(prefix=4100, suffix=2, version=1, facts=True):
        journal = journal_factory(count=prefix + suffix, processed=prefix)
        for ev in journal["events"][:prefix]:
            seq = ev["seq"]
            ev["history_id"] = -seq if seq % 2 else 10**40 + seq
            ev["event_type"] = "ignored"
        if facts:
            for ev in journal["events"][:3]:
                ev.update(event_type="completed", target_id="11")
            journal["events"][1].update(event_type="score_removed", score=None)
            journal["events"][2].update(event_at=None, created_at=None, time_quality="missing")
        original = journal["events"][:prefix]
        cur = {
            "period": "2026-Q2", "events": [], "pending_quarter_delivery": None,
            "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0},
        }
        ensure_event_time(cur)
        for ev in journal["events"]:
            if ev["event_type"] != "ignored":
                project_event(cur, journal, ev["seq"])
            cur["event_projection"]["applied_seq"] = ev["seq"]
        journal = migrate_outbox(journal, prefix)
        for ev in journal["events"][prefix:]:
            journal = enqueue(journal, ev, "frozen index suffix", {10: "b" * 32}, 1000)
        journal = compact_source_history(journal, cur, prefix)
        if version == 1:
            base = journal["source_base"]
            base["version"] = 1
            base["ids"] = [[ev["history_id"], semantic_hash(ev)] for ev in original]
            base["checksum"] = content_hash({k: v for k, v in base.items() if k != "checksum"})
        return journal, cur

    return factory


@pytest.fixture
def stats_capacity_factory(journal_factory):
    """Компактный recovery-набор разных тайтлов, построенный штатным reducer."""
    from event_journal_schema import validate_recovery_set
    from event_time_stats import (
        compact_source_history,
        ensure_event_time,
        project_event,
    )
    from notification_outbox import migrate_outbox

    def factory(count=20):
        journal = journal_factory(count=count, processed=count)
        cur = {
            "period": "2026-Q1", "events": [], "last_report_sent": None,
            "pending_quarter_delivery": None,
            "period_start": "2026-01-01T00:00:00+00:00",
            "tracking_since": "2026-01-01T00:00:00+00:00",
            "event_projection": {
                "journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0,
            },
        }
        ensure_event_time(cur)
        # Один полный reducer-проход даёт ту же проекцию без квадратичной подготовки.
        project_event(cur, journal, count)
        cur["event_projection"]["applied_seq"] = count
        journal = migrate_outbox(journal, count)
        journal = compact_source_history(journal, cur, count)
        validate_recovery_set(journal, cur)
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
