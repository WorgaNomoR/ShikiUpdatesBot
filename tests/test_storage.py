# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import asyncio
import json

import pytest

import storage
from storage import (
    BlockedUsersMutationError,
    BlockedUsersStateError,
    add_blocked_user,
    load_blocked_users,
    load_seen_ids,
    load_subscribers,
    reconcile_blocked_subscribers,
    save_blocked_users,
    save_seen_ids,
    save_subscribers,
    subscribers_from_payload,
)


@pytest.fixture
def user_registry_env(monkeypatch, tmp_path):
    """Изолировать реестр, настройку alerts и OWNER_ID."""
    known_users_path = tmp_path / "known_users.json"
    user_alerts_path = tmp_path / "user_alerts.json"
    monkeypatch.setattr(storage, "KNOWN_USERS_FILE", known_users_path)
    monkeypatch.setattr(storage, "USER_ALERTS_FILE", user_alerts_path)
    monkeypatch.setattr(storage, "OWNER_ID", 999)
    return known_users_path, user_alerts_path


def _known_user(
    user_id: int,
    name: str = "Neo",
    username: str | None = "the_one",
    first_seen_at: str = "2026-09-03T10:20:30Z",
) -> storage.KnownUser:
    return storage.KnownUser(user_id, name, username, first_seen_at)


def test_user_registry_missing_files_use_migration_defaults(user_registry_env):
    assert storage.load_known_users() == {}
    assert storage.list_known_users() == ()
    assert storage.known_user_count() == 0
    assert storage.load_user_alerts_enabled() is True


def test_user_registry_roundtrip_and_queries_are_strict_and_sorted(user_registry_env):
    known_users_path, _user_alerts_path = user_registry_env
    users = {
        30: _known_user(30, "Trinity", None, "2026-09-03T10:20:31Z"),
        10: _known_user(10),
    }

    storage.save_known_users(users)

    assert storage.load_known_users() == users
    assert storage.get_known_user(30) == users[30]
    assert storage.get_known_user(20) is None
    assert storage.known_user_count() == 2
    assert [user.user_id for user in storage.list_known_users()] == [10, 30]
    assert list(json.loads(known_users_path.read_text(encoding="utf-8"))["users"]) == [
        "10",
        "30",
    ]


def test_save_known_users_rejects_key_record_identity_mismatch(user_registry_env):
    with pytest.raises(storage.KnownUsersStateError):
        storage.save_known_users({10: _known_user(20)})


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"users": []},
        {"users": {"01": {"display_name": "Neo", "username": None, "first_seen_at": "2026-09-03T10:20:30Z"}}},
        {"users": {"10": {"display_name": "", "username": None, "first_seen_at": "2026-09-03T10:20:30Z"}}},
        {"users": {"10": {"display_name": "Neo", "username": 7, "first_seen_at": "2026-09-03T10:20:30Z"}}},
        {"users": {"10": {"display_name": "Neo", "username": None, "first_seen_at": "2026-09-03T10:20:30"}}},
        {"users": {"10": {"display_name": "Neo", "username": None, "first_seen_at": "2026-09-03T10:20:30Z", "extra": True}}},
    ],
)
def test_load_known_users_rejects_malformed_existing_state(
    user_registry_env,
    payload,
):
    known_users_path, _user_alerts_path = user_registry_env
    known_users_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(storage.KnownUsersStateError):
        storage.load_known_users()

    assert json.loads(known_users_path.read_text(encoding="utf-8")) == payload


def test_load_known_users_converts_invalid_utf8_to_state_error(user_registry_env):
    known_users_path, _user_alerts_path = user_registry_env
    original = b"\xff"
    known_users_path.write_bytes(original)

    with pytest.raises(storage.KnownUsersStateError):
        storage.load_known_users()

    assert known_users_path.read_bytes() == original


@pytest.mark.parametrize("payload", [{}, {"enabled": 1}, {"enabled": True, "x": 1}])
def test_load_user_alerts_rejects_malformed_existing_state(
    user_registry_env,
    payload,
):
    _known_users_path, user_alerts_path = user_registry_env
    user_alerts_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(storage.UserAlertsStateError):
        storage.load_user_alerts_enabled()

    assert json.loads(user_alerts_path.read_text(encoding="utf-8")) == payload


