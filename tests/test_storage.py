# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import json

import pytest

import storage
from storage import (
    load_seen_ids,
    load_subscribers,
    save_seen_ids,
    save_subscribers,
)


def test_load_seen_ids_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr("storage.SEEN_IDS_FILE", str(tmp_path / "missing.json"))

    assert load_seen_ids() == set()


def test_load_seen_ids_valid_json(monkeypatch, tmp_path):
    file = tmp_path / "seen_ids.json"

    file.write_text(
        json.dumps({"seen_ids": [1, 2, 3]}),
        encoding="utf-8",
    )

    monkeypatch.setattr("storage.SEEN_IDS_FILE", str(file))

    assert load_seen_ids() == {1, 2, 3}


def test_load_seen_ids_corrupted_json(monkeypatch, tmp_path):
    file = tmp_path / "seen_ids.json"

    file.write_text("{", encoding="utf-8")

    monkeypatch.setattr("storage.SEEN_IDS_FILE", str(file))

    assert load_seen_ids() == set()


def test_save_seen_ids(monkeypatch, tmp_path):
    file = tmp_path / "seen_ids.json"

    monkeypatch.setattr("storage.SEEN_IDS_FILE", str(file))

    save_seen_ids({1, 2, 3})

    data = json.loads(file.read_text(encoding="utf-8"))

    assert set(data["seen_ids"]) == {1, 2, 3}


def test_seen_ids_roundtrip(monkeypatch, tmp_path):
    file = tmp_path / "seen_ids.json"

    monkeypatch.setattr("storage.SEEN_IDS_FILE", str(file))

    original = {10, 20, 30}

    save_seen_ids(original)

    loaded = load_seen_ids()

    assert loaded == original


def test_load_subscribers_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr("storage.SUBS_FILE", str(tmp_path / "missing.json"))

    assert load_subscribers() == {}