def test_load_user_alerts_converts_invalid_utf8_to_state_error(user_registry_env):
    _known_users_path, user_alerts_path = user_registry_env
    original = b"\xff"
    user_alerts_path.write_bytes(original)

    with pytest.raises(storage.UserAlertsStateError):
        storage.load_user_alerts_enabled()

    assert user_alerts_path.read_bytes() == original


@pytest.mark.asyncio
async def test_register_known_user_preserves_first_identity_and_timestamp(
    user_registry_env,
):
    first = await storage.register_known_user(
        10,
        "Neo",
        "the_one",
        first_seen_at="2026-09-03T10:20:30Z",
    )
    repeated = await storage.register_known_user(
        10,
        "Thomas Anderson",
        "changed",
        first_seen_at="2026-09-03T10:21:30Z",
    )

    assert first == storage.KnownUserRegistration(
        _known_user(10),
        created=True,
        should_alert=True,
    )
    assert repeated == storage.KnownUserRegistration(
        _known_user(10),
        created=False,
        should_alert=False,
    )
    assert storage.load_known_users() == {10: _known_user(10)}


@pytest.mark.asyncio
async def test_register_known_user_rejects_noncanonical_explicit_timestamp(
    user_registry_env,
):
    with pytest.raises(storage.KnownUsersStateError):
        await storage.register_known_user(10, "Neo", None, first_seen_at="")

    assert storage.load_known_users() == {}


@pytest.mark.asyncio
async def test_concurrent_registration_creates_one_record_and_one_alert_decision(
    user_registry_env,
):
    results = await asyncio.gather(
        *(
            storage.register_known_user(
                10,
                "Neo",
                "the_one",
                first_seen_at="2026-09-03T10:20:30Z",
            )
            for _ in range(12)
        )
    )

    assert sum(result.created for result in results) == 1
    assert sum(result.should_alert for result in results) == 1
    assert storage.known_user_count() == 1


@pytest.mark.asyncio
async def test_disabled_alerts_register_without_backlog_replay(user_registry_env):
    assert await storage.set_user_alerts_enabled(False) is True
    disabled = await storage.register_known_user(
        10,
        "Neo",
        None,
        first_seen_at="2026-09-03T10:20:30Z",
    )
    assert disabled.created is True
    assert disabled.should_alert is False

    assert await storage.set_user_alerts_enabled(True) is True
    repeated = await storage.register_known_user(
        10,
        "Neo",
        None,
        first_seen_at="2026-09-03T10:21:30Z",
    )
    assert repeated.created is False
    assert repeated.should_alert is False


@pytest.mark.asyncio
async def test_malformed_alert_settings_suppress_alert_but_keep_registration(
    user_registry_env,
):
    _known_users_path, user_alerts_path = user_registry_env
    original = '{"enabled": "broken"}'
    user_alerts_path.write_text(original, encoding="utf-8")

    result = await storage.register_known_user(
        10,
        "Neo",
        None,
        first_seen_at="2026-09-03T10:20:30Z",
    )

    assert result.created is True
    assert result.should_alert is False
    assert storage.known_user_count() == 1
    assert user_alerts_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_setting_is_idempotent_and_malformed_state_is_not_overwritten(
    user_registry_env,
):
    _known_users_path, user_alerts_path = user_registry_env
    assert await storage.set_user_alerts_enabled(True) is False
    assert not user_alerts_path.exists()
    assert await storage.set_user_alerts_enabled(False) is True
    assert await storage.set_user_alerts_enabled(False) is False
    assert storage.load_user_alerts_enabled() is False

    original = '{"enabled": null}'
    user_alerts_path.write_text(original, encoding="utf-8")
    with pytest.raises(storage.UserAlertsStateError):
        await storage.set_user_alerts_enabled(True)
    assert user_alerts_path.read_text(encoding="utf-8") == original


@pytest.fixture
def access_state_env(monkeypatch, tmp_path):
    """Изолировать список блокировок, subscribers и OWNER_ID."""
    blocked_path = tmp_path / "blocked_users.json"
    subscribers_path = tmp_path / "subscribers.json"
    monkeypatch.setattr(storage, "BLOCKED_USERS_FILE", blocked_path)
    monkeypatch.setattr(storage, "SUBS_FILE", subscribers_path)
    monkeypatch.setattr(storage, "OWNER_ID", 999)
    return blocked_path, subscribers_path


def test_load_blocked_users_missing_file_migrates_to_empty(access_state_env):
    assert load_blocked_users() == set()


def test_blocked_users_roundtrip_is_canonical(access_state_env):
    blocked_path, _subscribers_path = access_state_env

    save_blocked_users({30, 10, 20})

    assert load_blocked_users() == {10, 20, 30}
    assert json.loads(blocked_path.read_text(encoding="utf-8")) == {
        "blocked_user_ids": [10, 20, 30]
    }


@pytest.mark.parametrize(
    "payload",
    [
        "{broken",
        json.dumps([]),
        json.dumps({"blocked_user_ids": "1"}),
        json.dumps({"blocked_user_ids": [1, 1]}),
        json.dumps({"blocked_user_ids": [True]}),
        json.dumps({"blocked_user_ids": [-1]}),
        json.dumps({"blocked_user_ids": [999]}),
        json.dumps({"blocked_user_ids": [], "extra": 1}),
    ],
)
def test_corrupted_blocked_users_state_never_degrades_to_empty(
    access_state_env,
    payload,
):
    blocked_path, _subscribers_path = access_state_env
    blocked_path.write_text(payload, encoding="utf-8")

    with pytest.raises(BlockedUsersStateError):
        load_blocked_users()


def test_owner_id_is_rejected_by_storage_save_and_check(access_state_env):
    blocked_path, _subscribers_path = access_state_env

    with pytest.raises(BlockedUsersStateError):
        save_blocked_users({storage.OWNER_ID})

    blocked_path.write_text("{broken", encoding="utf-8")
    assert storage.is_user_blocked(storage.OWNER_ID) is False


@pytest.mark.asyncio
async def test_block_atomically_removes_private_subscriber_and_unblock_does_not_restore(
    access_state_env,
):
    storage.save_subscribers({77: "Unknown", 88: "Other"})

    assert await add_blocked_user(77) == (True, True)
    assert load_blocked_users() == {77}
    assert storage.load_subscribers() == {88: "Other"}

    assert await add_blocked_user(77) == (False, False)
    assert await storage.remove_blocked_user(77) is True
    assert await storage.remove_blocked_user(77) is False
    assert storage.load_subscribers() == {88: "Other"}


@pytest.mark.asyncio
async def test_block_rejects_owner_before_any_write(access_state_env):
    blocked_path, subscribers_path = access_state_env

    with pytest.raises(ValueError, match="OWNER_ID"):
        await add_blocked_user(storage.OWNER_ID)
    with pytest.raises(ValueError, match="OWNER_ID"):
        await storage.remove_blocked_user(storage.OWNER_ID)

    assert not blocked_path.exists()
    assert not subscribers_path.exists()


@pytest.mark.asyncio
async def test_concurrent_blocked_users_mutations_do_not_lose_ids(access_state_env):
    await asyncio.gather(*(add_blocked_user(user_id) for user_id in range(1, 51)))

    assert load_blocked_users() == set(range(1, 51))


@pytest.mark.asyncio
async def test_block_rolls_back_blocked_users_when_subscriber_publication_fails(
    access_state_env,
    monkeypatch,
):
    blocked_path, subscribers_path = access_state_env
    save_blocked_users({10})
    storage.save_subscribers({77: "Target", 88: "Other"})
    original_atomic_write = storage._atomic_write

    def fail_subscribers(path, data):
        if storage.Path(path) == subscribers_path:
            raise OSError("disk failure")
        original_atomic_write(path, data)

    monkeypatch.setattr(storage, "_atomic_write", fail_subscribers)

    with pytest.raises(BlockedUsersMutationError, match="исходное состояние"):
        await add_blocked_user(77)

    assert load_blocked_users() == {10}
    assert storage.load_subscribers() == {77: "Target", 88: "Other"}


@pytest.mark.asyncio
async def test_startup_reconciliation_repairs_interrupted_block_publication(
    access_state_env,
):
    save_blocked_users({77, 99})
    save_subscribers({77: "Target", 88: "Other"})

    assert await reconcile_blocked_subscribers() == {77}
    assert load_blocked_users() == {77, 99}
    assert load_subscribers() == {88: "Other"}
    assert await reconcile_blocked_subscribers() == set()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"subscribers": None},
        {"subscribers": {"abc": "Name"}},
    ],
)
def test_subscribers_payload_validation_rejects_malformed_state(payload):
    with pytest.raises(ValueError):
        subscribers_from_payload(payload)