def test_load_subscribers_valid_json(monkeypatch, tmp_path):
    file = tmp_path / "subs.json"

    file.write_text(
        json.dumps(
            {
                "subscribers": {
                    "123": "Alice",
                    "456": "Bob",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("storage.SUBS_FILE", str(file))

    assert load_subscribers() == {
        123: "Alice",
        456: "Bob",
    }


def test_load_subscribers_corrupted_json(monkeypatch, tmp_path):
    file = tmp_path / "subs.json"

    file.write_text("{", encoding="utf-8")

    monkeypatch.setattr("storage.SUBS_FILE", str(file))

    assert load_subscribers() == {}


def test_load_subscribers_non_int_key_falls_back_to_empty(monkeypatch, tmp_path):
    # ключ подписчика не приводится к int -> ValueError -> пустой список,
    # а не падение (ветка except ValueError)
    file = tmp_path / "subs.json"
    file.write_text(json.dumps({"subscribers": {"abc": "X"}}), encoding="utf-8")
    monkeypatch.setattr("storage.SUBS_FILE", str(file))

    assert load_subscribers() == {}


def test_save_subscribers(monkeypatch, tmp_path):
    file = tmp_path / "subs.json"

    monkeypatch.setattr("storage.SUBS_FILE", str(file))

    save_subscribers(
        {
            123: "Alice",
            456: "Bob",
        }
    )

    data = json.loads(file.read_text(encoding="utf-8"))

    assert data["subscribers"] == {
        "123": "Alice",
        "456": "Bob",
    }


def test_subscribers_roundtrip(monkeypatch, tmp_path):
    file = tmp_path / "subs.json"

    monkeypatch.setattr("storage.SUBS_FILE", str(file))

    original = {
        111: "Alice",
        222: "Bob",
    }

    save_subscribers(original)

    loaded = load_subscribers()

    assert loaded == original


def test_save_seen_ids_removes_tmp_file(monkeypatch, tmp_path):
    file = tmp_path / "seen_ids.json"

    monkeypatch.setattr("storage.SEEN_IDS_FILE", str(file))

    save_seen_ids({1})

    assert file.exists()
    assert not (tmp_path / "seen_ids.json.tmp").exists()


def test_atomic_write_creates_parent_directory(tmp_path):
    from storage import _atomic_write

    target = tmp_path / "nested" / "folder" / "file.json"

    _atomic_write(target, '{"ok": true}')

    assert target.exists()
    assert target.read_text(encoding="utf-8") == '{"ok": true}'


def test_atomic_write_overwrites_existing_file(tmp_path):
    from storage import _atomic_write

    target = tmp_path / "data.json"

    target.write_text("old", encoding="utf-8")

    _atomic_write(target, "new")

    assert target.read_text(encoding="utf-8") == "new"


# ═══════════════════════════════════════════════════════════════════
#  stats_all.json — загрузка/сохранение + in-memory кэш
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _reset_stats_all_cache():
    """Сбрасываем модульный кэш stats_all между тестами (изоляция)."""
    storage._stats_all_cache = None
    storage._stats_all_cache_ts = 0.0
    yield
    storage._stats_all_cache = None
    storage._stats_all_cache_ts = 0.0


def _valid_stats_all() -> dict:
    return {
        "updated_at": "2026-01-01T00:00:00",
        "anime": {"titles": {"1": {"score": 9}}, "aggregates": {}},
        "manga": {"titles": {}, "aggregates": {}},
        "favourites": {"anime": [], "manga": [], "ranobe": [],
                       "characters": [], "people": []},
    }


def test_load_stats_all_reads_valid_file(monkeypatch, tmp_path):
    f = tmp_path / "stats_all.json"
    payload = _valid_stats_all()
    f.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(storage, "STATS_ALL_FILE", f)

    assert storage.load_stats_all() == payload


def test_load_stats_all_bad_structure_returns_empty(monkeypatch, tmp_path):
    # dict без обязательных anime/manga -> сброс на пустую структуру
    f = tmp_path / "stats_all.json"
    f.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
    monkeypatch.setattr(storage, "STATS_ALL_FILE", f)

    data = storage.load_stats_all()
    assert data == storage._empty_stats_all()


def test_load_stats_all_non_dict_returns_empty(monkeypatch, tmp_path):
    f = tmp_path / "stats_all.json"
    f.write_text(json.dumps([1, 2, 3]), encoding="utf-8")   # список, не dict
    monkeypatch.setattr(storage, "STATS_ALL_FILE", f)

    assert storage.load_stats_all() == storage._empty_stats_all()


def test_load_stats_all_corrupted_json_returns_empty(monkeypatch, tmp_path):
    f = tmp_path / "stats_all.json"
    f.write_text("{ battered", encoding="utf-8")
    monkeypatch.setattr(storage, "STATS_ALL_FILE", f)

    assert storage.load_stats_all() == storage._empty_stats_all()


def test_load_stats_all_cache_hit_skips_file_reread(monkeypatch, tmp_path):
    """В пределах TTL повторный load возвращает ТОТ ЖЕ объект, файл не перечитывается."""
    f = tmp_path / "stats_all.json"
    f.write_text(json.dumps(_valid_stats_all()), encoding="utf-8")
    monkeypatch.setattr(storage, "STATS_ALL_FILE", f)

    first = storage.load_stats_all()
    f.write_text(json.dumps({"anime": {}, "manga": {}, "changed": True}), encoding="utf-8")
    second = storage.load_stats_all()          # кэш ещё свежий
    assert second is first                       # тот же объект, файл проигнорирован


def test_load_stats_all_cache_expired_rereads_file(monkeypatch, tmp_path):
    f = tmp_path / "stats_all.json"
    f.write_text(json.dumps(_valid_stats_all()), encoding="utf-8")
    monkeypatch.setattr(storage, "STATS_ALL_FILE", f)

    first = storage.load_stats_all()
    storage._stats_all_cache_ts = 0.0            # состариваем кэш -> age > TTL
    updated = {"anime": {"titles": {}}, "manga": {"titles": {}}, "v": 2}
    f.write_text(json.dumps(updated), encoding="utf-8")
    second = storage.load_stats_all()
    assert second == updated and second is not first


def test_load_stats_all_use_cache_false_bypasses_cache(monkeypatch, tmp_path):
    f = tmp_path / "stats_all.json"
    f.write_text(json.dumps(_valid_stats_all()), encoding="utf-8")
    monkeypatch.setattr(storage, "STATS_ALL_FILE", f)

    storage.load_stats_all()                     # заполнили кэш
    updated = {"anime": {}, "manga": {}, "v": 3}
    f.write_text(json.dumps(updated), encoding="utf-8")
    assert storage.load_stats_all(use_cache=False) == updated   # кэш обойдён


def test_save_stats_all_writes_file_and_updates_cache(monkeypatch, tmp_path):
    f = tmp_path / "stats_all.json"
    monkeypatch.setattr(storage, "STATS_ALL_FILE", f)

    data = _valid_stats_all()
    original_updated_at = data["updated_at"]
    storage.save_stats_all(data)

    on_disk = json.loads(f.read_text(encoding="utf-8"))
    assert on_disk["updated_at"] != original_updated_at   # штамп времени реально обновлён
    assert on_disk["anime"] == data["anime"]
    # кэш обновлён тем же объектом -> следующий load отдаёт его без чтения файла
    assert storage.load_stats_all() is data


# ═══════════════════════════════════════════════════════════════════
#  stats_current.json — бэкофиллы и первый запуск
# ═══════════════════════════════════════════════════════════════════

def test_load_stats_current_backfills_tracking_since_from_period_start(monkeypatch, tmp_path):
    """Старый файл без tracking_since -> подставляем period_start."""
    f = tmp_path / "stats_current.json"
    f.write_text(json.dumps({
        "period": "2026-Q2",
        "period_start": "2026-04-01T00:00:00",
        "last_report_sent": None,
        "events": [],
    }), encoding="utf-8")
    monkeypatch.setattr(storage, "STATS_CURRENT_FILE", f)

    data = storage.load_stats_current()
    assert data["tracking_since"] == "2026-04-01T00:00:00"
    assert data["last_backup_at"] is None          # заодно бэкофилл last_backup_at


def test_load_stats_current_backfills_tracking_since_defaults_to_quarter(monkeypatch, tmp_path):
    """Нет ни tracking_since, ни period_start -> календарное начало квартала."""
    from utils import quarter_start
    f = tmp_path / "stats_current.json"
    f.write_text(json.dumps({"period": "2026-Q2", "events": []}), encoding="utf-8")
    monkeypatch.setattr(storage, "STATS_CURRENT_FILE", f)

    data = storage.load_stats_current()
    assert data["tracking_since"] == quarter_start().isoformat()


def test_load_stats_current_bad_structure_creates_fresh(monkeypatch, tmp_path):
    """dict без обязательных period/events -> сброс: создаётся и сохраняется свежий."""
    f = tmp_path / "stats_current.json"
    f.write_text(json.dumps({"nonsense": 1}), encoding="utf-8")
    monkeypatch.setattr(storage, "STATS_CURRENT_FILE", f)

    data = storage.load_stats_current()
    assert "period" in data and data["events"] == []
    # свежий сразу записан на диск (перезаписал битую структуру)
    on_disk = json.loads(f.read_text(encoding="utf-8"))
    assert on_disk["period"] == data["period"]


def test_load_stats_current_corrupted_json_creates_fresh(monkeypatch, tmp_path):
    f = tmp_path / "stats_current.json"
    f.write_text("{ broken", encoding="utf-8")
    monkeypatch.setattr(storage, "STATS_CURRENT_FILE", f)

    data = storage.load_stats_current()
    assert "period" in data and "events" in data
    assert f.exists()                               # свежий сохранён


def test_load_stats_current_first_run_creates_and_saves(monkeypatch, tmp_path):
    """Файла нет вовсе -> первый запуск: свежий квартал с tracking_since, записан."""
    f = tmp_path / "stats_current.json"
    monkeypatch.setattr(storage, "STATS_CURRENT_FILE", f)

    data = storage.load_stats_current()
    assert f.exists()                               # файл создан
    assert data["tracking_since"] is not None
    assert data["events"] == []
    # tracking_since = max(начало квартала, сейчас): не раньше начала квартала
    from utils import quarter_start
    assert data["tracking_since"] >= quarter_start().isoformat()


# ── save_* глотают ошибки записи (сбой диска не роняет вызывающий флоу) ──

def test_save_stats_all_swallows_write_error(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "STATS_ALL_FILE", tmp_path / "stats_all.json")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(storage, "_atomic_write", boom)
    # не должно пробросить исключение наверх
    storage.save_stats_all(_valid_stats_all())


def test_save_stats_current_swallows_write_error(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "STATS_CURRENT_FILE", tmp_path / "stats_current.json")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(storage, "_atomic_write", boom)
    storage.save_stats_current({"period": "2026-Q2", "events": []})