@pytest.mark.asyncio
async def test_startup_reconciliation_converts_subscriber_read_failure(
    access_state_env,
    monkeypatch,
):
    _blocked_path, subscribers_path = access_state_env
    save_blocked_users({77})
    save_subscribers({77: "Target"})
    original_read_text = storage.Path.read_text

    def fail_subscriber_read(path, *args, **kwargs):
        if path == subscribers_path:
            raise OSError("read failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(storage.Path, "read_text", fail_subscriber_read)

    with pytest.raises(BlockedUsersStateError):
        await reconcile_blocked_subscribers()


@pytest.mark.asyncio
async def test_startup_reconciliation_rejects_null_subscribers(access_state_env):
    _blocked_path, subscribers_path = access_state_env
    save_blocked_users({77})
    subscribers_path.write_text('{"subscribers": null}', encoding="utf-8")

    with pytest.raises(BlockedUsersStateError):
        await reconcile_blocked_subscribers()


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


def test_load_subscribers_read_error_falls_back_to_empty(monkeypatch, tmp_path):
    file = tmp_path / "subs.json"
    file.write_text('{"subscribers": {}}', encoding="utf-8")
    monkeypatch.setattr("storage.SUBS_FILE", str(file))

    def fail_read(*args, **kwargs):
        raise OSError("read failure")

    monkeypatch.setattr(storage.Path, "read_text", fail_read)

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
    storage._stats_all_cache_state = storage.STATS_ALL_MISSING
    yield
    storage._stats_all_cache = None
    storage._stats_all_cache_ts = 0.0
    storage._stats_all_cache_state = storage.STATS_ALL_MISSING


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


def test_load_stats_all_snapshot_distinguishes_missing_invalid_and_valid(
    monkeypatch,
    tmp_path,
):
    stats_file = tmp_path / "stats_all.json"
    monkeypatch.setattr(storage, "STATS_ALL_FILE", stats_file)

    missing = storage.load_stats_all_snapshot(use_cache=False)
    assert missing.state == storage.STATS_ALL_MISSING
    assert missing.data == storage._empty_stats_all()

    stats_file.write_text("{broken", encoding="utf-8")
    invalid = storage.load_stats_all_snapshot(use_cache=False)
    assert invalid.state == storage.STATS_ALL_INVALID
    assert invalid.data == storage._empty_stats_all()

    payload = _valid_stats_all()
    stats_file.write_text(json.dumps(payload), encoding="utf-8")
    valid = storage.load_stats_all_snapshot(use_cache=False)
    assert valid.state == storage.STATS_ALL_VALID
    assert valid.data == payload

    stats_file.write_text(json.dumps({"anime": {}}), encoding="utf-8")
    structural = storage.load_stats_all_snapshot(use_cache=False)
    assert structural.state == storage.STATS_ALL_INVALID
    assert structural.data == storage._empty_stats_all()
    cached = storage.load_stats_all_snapshot()
    assert cached.state == storage.STATS_ALL_INVALID
    assert cached.data is structural.data


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
    snapshot = storage.load_stats_all_snapshot()
    assert snapshot.state == storage.STATS_ALL_VALID
    assert snapshot.data is data


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
    assert data["pending_quarter_delivery"] is None


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


# ── update_state.json ──

def test_update_state_roundtrip(monkeypatch, tmp_path):
    path = tmp_path / "update_state.json"
    monkeypatch.setattr(storage, "UPDATE_STATE_FILE", path)
    expected = {
        "last_checked_at": "2026-08-05T12:00:00+00:00",
        "latest_main_version": "v1.3.0",
        "latest_version": "v1.2.0",
        "release_url": "https://example.test/release",
        "last_notified_version": None,
    }
    storage.save_update_state(expected)
    assert storage.load_update_state() == expected


def test_load_update_state_backfills_main_version(monkeypatch, tmp_path):
    path = tmp_path / "update_state.json"
    path.write_text(json.dumps({
        "last_checked_at": "2026-08-05T12:00:00+00:00",
        "latest_version": "v1.2.0",
        "release_url": "https://example.test/release",
        "last_notified_version": "v1.2.0",
    }), encoding="utf-8")
    monkeypatch.setattr(storage, "UPDATE_STATE_FILE", path)

    state = storage.load_update_state()

    assert state["latest_main_version"] is None
    assert state["latest_version"] == "v1.2.0"


def test_load_update_state_bad_json_returns_defaults(monkeypatch, tmp_path):
    path = tmp_path / "update_state.json"
    storage._atomic_write(path, "{broken")
    monkeypatch.setattr(storage, "UPDATE_STATE_FILE", path)
    assert storage.load_update_state() == storage._empty_update_state()
