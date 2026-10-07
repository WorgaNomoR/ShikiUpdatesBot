# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import asyncio
import json
import os
from copy import deepcopy
from uuid import uuid4

import pytest

import storage
from report_asset_ids import (
    REPORT_POSTER_PLACEHOLDER_MEDIA,
    REPORT_POSTER_PLACEHOLDER_V1_MEDIA,
)
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
    original = blocked_path.read_bytes()

    with pytest.raises(BlockedUsersStateError):
        load_blocked_users()

    assert blocked_path.read_bytes() == original


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
    original_open = storage.Path.open

    def fail_subscriber_read(path, *args, **kwargs):
        if path == subscribers_path:
            raise OSError("read failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(storage.Path, "open", fail_subscriber_read)

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


@pytest.fixture(params=["history", "favourites"])
def seen_cache_loader(request, monkeypatch, tmp_path):
    """Изолировать оба кеша и сохранить их разные типы идентификаторов."""
    if request.param == "history":
        key, setting, loader, valid_id = (
            "seen_ids", "SEEN_IDS_FILE", storage.load_seen_ids, 17,
        )
    else:
        key, setting, loader, valid_id = (
            "seen_favourites", "SEEN_FAVS_FILE", storage.load_seen_favourites,
            "anime_17",
        )
    path = tmp_path / f"{key}.json"
    monkeypatch.setattr(f"storage.{setting}", path)
    return path, key, loader, valid_id


@pytest.mark.parametrize("payload", [None, False, 0, "seen", []])
def test_seen_cache_rejects_non_object_payload(seen_cache_loader, payload, caplog):
    path, _key, loader, _valid_id = seen_cache_loader
    raw = json.dumps(payload).encode("utf-8")
    path.write_bytes(raw)

    assert loader() == set()
    assert path.read_bytes() == raw
    assert "Не удалось прочитать" in caplog.text


@pytest.mark.parametrize(
    "value", [None, False, 0, "12", {}, [None], [False], [1.5], [{}], [[]]],
)
def test_seen_cache_rejects_invalid_id_collection(seen_cache_loader, value, caplog):
    path, key, loader, _valid_id = seen_cache_loader
    raw = json.dumps({key: value}).encode("utf-8")
    path.write_bytes(raw)

    assert loader() == set()
    assert path.read_bytes() == raw
    assert "Не удалось прочитать" in caplog.text


def test_seen_cache_rejects_whole_list_with_wrong_id_type(seen_cache_loader, caplog):
    path, key, loader, valid_id = seen_cache_loader
    invalid_id = str(valid_id) if isinstance(valid_id, int) else 17
    raw = json.dumps({key: [valid_id, invalid_id]}).encode("utf-8")
    path.write_bytes(raw)

    assert loader() == set()
    assert path.read_bytes() == raw
    assert "Не удалось прочитать" in caplog.text


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"\xff\xfe", id="invalid-utf8"),
        pytest.param(b"[" * 5000 + b"]" * 5000, id="deep-json"),
    ],
)
def test_seen_cache_contains_decode_failures(seen_cache_loader, raw, caplog):
    path, _key, loader, _valid_id = seen_cache_loader
    path.write_bytes(raw)

    assert loader() == set()
    assert path.read_bytes() == raw
    assert "Не удалось прочитать" in caplog.text


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("read failed")])
def test_seen_cache_contains_read_failures(seen_cache_loader, monkeypatch, error, caplog):
    path, key, loader, valid_id = seen_cache_loader
    raw = json.dumps({key: [valid_id]}).encode("utf-8")
    path.write_bytes(raw)

    def fail_read(*args, **kwargs):
        raise error

    monkeypatch.setattr("storage.Path.read_text", fail_read)

    assert loader() == set()
    assert path.read_bytes() == raw
    assert "Не удалось прочитать" in caplog.text


@pytest.mark.parametrize("with_key", [False, True])
def test_seen_cache_preserves_empty_compatibility(seen_cache_loader, with_key, caplog):
    path, key, loader, _valid_id = seen_cache_loader
    raw = json.dumps({key: []} if with_key else {}).encode("utf-8")
    path.write_bytes(raw)

    assert loader() == set()
    assert path.read_bytes() == raw
    assert "Не удалось прочитать" not in caplog.text


def test_seen_cache_preserves_valid_duplicates(seen_cache_loader, caplog):
    path, key, loader, valid_id = seen_cache_loader
    raw = json.dumps({key: [valid_id, valid_id]}).encode("utf-8")
    path.write_bytes(raw)

    assert loader() == {valid_id}
    assert path.read_bytes() == raw
    assert "Не удалось прочитать" not in caplog.text


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


def test_load_subscribers_strict_accepts_missing_legacy_and_current_state(
    monkeypatch,
    tmp_path,
):
    file = tmp_path / "subs.json"
    monkeypatch.setattr(storage, "SUBS_FILE", file)

    assert storage.load_subscribers_strict() == {}

    legacy = json.dumps({"subscribers": {"1": "One", "-100": "Channel"}})
    file.write_text(legacy, encoding="utf-8")
    before = file.read_bytes()
    assert storage.load_subscribers_strict() == {1: "One", -100: "Channel"}
    assert file.read_bytes() == before

    storage.save_subscriber_state(storage.SubscriberState(
        {2: "Two"},
        {
            "version": 1,
            "last_backup_at": None,
            "weekly_started_at": 100.0,
            "pending": None,
        },
    ))
    assert storage.load_subscribers_strict() == {2: "Two"}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"subscribers": []},
        {"subscribers": {"abc": "Name"}},
        {"subscribers": {"01": "Name"}},
        {"subscribers": {"+1": "Name"}},
        {"subscribers": {" 1": "Name"}},
        {"subscribers": {"0": "Name"}},
        {"subscribers": {str(2**63): "Name"}},
        {"subscribers": {str(-(2**63) - 1): "Name"}},
        {"subscribers": {"1": 7}},
    ],
)
def test_load_subscribers_strict_rejects_malformed_or_lossy_state(
    monkeypatch,
    tmp_path,
    payload,
):
    file = tmp_path / "subs.json"
    file.write_text(json.dumps(payload), encoding="utf-8")
    before = file.read_bytes()
    monkeypatch.setattr(storage, "SUBS_FILE", file)

    with pytest.raises(storage.SubscribersStateError):
        storage.load_subscribers_strict()

    assert file.read_bytes() == before


def test_load_subscribers_strict_rejects_invalid_utf8(monkeypatch, tmp_path):
    file = tmp_path / "subs.json"
    file.write_bytes(b"\xff")
    monkeypatch.setattr(storage, "SUBS_FILE", file)

    with pytest.raises(storage.SubscribersStateError):
        storage.load_subscribers_strict()

    assert file.read_bytes() == b"\xff"


def test_load_subscribers_strict_rejects_read_error(monkeypatch, tmp_path):
    file = tmp_path / "subs.json"
    file.write_text('{"subscribers": {}}', encoding="utf-8")
    monkeypatch.setattr(storage, "SUBS_FILE", file)

    def fail_read(*_args, **_kwargs):
        raise OSError("read failure")

    monkeypatch.setattr(storage.Path, "open", fail_read)

    with pytest.raises(storage.SubscribersStateError):
        storage.load_subscribers_strict()


@pytest.mark.asyncio
async def test_notification_membership_migration_and_mutations_preserve_backup_contract(backup_env):
    storage.SUBS_FILE.write_text('{"subscribers":{"10":"legacy","-100":"group"}}', encoding="utf-8")
    async with storage.restorable_state_transaction():
        first = storage.notification_memberships()
        second = storage.notification_memberships()
    assert first == second
    assert set(first) == {10, -100}
    assert storage.load_subscriber_state().backup_schedule["pending"] is None
    storage.save_subscribers({10: "rename", -100: "group"})
    assert storage.load_subscriber_state().notification_memberships == first
    await storage.mutate_subscription(10, "rename", subscribed=False)
    await storage.mutate_subscription(10, "rename", subscribed=True)
    current = storage.load_subscriber_state()
    assert current.notification_memberships[10] != first[10]
    assert current.notification_memberships[-100] == first[-100]
    assert current.backup_schedule["pending"]["subscriptions"] == 1
    assert current.backup_schedule["pending"]["unsubscriptions"] == 1


@pytest.mark.parametrize("damage", ["unsupported", "missing", "token", "duplicate", "oversized"])
def test_notification_memberships_corruption_preserves_bytes(backup_env, monkeypatch, damage):
    storage.save_subscribers({10: "keep"})
    payload = json.loads(storage.SUBS_FILE.read_text(encoding="utf-8"))
    if damage == "unsupported":
        payload["notification_memberships"]["version"] = 2
    elif damage == "missing":
        payload["notification_memberships"]["tokens"] = {}
    elif damage == "token":
        payload["notification_memberships"]["tokens"]["10"] = True
    if damage == "duplicate":
        raw = b'{"subscribers":{"10":"keep","10":"replace"}}'
    else:
        raw = json.dumps(payload).encode()
    storage.SUBS_FILE.write_bytes(raw)
    if damage == "oversized":
        monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", len(raw) - 1)
    with pytest.raises(storage.SubscribersStateError):
        storage.load_subscribers_strict()
    with pytest.raises(ValueError):
        storage.notification_memberships()
    assert storage.SUBS_FILE.read_bytes() == raw


@pytest.mark.parametrize("has_memberships", [False, True])
def test_membership_migration_preserves_legacy_weekly_anchor(
    backup_env, monkeypatch, has_memberships
):
    now = 1800000000.0
    anchor = now - 7 * 24 * 60 * 60 - 100
    monkeypatch.setattr("storage.time.time", lambda: now)
    storage.save_stats_current(
        {"period": "2026-Q2", "events": [], "last_backup_at": anchor}, strict=True
    )
    payload = {"subscribers": {"10": "legacy"}}
    if has_memberships:
        payload["notification_memberships"] = {"version": 1, "tokens": {"10": "a" * 32}}
    storage.SUBS_FILE.write_text(json.dumps(payload), encoding="utf-8")
    memberships = storage.notification_memberships()
    current = storage.load_subscriber_state(strict_subscribers=True)
    assert current.notification_memberships == memberships
    assert current.schedule_missing is False
    assert current.backup_schedule["weekly_started_at"] == anchor
    assert current.backup_schedule["last_backup_at"] is None
    assert current.backup_schedule["pending"] is None


@pytest.mark.parametrize("label", [True, None, {}])
def test_notification_membership_publication_rejects_invalid_labels(backup_env, label):
    storage.save_subscribers({10: "keep"})
    before = storage.SUBS_FILE.read_bytes()
    state = storage.load_subscriber_state(strict_subscribers=True)
    state.subscribers[10] = label
    with pytest.raises(ValueError):
        storage.save_subscriber_state(state)
    assert storage.SUBS_FILE.read_bytes() == before


def test_outbox_capacity_includes_all_remaining_progress(backup_env, journal_factory, monkeypatch):
    from event_journal_schema import journal_json
    from notification_outbox import (
        enqueue,
        migrate_outbox,
        progress_reserve,
    )
    journal = migrate_outbox(journal_factory(), 0)
    journal = enqueue(journal, journal["events"][0], "frozen", {10: "b" * 32}, 1000)
    limit = len(journal_json(journal).encode()) + progress_reserve(journal)
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", limit)
    storage.save_event_journal(journal)
    before = storage.EVENT_JOURNAL_FILE.read_bytes()
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", limit - 1)
    with pytest.raises(storage.EventJournalStateError, match="capacity"):
        storage.save_event_journal(journal)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == before


@pytest.mark.parametrize("phase", ["marker", "ack", "terminal"])
@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("after", [False, True])
def test_bounded_capacity_survives_every_publication_and_restart(
    backup_env, outbox_capacity_factory, monkeypatch, phase, interrupt, after,
):
    from event_journal_schema import journal_json
    from notification_outbox import (
        MAX_ATTEMPTS,
        begin_attempt,
        complete_attempt,
        finish,
    )
    journal = outbox_capacity_factory(audience=20)
    monkeypatch.setattr("storage.SHIKI_USER", journal["profile"])
    limit = len(journal_json(journal).encode()) + 441 * 20
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", limit)
    storage.save_event_journal(journal)
    history_bytes = storage.EVENT_JOURNAL_FILE.read_bytes()
    write = storage._atomic_write
    failed = False

    def publish(candidate, boundary):
        nonlocal failed
        progress = storage.notification_progress_file()
        original = progress.read_bytes()

        def fail(path, payload):
            if path == progress:
                if after:
                    write(path, payload)
                if interrupt:
                    raise KeyboardInterrupt
                raise OSError("publication")
            write(path, payload)

        if boundary == phase and not failed:
            with monkeypatch.context() as patch:
                patch.setattr("storage._atomic_write", fail)
                with pytest.raises(KeyboardInterrupt if interrupt else storage.EventJournalStateError):
                    storage.save_event_journal(candidate)
            if not after:
                assert progress.read_bytes() == original
            else:
                assert storage.load_event_journal() == candidate
            failed = True
        storage.save_event_journal(candidate)
        # Только опубликованная authority, включая cold history после restart.
        monkeypatch.setattr("storage._journal_history_cache", None)
        assert storage.load_event_journal() == candidate
        assert storage.EVENT_JOURNAL_FILE.read_bytes() == history_bytes
        return storage.load_event_journal()

    for index in range(MAX_ATTEMPTS):
        journal = storage.load_event_journal()
        recipient = journal["outbox"]["records"][0]["recipients"]["100000000"]
        now = recipient["next_attempt_at"]
        begin_attempt(recipient, now)
        journal = publish(journal, "marker")
        recipient = journal["outbox"]["records"][0]["recipients"]["100000000"]
        if index < MAX_ATTEMPTS - 1:
            complete_attempt(recipient, "uncertain", now, retry_delay=0)
            journal = publish(journal, "ack")
        else:
            finish(recipient, "expired", "attempt_budget", now)
            journal = publish(journal, "terminal")
    assert failed
    assert journal["processed_seq"] == journal["outbox"]["enqueued_seq"] == 1
    recipient = journal["outbox"]["records"][0]["recipients"]["100000000"]
    assert recipient["status"] == "expired"
    assert len(recipient["attempts"]) == MAX_ATTEMPTS
    assert all(a["outcome"] == "uncertain" for a in recipient["attempts"])


@pytest.mark.parametrize("failure", ["write", "interruption", "generation", "newer_state"])
@pytest.mark.parametrize("summarized", [False, True])
def test_compaction_publication_guards_and_exact_bytes(backup_env, journal_factory, monkeypatch, failure, summarized):
    from notification_outbox import (
        compact_outbox,
        enqueue,
        migrate_outbox,
    )

    journal = migrate_outbox(journal_factory(count=2), 0)
    journal = enqueue(journal, journal["events"][0], "empty audience", {}, 1000)
    if summarized:
        journal = compact_outbox(journal)
    storage.save_event_journal(journal)
    generation = storage.restorable_restore_generation()
    if failure == "generation":
        monkeypatch.setattr("storage._restorable_restore_generation", generation + 1)
    elif failure == "newer_state":
        current = enqueue(journal, journal["events"][1], "new pending", {10: "b" * 32}, 1000)
        storage.save_event_journal(current)
    elif failure == "write":
        monkeypatch.setattr("storage._atomic_write", lambda *a: (_ for _ in ()).throw(OSError("write")))
    else:
        monkeypatch.setattr("storage._atomic_write", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    before = storage.EVENT_JOURNAL_FILE.read_bytes()
    with pytest.raises(KeyboardInterrupt if failure == "interruption" else storage.EventJournalStateError):
        storage.compact_event_journal(journal, expected_generation=generation)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == before


def test_retention_retires_summaries_with_absolute_completion_checkpoint(backup_env, journal_factory):
    from notification_outbox import (
        compact_outbox,
        enqueue,
        migrate_outbox,
    )

    journal = migrate_outbox(journal_factory(count=3), 0)
    journal = enqueue(journal, journal["events"][0], "empty audience", {}, 1000)
    journal = enqueue(journal, journal["events"][1], "pending", {10: "b" * 32}, 1000)
    journal = compact_outbox(journal)
    storage.save_event_journal(journal)
    current = storage.compact_event_journal(
        journal, expected_generation=storage.restorable_restore_generation(),
    )
    assert current["outbox"]["records"] == journal["outbox"]["records"][1:]
    assert current["outbox"]["completed_seq"] == 1
    assert current["outbox"]["baseline_seq"] == 0
    assert current["processed_seq"] == current["outbox"]["enqueued_seq"] == 2
    assert current["events"] == journal["events"]
    assert storage.load_event_journal() == current


def test_load_subscribers_strict_rejects_malformed_present_schedule(
    monkeypatch,
    tmp_path,
):
    file = tmp_path / "subs.json"
    payload = {
        "subscribers": {"1": "One"},
        "backup_schedule": {"version": 1},
    }
    file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(storage, "SUBS_FILE", file)

    with pytest.raises(storage.SubscribersStateError):
        storage.load_subscribers_strict()


@pytest.mark.asyncio
async def test_user_directory_snapshot_waits_for_complete_restorable_write(backup_env):
    storage.save_known_users({
        1: _known_user(1, "Old", None, "2026-09-03T10:20:30Z"),
    })
    storage.save_subscribers({1: "Old"})
    storage.save_blocked_users(set())
    first_file_published = asyncio.Event()
    finish_write = asyncio.Event()

    async def publish_new_state():
        async with storage.restorable_state_transaction():
            storage.save_known_users({
                2: _known_user(2, "New", None, "2026-09-04T10:20:30Z"),
            })
            first_file_published.set()
            await finish_write.wait()
            storage.save_subscribers({2: "New"})
            storage.save_blocked_users({3})

    writer = asyncio.create_task(publish_new_state())
    await first_file_published.wait()
    reader = asyncio.create_task(storage.load_user_directory_snapshot())
    await asyncio.sleep(0)
    assert not reader.done()

    finish_write.set()
    await writer
    snapshot = await reader

    assert [user.user_id for user in snapshot.known_users] == [2]
    assert snapshot.subscribers == ((2, "New"),)
    assert snapshot.blocked_user_ids == frozenset({3})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loader_name", "error", "source"),
    [
        ("load_known_users", storage.KnownUsersStateError("broken"), "known_users"),
        ("load_subscribers_strict", storage.SubscribersStateError("broken"), "subscribers"),
        ("load_blocked_users", storage.BlockedUsersStateError("broken"), "blocked_users"),
    ],
)
async def test_user_directory_snapshot_identifies_failed_source(
    backup_env,
    monkeypatch,
    loader_name,
    error,
    source,
):
    def fail():
        raise error

    monkeypatch.setattr(storage, loader_name, fail)

    with pytest.raises(storage.UserDirectorySnapshotError) as exc_info:
        await storage.load_user_directory_snapshot()

    assert exc_info.value.source == source


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


@pytest.mark.asyncio
async def test_subscription_mutations_are_atomic_durable_and_idempotent(backup_env):
    first = await storage.mutate_subscription(111, "Alice", subscribed=True)
    repeated_start = await storage.mutate_subscription(
        111,
        "Alice renamed",
        subscribed=True,
    )
    stopped = await storage.mutate_subscription(111, "Alice", subscribed=False)
    repeated_stop = await storage.mutate_subscription(111, "Alice", subscribed=False)

    assert first == storage.SubscriptionMutation(True, 1)
    assert repeated_start == storage.SubscriptionMutation(False, 1)
    assert stopped == storage.SubscriptionMutation(True, 0)
    assert repeated_stop == storage.SubscriptionMutation(False, 0)
    state = storage.load_subscriber_state(strict_subscribers=True)
    assert state.subscribers == {}
    assert state.backup_schedule["pending"]["subscriptions"] == 1
    assert state.backup_schedule["pending"]["unsubscriptions"] == 1
    assert state.backup_schedule["pending"]["counts_known"] is True


@pytest.mark.asyncio
async def test_concurrent_subscription_mutations_do_not_lose_counts(backup_env):
    storage.save_subscribers({1: "One", 2: "Two"})

    results = await asyncio.gather(
        storage.mutate_subscription(3, "Three", subscribed=True),
        storage.mutate_subscription(4, "Four", subscribed=True),
        storage.mutate_subscription(1, "One", subscribed=False),
        storage.mutate_subscription(2, "Two", subscribed=False),
    )

    assert all(result.changed for result in results)
    state = storage.load_subscriber_state(strict_subscribers=True)
    assert state.subscribers == {3: "Three", 4: "Four"}
    assert state.backup_schedule["pending"]["subscriptions"] == 2
    assert state.backup_schedule["pending"]["unsubscriptions"] == 2


@pytest.mark.asyncio
async def test_subscription_publication_failure_keeps_previous_combined_state(
    backup_env,
    monkeypatch,
):
    storage.save_subscribers({1: "One"})
    before = storage.SUBS_FILE.read_bytes()
    monkeypatch.setattr(
        storage,
        "_atomic_write",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError):
        await storage.mutate_subscription(2, "Two", subscribed=True)

    assert storage.SUBS_FILE.read_bytes() == before


def test_legacy_subscriber_writer_preserves_backup_schedule(backup_env):
    state = storage.SubscriberState(
        {1: "One"},
        {
            "version": 1,
            "last_backup_at": 123.0,
            "weekly_started_at": 100.0,
            "pending": {
                "subscriptions": 2,
                "unsubscriptions": 1,
                "counts_known": True,
                "token": uuid4().hex,
            },
        },
    )
    storage.save_subscriber_state(state)

    storage.save_subscribers({2: "Two"})

    restored = storage.load_subscriber_state(strict_subscribers=True)
    assert restored.subscribers == {2: "Two"}
    assert restored.backup_schedule == state.backup_schedule


@pytest.mark.asyncio
async def test_user_registry_and_alert_settings_do_not_create_backup_pending(
    backup_env,
):
    state = storage.SubscriberState(
        {1: "One"},
        {
            "version": 1,
            "last_backup_at": 123.0,
            "weekly_started_at": 100.0,
            "pending": None,
        },
    )
    storage.save_subscriber_state(state)

    await storage.register_known_user(
        7,
        "Neo",
        "the_one",
        first_seen_at="2026-09-03T10:20:30Z",
    )
    await storage.set_user_alerts_enabled(False)

    assert storage.load_subscription_backup_state() == state.backup_schedule


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

def test_empty_stats_current_keeps_only_quarter_delivery_state():
    fresh = storage._empty_stats_current("2026-Q2")
    assert "last_backup_at" not in fresh
    assert fresh["pending_quarter_delivery"] is None


def test_load_stats_current_does_not_add_backup_schedule_fields(backup_env):
    storage.STATS_CURRENT_FILE.write_text(
        json.dumps(
            {
                "period": "2026-Q2",
                "period_start": "2026-04-01T00:00:00",
                "tracking_since": "2026-04-01T00:00:00",
                "last_report_sent": None,
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    data = storage.load_stats_current()
    assert "last_backup_at" not in data
    assert data["pending_quarter_delivery"] is None

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
    assert "last_backup_at" not in data
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


def _quarter_state():
    return {
        "period": "2026-Q3", "events": [],
        "pending_quarter_delivery": storage.new_quarter_delivery(
            "2026-Q2", "2026-Q3", ["first", "second"],
        ),
    }


def _rich_frozen_unit(label: str) -> dict:
    return {
        "transport": "rich",
        "content": {
            "blocks": [{"type": "paragraph", "text": label}],
            "skip_entity_detection": True,
        },
        "fallback_html": [f"<b>{label}</b>"],
        "fallback_disable_preview": False,
    }


def _quarter_state_v2():
    return {
        "period": "2026-Q3",
        "events": [],
        "pending_quarter_delivery": storage.new_quarter_delivery_plan(
            "2026-Q2",
            "2026-Q3",
            [_rich_frozen_unit("first"), _rich_frozen_unit("second")],
        ),
    }


def test_new_quarter_delivery_plan_normalizes_invalid_frozen_units():
    with pytest.raises(storage.QuarterDeliveryStateError, match="^report_units$"):
        storage.new_quarter_delivery_plan(
            "2026-Q2",
            "2026-Q3",
            [{"transport": "unknown"}],
        )


@pytest.mark.parametrize("key,value", [
    ("version", 2), ("version", True), ("version", 1.0),
    ("next_unit", -1), ("next_unit", True), ("next_unit", 0.5), ("next_unit", 3),
    ("report_messages", [None]), ("report_messages", [12]),
    ("report_messages", [""]), ("report_messages", [" \n"]),
    ("report_messages", "secret"), ("report_messages", ["\ud800"]),
    ("plan_id", "broken"), ("plan_hash", "broken"),
    ("report_messages", ["changed"]),
    ("old_period", "2026-Q3"), ("old_period", "2026-Q4"),
    ("old_period", "bad"), ("new_period", "2026-Q4"),
])
def test_quarter_pending_schema_rejects_invalid_state(key, value):
    cur = _quarter_state()
    cur["pending_quarter_delivery"][key] = value
    with pytest.raises(storage.QuarterDeliveryStateError):
        storage.validate_pending_quarter_delivery(cur)


@pytest.mark.parametrize("pending", [False, [], {}, {"report_sent": True}])
def test_quarter_pending_never_confuses_malformed_with_absent(pending):
    with pytest.raises(storage.QuarterDeliveryStateError):
        storage.validate_pending_quarter_delivery({"pending_quarter_delivery": pending})


@pytest.mark.parametrize("sent", [False, True])
def test_legacy_quarter_migration_preserves_frozen_messages(sent):
    pending = {
        "old_period": "2026-Q2", "new_period": "2026-Q3",
        "report_messages": ["first", "second"], "report_sent": sent,
    }
    assert storage.validate_pending_quarter_delivery({
        "period": "2026-Q3", "pending_quarter_delivery": pending,
    }) == pending
    migrated = storage.migrate_quarter_delivery(pending)
    assert migrated["version"] == 1
    assert migrated["next_unit"] == (2 if sent else 0)
    assert migrated["report_messages"] == ["first", "second"]
    assert "report_sent" not in migrated
    assert "version" not in pending
    assert storage.migrate_quarter_delivery(migrated) == migrated


@pytest.mark.parametrize("key,value", [("report_sent", 1), ("report_messages", [""]), ("new_period", "2026-Q4")])
def test_legacy_quarter_schema_remains_strict(key, value):
    pending = {
        "old_period": "2026-Q2", "new_period": "2026-Q3",
        "report_messages": ["frozen"], "report_sent": False,
    }
    pending[key] = value
    with pytest.raises(storage.QuarterDeliveryStateError):
        storage.validate_pending_quarter_delivery({"period": "2026-Q3", "pending_quarter_delivery": pending})


def test_quarter_plan_identity_is_unique_and_progress_does_not_change_it():
    first = _quarter_state()
    second = _quarter_state()
    assert first["pending_quarter_delivery"]["plan_id"] != second["pending_quarter_delivery"]["plan_id"]
    digest = first["pending_quarter_delivery"]["plan_hash"]
    first["pending_quarter_delivery"]["next_unit"] = 2
    assert storage.validate_pending_quarter_delivery(first)["plan_hash"] == digest
    first["period"] = "2026-Q4"
    with pytest.raises(storage.QuarterDeliveryStateError, match="period_lineage"):
        storage.validate_pending_quarter_delivery(first)


def test_version2_quarter_plan_validates_exact_transport_content_and_progress():
    cur = _quarter_state_v2()
    pending = cur["pending_quarter_delivery"]
    expected = deepcopy(pending)
    digest = pending["plan_hash"]

    assert storage.validate_pending_quarter_delivery(cur) == expected
    assert pending["version"] == 2
    assert storage.migrate_quarter_delivery(pending) == expected
    pending["next_unit"] = 1
    assert storage.validate_pending_quarter_delivery(cur)["plan_hash"] == digest


@pytest.mark.parametrize("placeholder_media", [
    REPORT_POSTER_PLACEHOLDER_V1_MEDIA,
    REPORT_POSTER_PLACEHOLDER_MEDIA,
])
def test_version2_quarter_plan_accepts_exact_versioned_local_media_reference(
    placeholder_media,
):
    media_unit = _rich_frozen_unit("media")
    media_unit["content"]["blocks"] = [{
        "type": "collage",
        "blocks": [
            {
                "type": "photo",
                "photo": {
                    "type": "photo",
                    "media": "https://cdn.example.test/first.jpg",
                },
            },
            {
                "type": "photo",
                "photo": {
                    "type": "photo",
                    "media": placeholder_media,
                },
            },
        ],
    }]
    expected_media_unit = deepcopy(media_unit)
    cur = {
        "period": "2026-Q3",
        "events": [],
        "pending_quarter_delivery": storage.new_quarter_delivery_plan(
            "2026-Q2",
            "2026-Q3",
            [media_unit],
        ),
    }
    media_unit["content"]["blocks"].clear()

    assert storage.validate_pending_quarter_delivery(cur)["report_units"] == [
        expected_media_unit
    ]


@pytest.mark.parametrize("foreign_media", [
    "attach://foreign",
    REPORT_POSTER_PLACEHOLDER_MEDIA + "-foreign",
])
def test_version2_quarter_plan_rejects_foreign_local_media_reference(foreign_media):
    cur = _quarter_state_v2()
    media_unit = cur["pending_quarter_delivery"]["report_units"][0]
    media_unit["content"]["blocks"] = [{
        "type": "collage",
        "blocks": [
            {
                "type": "photo",
                "photo": {
                    "type": "photo",
                    "media": "https://cdn.example.test/first.jpg",
                },
            },
            {
                "type": "photo",
                "photo": {
                    "type": "photo",
                    "media": foreign_media,
                },
            },
        ],
    }]

    with pytest.raises(storage.QuarterDeliveryStateError, match="^report_units$"):
        storage.validate_pending_quarter_delivery(cur)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda unit: unit.update(transport="markdown"),
        lambda unit: unit["content"].update(skip_entity_detection=False),
        lambda unit: unit["content"]["blocks"].append({
            "type": "anchor",
            "name": "<hostile>",
        }),
        lambda unit: unit.update(fallback_html=[]),
        lambda unit: unit.update(fallback_disable_preview="yes"),
    ],
)
def test_version2_quarter_plan_rejects_invalid_frozen_payload(mutate):
    cur = _quarter_state_v2()
    mutate(cur["pending_quarter_delivery"]["report_units"][0])

    with pytest.raises(storage.QuarterDeliveryStateError, match="^report_units$"):
        storage.validate_pending_quarter_delivery(cur)


def test_exact_unsupported_downgrade_replaces_only_unacknowledged_rich_units():
    cur = _quarter_state_v2()
    pending = cur["pending_quarter_delivery"]
    pending["next_unit"] = 1
    pending["report_units"][1]["fallback_html"] = [
        "<b>second-a</b>",
        "<b>second-b</b>",
    ]
    expected_acknowledged = deepcopy(pending["report_units"][0])

    downgraded = storage.downgrade_quarter_delivery(pending, 1)

    assert downgraded["plan_id"] != pending["plan_id"]
    assert downgraded["next_unit"] == 1
    assert downgraded["report_units"] == [
        expected_acknowledged,
        {
            "transport": "html",
            "content": "<b>second-a</b>",
            "disable_preview": False,
        },
        {
            "transport": "html",
            "content": "<b>second-b</b>",
            "disable_preview": False,
        },
    ]
    cur["pending_quarter_delivery"] = downgraded
    assert storage.validate_pending_quarter_delivery(cur) == downgraded


def test_unsupported_downgrade_validates_the_new_plan_before_returning():
    pending = _quarter_state_v2()["pending_quarter_delivery"]
    pending["new_period"] = "bad"
    original = deepcopy(pending)

    with pytest.raises(storage.QuarterDeliveryStateError, match="^period_format$"):
        storage.downgrade_quarter_delivery(pending, 0)

    assert pending == original


def test_unsupported_downgrade_rejects_missing_lineage_key():
    pending = _quarter_state_v2()["pending_quarter_delivery"]
    del pending["old_period"]

    with pytest.raises(
        storage.QuarterDeliveryStateError,
        match="^unsupported_downgrade$",
    ):
        storage.downgrade_quarter_delivery(pending, 0)


@pytest.mark.parametrize("legacy", [False, True])
def test_quarter_partial_plan_cannot_claim_full_report_completion(legacy):
    cur = _quarter_state()
    if legacy:
        cur["pending_quarter_delivery"] = {
            "old_period": "2026-Q2", "new_period": "2026-Q3",
            "report_messages": ["first", "second"], "report_sent": False,
        }
    cur["last_report_sent"] = "2026-Q3"
    with pytest.raises(storage.QuarterDeliveryStateError, match="premature_completion"):
        storage.validate_pending_quarter_delivery(cur)


@pytest.mark.parametrize("revisions", [{"2026-Q1": True}, {"2026-Q1": -1}, {"2026-Q3": 1}, {}])
def test_v3_quarter_plan_rejects_invalid_correction_revisions(revisions):
    with pytest.raises(storage.QuarterDeliveryStateError):
        storage.new_quarter_delivery_plan("2026-Q1", "2026-Q2", [], event_time_revisions=revisions)


def test_v3_revision_map_is_hash_bound_and_survives_downgrade():
    plan = storage.new_quarter_delivery_plan("2026-Q1", "2026-Q2", [_rich_frozen_unit("first")], event_time_revisions={"2026-Q1": 3})
    assert plan["version"] == 3
    downgraded = storage.downgrade_quarter_delivery(plan, 0)
    assert downgraded["event_time_revisions"] == plan["event_time_revisions"]
    plan["event_time_revisions"]["2026-Q1"] = 4
    with pytest.raises(storage.QuarterDeliveryStateError, match="plan_integrity"):
        storage.validate_pending_quarter_delivery({"period": "2026-Q2", "pending_quarter_delivery": plan})


def test_current_capacity_reserves_actual_final_acknowledgement(backup_env, monkeypatch):
    cur = _quarter_state()
    cur["tracking_since"] = "2026-07-01T00:00:00+00:00"
    cur["pending_quarter_delivery"]["report_messages"] *= 5
    cur["pending_quarter_delivery"]["plan_hash"] = storage._quarter_plan_hash(cur["pending_quarter_delivery"])
    storage.save_stats_current(cur, strict=True)
    original = storage.STATS_CURRENT_FILE.read_bytes()
    current_size = storage.json_publication_size(storage.stats_current_json(cur, strict=False))
    completed = json.loads(json.dumps(cur))
    completed["last_report_sent"] = completed["period"]
    completed["pending_quarter_delivery"]["next_unit"] = len(completed["pending_quarter_delivery"]["report_messages"])
    final_size = storage.json_publication_size(storage.stats_current_json(completed, strict=False))
    started = deepcopy(cur)
    started["pending_quarter_delivery"].update(next_unit=9, delivery_uncertain=False)
    boundary = max(final_size, storage.json_publication_size(storage.stats_current_json(started, strict=False)))
    assert final_size > current_size
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", current_size)
    with pytest.raises(storage.QuarterDeliveryStateError):
        storage.save_stats_current(cur, strict=True)
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", boundary)
    storage.save_stats_current(cur, strict=True)
    storage.save_stats_current(completed, strict=True)
    assert storage.load_stats_current(strict=True)["last_report_sent"] == cur["period"]


def test_strict_quarter_write_failure_preserves_previous_file(backup_env, monkeypatch, caplog):
    storage.save_stats_current(_quarter_state(), strict=True)
    original = storage.STATS_CURRENT_FILE.read_bytes()

    def fail(*args):
        raise OSError("report content must not escape")

    monkeypatch.setattr(storage, "_atomic_write", fail)
    with pytest.raises(storage.QuarterDeliveryStateError, match="^current_write$"):
        storage.save_stats_current({"other": "state"}, strict=True)
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    assert "save_stats_current: strict-запись не удалась: OSError" in caplog.text
    assert "report content must not escape" not in caplog.text


@pytest.mark.parametrize("raw,reason", [
    ('{broken', "current_read"),
    ('{"events": []}', "current_structure"),
    ('{"period": "2026-Q3", "events": false}', "current_structure"),
])
@pytest.mark.parametrize("initialize_missing", [False, True])
def test_strict_quarter_load_does_not_reset_unreadable_state(backup_env, raw, reason, initialize_missing):
    storage.STATS_CURRENT_FILE.write_text(raw, encoding="utf-8")
    with pytest.raises(storage.QuarterDeliveryStateError, match=f"^{reason}$"):
        storage.load_stats_current(strict=True, initialize_missing=initialize_missing)
    assert storage.STATS_CURRENT_FILE.read_text(encoding="utf-8") == raw


def test_strict_quarter_load_does_not_recreate_disappeared_file(backup_env):
    with pytest.raises(storage.QuarterDeliveryStateError, match="^current_missing$"):
        storage.load_stats_current(strict=True)
    assert not storage.STATS_CURRENT_FILE.exists()


@pytest.mark.parametrize("period", ["old", "2026-Q2 ", " 2026-Q2", "2026-Q0", "2026-Q5", "0000-Q1", "26-Q2", "２０２６-Q2", "2026-Q2\n"])
def test_strict_quarter_load_rejects_invalid_period_without_pending(backup_env, period):
    storage.save_stats_current({"period": period, "events": []})
    original = storage.STATS_CURRENT_FILE.read_bytes()
    with pytest.raises(storage.QuarterDeliveryStateError, match="^period_format$"):
        storage.load_stats_current(strict=True)
    assert storage.STATS_CURRENT_FILE.read_bytes() == original


@pytest.mark.parametrize("period", ["0001-Q1", "2026-Q2", "9999-Q4"])
def test_strict_quarter_load_accepts_canonical_period_without_pending(backup_env, period):
    storage.save_stats_current({"period": period, "events": []})
    assert storage.load_stats_current(strict=True)["period"] == period


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


def test_load_seen_favourites_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "storage.SEEN_FAVS_FILE",
        str(tmp_path / "missing.json"),
    )

    assert storage.load_seen_favourites() == set()


def test_load_seen_favourites_valid_json(monkeypatch, tmp_path):
    file = tmp_path / "favs.json"

    file.write_text(
        json.dumps(
            {
                "seen_favourites": [
                    "animes_1",
                    "mangas_2",
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "storage.SEEN_FAVS_FILE",
        str(file),
    )

    assert storage.load_seen_favourites() == {
        "animes_1",
        "mangas_2",
    }


def test_load_seen_favourites_corrupted_json(monkeypatch, tmp_path):
    file = tmp_path / "favs.json"

    file.write_text("{", encoding="utf-8")

    monkeypatch.setattr(
        "storage.SEEN_FAVS_FILE",
        str(file),
    )

    assert storage.load_seen_favourites() == set()


def test_seen_favourites_roundtrip(monkeypatch, tmp_path):
    file = tmp_path / "favs.json"

    monkeypatch.setattr(
        "storage.SEEN_FAVS_FILE",
        str(file),
    )

    original = {
        "animes_1",
        "mangas_2",
    }

    storage.save_seen_favourites(original)

    assert storage.load_seen_favourites() == original


@pytest.mark.parametrize("raw", [b"\xff\xfe", b"[" * 5000 + b"]" * 5000])
def test_legacy_backup_anchor_ignores_damaged_quarter_bytes(backup_env, raw):
    storage.STATS_CURRENT_FILE.write_bytes(raw)
    state = storage.subscriber_state_from_payload({"subscribers": {}})
    assert storage.ensure_backup_schedule(state, now=123.0) is True
    assert state.backup_schedule["weekly_started_at"] == 123.0
    assert storage.STATS_CURRENT_FILE.read_bytes() == raw


def test_strict_missing_quarter_initialization_propagates_write_failure(backup_env, monkeypatch):
    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr("storage._atomic_write", fail)
    with pytest.raises(storage.QuarterDeliveryStateError, match="^current_write$"):
        storage.load_stats_current(strict=True, initialize_missing=True)
    assert not storage.STATS_CURRENT_FILE.exists()


def test_strict_quarter_load_rejects_damaged_pending_without_reset(backup_env):
    cur = _quarter_state()
    cur["pending_quarter_delivery"]["next_unit"] = True
    storage.save_stats_current(cur)
    original = storage.STATS_CURRENT_FILE.read_bytes()
    with pytest.raises(storage.QuarterDeliveryStateError, match="^progress_index$"):
        storage.load_stats_current(strict=True, initialize_missing=True)
    assert storage.STATS_CURRENT_FILE.read_bytes() == original


@pytest.mark.parametrize("change", [
    lambda j: j.update(version=True),
    lambda j: j.update(version=2),
    lambda j: j.update(normalization_version=2),
    lambda j: j.update(profile="Other"),
    lambda j: j.update(baseline_initialized=1),
    lambda j: j.update(baseline_ids=[1, True]),
    lambda j: j.update(baseline_ids=[1, 1]),
    lambda j: j.update(processed_seq=True),
    lambda j: j.update(processed_seq=-1),
    lambda j: j.update(processed_seq=2),
    lambda j: j["events"][0].update(seq=True),
    lambda j: j["events"][0].update(seq=2),
    lambda j: j["events"][0].update(history_id=True),
    lambda j: j["events"].append(dict(j["events"][0], seq=2)),
    lambda j: j["events"][0].update(event_type=[]),
    lambda j: j["events"][0].update(media=[]),
    lambda j: j["events"][0].update(score=True),
    lambda j: j["events"][0].update(score_change=[1, False]),
    lambda j: j["events"][0].update(event_at="2026-04-01T00:00:00+00:00"),
    lambda j: j["events"][0].update(observed_at="2026-04-02T00:00:00"),
    lambda j: j["events"][0].update(time_quality="missing"),
    lambda j: j.update(baseline_initialized=False),
])
def test_journal_strict_validation_preserves_bad_bytes(journal_factory, change):
    from event_journal_schema import EventJournalStateError

    journal = journal_factory()
    change(journal)
    raw = json.dumps(journal).encode("utf-8")
    storage.EVENT_JOURNAL_FILE.write_bytes(raw)
    with pytest.raises(EventJournalStateError):
        storage.load_event_journal()
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == raw


@pytest.mark.parametrize("raw", [
    b"{broken", b"\xff", b"[" * 1500 + b"]" * 1500,
    b'{"version":1,"version":1}',
])
def test_journal_parser_rejects_invalid_encoding_depth_and_duplicate_keys(raw):
    from event_journal_schema import EventJournalStateError

    storage.EVENT_JOURNAL_FILE.write_bytes(raw)
    with pytest.raises(EventJournalStateError):
        storage.load_event_journal()
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == raw


def test_journal_read_failure_is_not_absence(monkeypatch):
    from event_journal_schema import EventJournalStateError

    original = type(storage.EVENT_JOURNAL_FILE).open

    def fail_read(path, *args, **kwargs):
        if path == storage.EVENT_JOURNAL_FILE:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    storage.EVENT_JOURNAL_FILE.write_bytes(b"{}")
    monkeypatch.setattr(type(storage.EVENT_JOURNAL_FILE), "open", fail_read)
    with pytest.raises(EventJournalStateError, match="journal_read"):
        storage.load_event_journal()


def test_journal_size_and_checkpoint_reserve_are_inclusive(journal_factory, monkeypatch):
    from event_journal_schema import (
        EventJournalStateError,
        journal_json,
        parse_event_journal,
    )

    journal = journal_factory()
    size = len(journal_json(journal).encode("utf-8"))
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", size + 16)
    monkeypatch.setattr("storage.JOURNAL_CHECKPOINT_RESERVE", 16)
    assert storage.save_event_journal(journal, admitting=True) == size
    original = storage.EVENT_JOURNAL_FILE.read_bytes()
    journal["events"][0]["description"] += "x"
    with pytest.raises(EventJournalStateError, match="journal_capacity"):
        storage.save_event_journal(journal, admitting=True)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == original
    journal["processed_seq"] = 1
    storage.save_event_journal(journal)
    raw = storage.EVENT_JOURNAL_FILE.read_bytes()
    monkeypatch.setattr("event_journal_schema.JOURNAL_MAX_BYTES", len(raw))
    assert parse_event_journal(raw) == journal
    with pytest.raises(EventJournalStateError):
        parse_event_journal(raw + b" ")


@pytest.mark.parametrize("projection", [
    {"journal_id": "b" * 32, "baseline_seq": 0, "applied_seq": 0},
    {"journal_id": "a" * 32, "baseline_seq": True, "applied_seq": 0},
    {"journal_id": "a" * 32, "baseline_seq": 1, "applied_seq": 1},
    {"journal_id": "a" * 32, "baseline_seq": 0, "applied_seq": 2},
])
def test_journal_recovery_set_rejects_invalid_identity_and_cursor(journal_factory, projection):
    from event_journal_schema import (
        EventJournalStateError,
        validate_recovery_set,
    )

    with pytest.raises(EventJournalStateError):
        validate_recovery_set(journal_factory(), {"event_projection": projection})


def test_existing_journal_prevents_fabricated_first_run_quarter(journal_factory):
    storage.save_event_journal(journal_factory(count=0))
    with pytest.raises(storage.QuarterDeliveryStateError, match="current_missing"):
        storage.load_stats_current(strict=True, initialize_missing=True)
    assert not storage.STATS_CURRENT_FILE.exists()


def test_strict_projection_write_rejects_bool_without_replacing_current(journal_factory):
    from copy import deepcopy

    cur = {"period": "2026-Q2", "events": []}
    storage.save_stats_current(cur, strict=True)
    original = storage.STATS_CURRENT_FILE.read_bytes()
    candidate = deepcopy(cur)
    candidate["event_projection"] = {"journal_id": journal_factory()["journal_id"], "baseline_seq": 0, "applied_seq": True}
    with pytest.raises(storage.QuarterDeliveryStateError):
        storage.save_stats_current(candidate, strict=True)
    assert storage.STATS_CURRENT_FILE.read_bytes() == original


@pytest.mark.parametrize("change", [
    lambda j: j.update(version=3),
    lambda j: j.pop("catchup"),
    lambda j: j.update(baseline_initialized=False),
    lambda j: j["catchup"].update(page=True),
    lambda j: j["catchup"].update(page=0),
    lambda j: j["catchup"].update(spanning=1),
    lambda j: j["catchup"].update(phase=[]),
    lambda j: j["catchup"].update(phase="unknown"),
    lambda j: j["catchup"].update(frontier=[]),
    lambda j: j["catchup"].update(frontier=[999]),
    lambda j: j["catchup"].update(frontier=[True]),
    lambda j: j["catchup"].update(head_ids=[2, 2]),
    lambda j: j["catchup"].update(head_ids=[999]),
    lambda j: j["catchup"]["staged"][0].update(seq=2),
    lambda j: j["catchup"]["staged"][1].update(history_id=2),
    lambda j: j.update(baseline_ids=[1, 2]),
    lambda j: j.update(events=j["catchup"]["staged"], processed_seq=0),
    lambda j: j["catchup"].update(phase="head", spanning=False),
])
def test_acquisition_validation_rejects_unsafe_state_without_replacing_bytes(acquisition_factory, change):
    from event_journal_schema import EventJournalStateError

    journal = acquisition_factory()
    change(journal)
    original = json.dumps(journal).encode("utf-8")
    storage.EVENT_JOURNAL_FILE.write_bytes(original)
    with pytest.raises(EventJournalStateError):
        storage.load_event_journal()
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == original


def test_acquisition_shares_inclusive_member_limit_and_reserve(acquisition_factory, monkeypatch):
    from event_journal_schema import (
        EventJournalStateError,
        journal_json,
        parse_event_journal,
    )

    journal = acquisition_factory()
    size = len(journal_json(journal).encode("utf-8"))
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", size + 16)
    monkeypatch.setattr("storage.JOURNAL_CHECKPOINT_RESERVE", 16)
    assert storage.save_event_journal(journal, admitting=True) == size
    original = storage.EVENT_JOURNAL_FILE.read_bytes()
    journal["catchup"]["staged"][0]["description"] += "x"
    with pytest.raises(EventJournalStateError, match="journal_capacity"):
        storage.save_event_journal(journal, admitting=True)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == original
    monkeypatch.setattr("event_journal_schema.JOURNAL_MAX_BYTES", len(original))
    assert parse_event_journal(original)["catchup"]["page"] == 2
    with pytest.raises(EventJournalStateError):
        parse_event_journal(original + b" ")


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("value", [False, True])
def test_optional_delivery_uncertainty_preserves_old_frozen_hash(version, value):
    cur = _quarter_state() if version == 1 else _quarter_state_v2()
    plan = cur["pending_quarter_delivery"]
    if version == 3:
        plan = storage.new_quarter_delivery_plan(
            plan["old_period"], plan["new_period"], plan["report_units"],
            event_time_revisions={plan["old_period"]: 3},
        )
        cur["pending_quarter_delivery"] = plan
    original_hash = plan["plan_hash"]
    plan["delivery_uncertain"] = value
    assert storage.validate_pending_quarter_delivery(cur) is plan
    assert plan["plan_hash"] == original_hash
    if value and version != 1:
        with pytest.raises(storage.QuarterDeliveryStateError, match="uncertain_downgrade"):
            storage.downgrade_quarter_delivery(plan, plan["next_unit"])


@pytest.mark.parametrize("value", [None, 0, 1, "true", []])
def test_delivery_uncertainty_rejects_nonboolean_marker(value):
    cur = _quarter_state()
    cur["pending_quarter_delivery"]["delivery_uncertain"] = value
    with pytest.raises(storage.QuarterDeliveryStateError, match="delivery_uncertainty"):
        storage.validate_pending_quarter_delivery(cur)


def test_completed_plan_cannot_retain_unacknowledged_dispatch_marker():
    cur = _quarter_state()
    plan = cur["pending_quarter_delivery"]
    plan["next_unit"] = len(plan["report_messages"])
    plan["delivery_uncertain"] = True
    with pytest.raises(storage.QuarterDeliveryStateError, match="completed_uncertainty"):
        storage.validate_pending_quarter_delivery(cur)


def test_current_capacity_reserves_dispatch_marker_before_sending(backup_env, monkeypatch):
    cur = _quarter_state()
    cur["last_report_sent"] = None
    cur["tracking_since"] = "2026-07-01T00:00:00+00:00"
    storage.save_stats_current(cur, strict=True)
    original = storage.STATS_CURRENT_FILE.read_bytes()
    started = json.loads(json.dumps(cur))
    started["pending_quarter_delivery"].update(next_unit=1, delivery_uncertain=False)
    started_size = storage.json_publication_size(storage.stats_current_json(started, strict=False))
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", started_size - 1)
    with pytest.raises(storage.QuarterDeliveryStateError, match="current_capacity"):
        storage.stats_current_json(cur)
    with pytest.raises(storage.QuarterDeliveryStateError, match="current_write"):
        storage.save_stats_current(cur, strict=True)
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", started_size)
    storage.save_stats_current(cur, strict=True)
    storage.save_stats_current(started, strict=True)
    assert json.loads(storage.STATS_CURRENT_FILE.read_bytes()) == started
    assert storage.load_stats_current(strict=True)["pending_quarter_delivery"] == started["pending_quarter_delivery"]


@pytest.mark.parametrize("strict", [False, True])
def test_current_publication_is_compact_and_preserves_all_values(backup_env, strict):
    cur = _quarter_state_v2()
    cur["events"] = [{"id": "旧", "title": "строка\n日本語 🍀"}]
    cur["pending_quarter_delivery"].update(next_unit=1, delivery_uncertain=True)
    before = deepcopy(cur)
    storage.save_stats_current(cur, strict=strict)
    raw = storage.STATS_CURRENT_FILE.read_bytes()
    assert raw == json.dumps(cur, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert json.loads(raw) == before == cur


def test_current_initial_capacity_is_inclusive_and_preserves_old_bytes(backup_env, monkeypatch):
    cur = {"period": "2026-Q1", "events": [], "tracking_since": "2026-01-01T00:00:00+00:00", "pending_quarter_delivery": None}
    storage.save_stats_current(cur, strict=True)
    raw = storage.STATS_CURRENT_FILE.read_bytes()
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", len(raw))
    assert storage.load_stats_current(strict=True) == cur
    storage.save_stats_current(cur, strict=True)
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", len(raw) - 1)
    with pytest.raises(storage.QuarterDeliveryStateError, match="current_capacity"):
        storage.stats_current_json(cur)
    with pytest.raises(storage.QuarterDeliveryStateError, match="current_write"):
        storage.save_stats_current(cur, strict=True)
    assert storage.STATS_CURRENT_FILE.read_bytes() == raw


def test_current_capacity_reserves_reader_defaults(backup_env, monkeypatch):
    fixed_start = storage.quarter_start()
    monkeypatch.setattr("storage.quarter_start", lambda: fixed_start)
    cur = {"period": "2026-Q1", "events": []}
    completed = {**cur, "tracking_since": storage.quarter_start().isoformat(), "pending_quarter_delivery": None}
    boundary = len(storage.stats_current_json(completed, strict=False).encode("utf-8"))
    original = json.dumps(cur, separators=(",", ":")).encode("utf-8")
    storage.STATS_CURRENT_FILE.write_bytes(original)
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", boundary - 1)
    with pytest.raises(storage.QuarterDeliveryStateError, match="current_capacity"):
        storage.load_stats_current(strict=True)
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", boundary)
    storage.save_stats_current(storage.load_stats_current(strict=True), strict=True)
    assert storage.STATS_CURRENT_FILE.read_bytes() == storage.stats_current_json(completed).encode("utf-8")


def test_current_marker_capacity_includes_future_progress_digits(backup_env, monkeypatch):
    cur = _quarter_state()
    cur["last_report_sent"] = None
    cur["tracking_since"] = "2026-07-01T00:00:00+00:00"
    plan = cur["pending_quarter_delivery"]
    plan["report_messages"] = ["frozen"] * 1000
    plan["next_unit"] = 9
    plan["plan_hash"] = storage._quarter_plan_hash(plan)
    marker = deepcopy(cur)
    marker["pending_quarter_delivery"].update(next_unit=999, delivery_uncertain=False)
    boundary = len(storage.stats_current_json(marker, strict=False).encode("utf-8"))
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", boundary - 1)
    with pytest.raises(storage.QuarterDeliveryStateError, match="current_capacity"):
        storage.stats_current_json(cur)
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", boundary)
    for index, uncertain in [(9, True), (10, False), (999, True), (999, False), (1000, None)]:
        candidate = deepcopy(cur)
        candidate["pending_quarter_delivery"]["next_unit"] = index
        if uncertain is not None:
            candidate["pending_quarter_delivery"]["delivery_uncertain"] = uncertain
        else:
            candidate["last_report_sent"] = candidate["period"]
        storage.save_stats_current(candidate, strict=True)
        assert len(storage.STATS_CURRENT_FILE.read_bytes()) <= boundary
        assert storage.load_stats_current(strict=True) == candidate


def test_current_final_capacity_includes_correction_revision_digits(
    backup_env, source_history_factory, monkeypatch,
):
    from event_time_stats import acknowledge_revisions

    _, cur = source_history_factory()
    bucket = cur["event_time"]["periods"]["2026-Q1"]
    bucket["revision"] = 10 ** 80
    cur["event_projection"]["applied_seq"] = bucket["revision"]
    revisions = {"2026-Q1": bucket["revision"]}
    plan = storage.new_quarter_delivery_plan(
        "2026-Q1", "2026-Q2", [{"transport": "html", "content": "frozen", "disable_preview": False}] * 10,
        event_time_revisions=revisions,
    )
    cur["pending_quarter_delivery"] = plan
    cur["event_time"]["report_ack"] = {"plan_id": plan["plan_id"], "revisions": revisions}
    completed = deepcopy(cur)
    completed["pending_quarter_delivery"]["next_unit"] = 10
    completed["last_report_sent"] = "2026-Q2"
    acknowledge_revisions(completed)
    boundary = len(storage.stats_current_json(completed, strict=False).encode("utf-8"))
    assert boundary > len(storage.stats_current_json(cur, strict=False).encode("utf-8")) + 30
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", boundary - 1)
    with pytest.raises(storage.QuarterDeliveryStateError, match="current_capacity"):
        storage.stats_current_json(cur)
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", boundary)
    storage.save_stats_current(cur, strict=True)
    started = deepcopy(cur)
    started["pending_quarter_delivery"].update(next_unit=9, delivery_uncertain=True)
    storage.save_stats_current(started, strict=True)
    storage.save_stats_current(completed, strict=True)
    assert storage.load_stats_current(strict=True) == completed
    assert cur["event_time"]["periods"]["2026-Q1"]["announced_revision"] == 0


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
@pytest.mark.parametrize("version", ["legacy", 1, 2, 3])
def test_formatted_current_migrates_encoding_without_changing_frozen_state(
    backup_env, source_history_factory, eol, version,
):
    _, cur = source_history_factory()
    if version == "legacy":
        plan = {"old_period": "2026-Q1", "new_period": "2026-Q2", "report_messages": ["frozen 日本語"], "report_sent": True}
    elif version == 1:
        plan = storage.new_quarter_delivery("2026-Q1", "2026-Q2", ["frozen", "続き"])
        plan.update(next_unit=1, delivery_uncertain=True)
    else:
        revisions = {"2026-Q1": 1} if version == 3 else None
        plan = storage.new_quarter_delivery_plan("2026-Q1", "2026-Q2", [_rich_frozen_unit("frozen"), _rich_frozen_unit("続き")], event_time_revisions=revisions)
        plan.update(next_unit=1, delivery_uncertain=True)
        if version == 3:
            cur["event_time"]["report_ack"] = {"plan_id": plan["plan_id"], "revisions": revisions}
    cur["pending_quarter_delivery"] = plan
    raw = json.dumps(cur, ensure_ascii=False, indent=2).replace("\n", eol).encode("utf-8")
    storage.STATS_CURRENT_FILE.write_bytes(raw)
    loaded = storage.load_stats_current(strict=True)
    assert loaded == cur
    assert storage.STATS_CURRENT_FILE.read_bytes() == raw
    storage.save_stats_current(loaded, strict=True)
    assert b"\n" not in storage.STATS_CURRENT_FILE.read_bytes()
    assert storage.load_stats_current(strict=True) == cur


def _progress_journal(factory):
    from notification_outbox import (
        enqueue,
        migrate_outbox,
    )
    journal = migrate_outbox(factory(), 0)
    return enqueue(journal, journal["events"][0], "frozen", {10: "b" * 32}, 1000)


def test_recipient_publications_never_rewrite_or_reparse_retained_history(
    backup_env, journal_factory, monkeypatch,
):
    from notification_outbox import (
        begin_attempt,
        complete_attempt,
    )
    journal = _progress_journal(journal_factory)
    storage.save_event_journal(journal)
    # Прогреваем revision cache до начала измеряемой recipient работы.
    storage.load_notification_journal()
    original = storage.EVENT_JOURNAL_FILE.read_bytes()
    writes = []
    write = storage._atomic_write

    def observe(path, payload):
        writes.append(path)
        write(path, payload)

    monkeypatch.setattr("storage._atomic_write", observe)
    monkeypatch.setattr("storage.parse_history_member", lambda *a, **k: pytest.fail("history reparsed"))
    journal = storage.load_notification_journal()
    recipient = deepcopy(journal["outbox"]["records"][0]["recipients"]["10"])
    begin_attempt(recipient, 1000)
    journal = storage.save_notification_recipient(journal, 1, "10", recipient)
    complete_attempt(recipient, "confirmed_success", 1001)
    storage.save_notification_recipient(journal, 1, "10", recipient)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == original
    assert writes == [storage.notification_progress_file()] * 2
    assert storage.load_notification_journal()["outbox"]["records"][0]["recipients"]["10"]["status"] == "delivered"


def test_warm_progress_reads_and_recipient_updates_do_not_repeat_read_validation(
    backup_env, journal_factory, monkeypatch,
):
    from notification_outbox import begin_attempt

    storage.save_event_journal(_progress_journal(journal_factory))
    original_join = storage.join_history_progress
    joins = []

    def join(*args):
        joins.append(args)
        return original_join(*args)

    monkeypatch.setattr("storage.join_history_progress", join)
    journal = storage.load_notification_journal()
    assert len(joins) == 1
    monkeypatch.setattr("storage._read_journal_member", lambda *a: pytest.fail("warm read"))
    for _ in range(3):
        assert storage.load_notification_journal() is journal
    recipient = deepcopy(journal["outbox"]["records"][0]["recipients"]["10"])
    begin_attempt(recipient, 1000)
    marked = storage.save_notification_recipient(journal, 1, "10", recipient)
    assert len(joins) == 2  # Новая публикация всё равно проходит общий validator.
    for _ in range(3):
        assert storage.load_notification_journal() is marked
    assert len(joins) == 2
    assert journal["outbox"]["records"][0]["recipients"]["10"]["attempts"] == []
    recipient["attempts"].clear()
    assert len(marked["outbox"]["records"][0]["recipients"]["10"]["attempts"]) == 1


def test_general_progress_read_and_save_cannot_lend_mutable_aliases(
    backup_env, journal_factory,
):
    journal = _progress_journal(journal_factory)
    storage.save_event_journal(journal)
    borrowed = storage.load_notification_journal()
    snapshot = storage.load_event_journal()
    snapshot["outbox"]["records"][0]["payload"]["text"] = "different"
    snapshot["outbox"]["records"][0]["recipients"]["10"]["attempts"].append({})
    assert storage.load_notification_journal() is borrowed
    assert borrowed == journal
    storage.save_event_journal(journal)
    journal["outbox"]["records"].clear()
    assert storage.load_notification_journal() == borrowed


@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("after", [False, True])
def test_owned_recipient_publication_adopts_only_successful_published_state(
    backup_env, journal_factory, monkeypatch, interrupt, after,
):
    from notification_outbox import begin_attempt

    storage.save_event_journal(_progress_journal(journal_factory))
    journal = storage.load_notification_journal()
    before = storage.notification_progress_file().read_bytes()
    recipient = deepcopy(journal["outbox"]["records"][0]["recipients"]["10"])
    begin_attempt(recipient, 1000)
    write = storage._atomic_write

    def fail(path, payload):
        if after:
            write(path, payload)
        raise KeyboardInterrupt if interrupt else OSError("publication")

    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", fail)
        with pytest.raises(KeyboardInterrupt if interrupt else storage.EventJournalStateError):
            storage.save_notification_recipient(journal, 1, "10", recipient)
    assert journal["outbox"]["records"][0]["recipients"]["10"]["attempts"] == []
    restored = storage.load_notification_journal()
    actual = restored["outbox"]["records"][0]["recipients"]["10"]
    assert actual["attempts"] == (recipient["attempts"] if after else [])
    assert actual["status"] == "pending"
    if not after:
        assert storage.notification_progress_file().read_bytes() == before


def test_owned_recipient_publication_still_rejects_invalid_delta(
    backup_env, journal_factory,
):
    storage.save_event_journal(_progress_journal(journal_factory))
    journal = storage.load_notification_journal()
    before = storage.notification_progress_file().read_bytes()
    recipient = deepcopy(journal["outbox"]["records"][0]["recipients"]["10"])
    recipient["attempts"] = [{"at": 1000, "outcome": "confirmed_success"}]
    with pytest.raises(storage.EventJournalStateError, match="progress_invalid"):
        storage.save_notification_recipient(journal, 1, "10", recipient)
    assert storage.notification_progress_file().read_bytes() == before
    assert storage.load_notification_journal() == journal


@pytest.mark.parametrize("change", ["replacement", "generation", "cold"])
def test_warm_progress_revision_change_runs_full_parser_and_rejects_old_borrow(
    backup_env, journal_factory, monkeypatch, change,
):
    storage.save_event_journal(_progress_journal(journal_factory))
    old = storage.load_notification_journal()
    path = storage.notification_progress_file()
    if change == "replacement":
        stat = path.stat()
        value = json.loads(path.read_bytes())
        value["outbox"]["records"][0]["payload"]["text"] = "change"
        storage._atomic_write(path, storage.compact_json(value))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert path.stat().st_size == stat.st_size
    elif change == "generation":
        storage.mark_restorable_state_restored()
    else:
        monkeypatch.setattr("storage._journal_history_cache", None)
        monkeypatch.setattr("storage._journal_progress_cache", None)
    parse = storage.parse_progress_member
    parsed = []

    def observe(raw):
        parsed.append(raw)
        return parse(raw)

    monkeypatch.setattr("storage.parse_progress_member", observe)
    current = storage.load_notification_journal()
    assert len(parsed) == 1
    assert current is not old
    if change == "replacement":
        assert current["outbox"]["records"][0]["payload"]["text"] == "change"
    with pytest.raises(storage.EventJournalStateError, match="recipient_changed"):
        storage.save_notification_recipient(old, 1, "10", old["outbox"]["records"][0]["recipients"]["10"])


@pytest.mark.parametrize("phase", ["prepare", "activate"])
@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("interrupt", [False, True])
def test_split_migration_interruption_keeps_published_authority(
    backup_env, journal_factory, monkeypatch, phase, after, interrupt,
):
    journal = _progress_journal(journal_factory)
    # Существующий поддерживаемый journal v3 и необязательная старая preparation.
    storage.EVENT_JOURNAL_FILE.write_text(json.dumps(journal), encoding="utf-8")
    storage.notification_progress_file().write_bytes(b"abandoned preparation")
    before = storage.EVENT_JOURNAL_FILE.read_bytes()
    write = storage._atomic_write
    target = storage.notification_progress_file() if phase == "prepare" else storage.EVENT_JOURNAL_FILE

    def fail(path, payload):
        if path == target:
            if after:
                write(path, payload)
            if interrupt:
                raise KeyboardInterrupt
            raise OSError("publication")
        write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", fail)
        with pytest.raises(KeyboardInterrupt if interrupt else storage.EventJournalStateError):
            storage.save_event_journal(journal)
    assert storage.load_event_journal() == journal
    if phase == "prepare" or not after:
        assert storage.EVENT_JOURNAL_FILE.read_bytes() == before
    storage.save_event_journal(journal)
    assert storage.load_event_journal() == journal
    assert json.loads(storage.EVENT_JOURNAL_FILE.read_bytes())["version"] == 4


@pytest.mark.parametrize("after", [False, True])
def test_admission_interruption_keeps_old_progress_valid(
    backup_env, journal_factory, monkeypatch, after,
):
    journal = _progress_journal(journal_factory)
    storage.save_event_journal(journal)
    candidate = deepcopy(journal)
    candidate["events"].append(journal_factory(count=2)["events"][1])
    before = storage.notification_progress_file().read_bytes()
    write = storage._atomic_write

    def fail(path, payload):
        assert path == storage.EVENT_JOURNAL_FILE
        if after:
            write(path, payload)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", fail)
        with pytest.raises(KeyboardInterrupt):
            storage.save_event_journal(candidate, admitting=True)
    assert storage.notification_progress_file().read_bytes() == before
    assert storage.load_event_journal() == (candidate if after else journal)
    storage.save_event_journal(candidate, admitting=True)
    assert storage.load_event_journal() == candidate


@pytest.mark.parametrize("damage", ["missing_progress", "bad_progress", "bad_history", "generation"])
def test_warm_history_cache_never_hides_replacement_or_restore(
    backup_env, journal_factory, monkeypatch, damage,
):
    journal = _progress_journal(journal_factory)
    storage.save_event_journal(journal)
    assert storage.load_notification_journal() == journal
    if damage == "missing_progress":
        storage.notification_progress_file().unlink()
    elif damage == "bad_progress":
        storage.notification_progress_file().write_bytes(b"{broken")
    elif damage == "bad_history":
        storage.EVENT_JOURNAL_FILE.write_bytes(b"{broken")
    else:
        storage.mark_restorable_state_restored()
        monkeypatch.setattr("storage.parse_history_member", lambda *a, **k: (_ for _ in ()).throw(storage.EventJournalStateError("fresh read")))
    with pytest.raises(storage.EventJournalStateError):
        storage.load_notification_journal()


def test_general_history_reader_cannot_mutate_cached_authority(backup_env, journal_factory):
    journal = _progress_journal(journal_factory)
    storage.save_event_journal(journal)
    snapshot = storage.load_event_journal()
    snapshot["events"][0]["title"]["name"] = "changed"
    snapshot["baseline_ids"].clear()
    assert storage.load_notification_journal() == journal


def test_split_first_baseline_initialization_remains_quiet(backup_env, journal_factory):
    from notification_outbox import migrate_outbox

    journal = journal_factory(count=0)
    journal.update(baseline_initialized=False, baseline_ids=[])
    journal = migrate_outbox(journal, 0)
    storage.save_event_journal(journal)
    journal.update(baseline_initialized=True, baseline_ids=[1, 2, 3])
    storage.save_event_journal(journal, admitting=True)
    assert storage.load_event_journal() == journal
    assert journal["outbox"]["records"] == []


def test_atomic_history_replacement_with_same_size_and_mtime_invalidates_cache(backup_env, journal_factory):
    journal = _progress_journal(journal_factory)
    storage.save_event_journal(journal)
    assert storage.load_notification_journal() == journal
    stat = storage.EVENT_JOURNAL_FILE.stat()
    history = json.loads(storage.EVENT_JOURNAL_FILE.read_bytes())
    before = history["events"][0]["title"]["name"]
    history["events"][0]["title"]["name"] = "x" * len(before)
    storage._atomic_write(storage.EVENT_JOURNAL_FILE, storage.compact_json(history))
    os.utime(storage.EVENT_JOURNAL_FILE, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert storage.EVENT_JOURNAL_FILE.stat().st_size == stat.st_size
    assert storage.load_notification_journal()["events"][0]["title"]["name"] != before


@pytest.mark.parametrize("change", [
    lambda journal: journal.update(version=3.0),
    lambda journal: journal.pop("processed_seq"),
    lambda journal: journal.pop("outbox"),
    lambda journal: journal.update(processed_seq=True),
])
def test_progress_save_keeps_strict_root_validation_after_cache_warmup(
    backup_env, journal_factory, change,
):
    journal = _progress_journal(journal_factory)
    storage.save_event_journal(journal)
    journal = storage.load_event_journal()
    original = storage.notification_progress_file().read_bytes()
    change(journal)
    with pytest.raises(storage.EventJournalStateError):
        storage.save_event_journal(journal)
    assert storage.notification_progress_file().read_bytes() == original


@pytest.mark.parametrize("failure", [None, "write", "before", "after"])
def test_source_compaction_atomic_boundary_restart_and_exact_progress(
    backup_env, source_history_factory, monkeypatch, failure,
):
    from event_journal_schema import validate_recovery_set
    from source_history import (
        event_at_seq,
        event_count,
        known_history_ids,
        prefix_seq,
    )
    journal, cur = source_history_factory(count=8, pending_from=6)
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    original = storage.EVENT_JOURNAL_FILE.read_bytes()
    progress = storage.notification_progress_file().read_bytes()
    write = storage._atomic_write

    def publish(path, payload):
        if path == storage.EVENT_JOURNAL_FILE and json.loads(payload).get("version") == 6:
            if failure == "write":
                raise OSError("publication")
            if failure == "before":
                raise asyncio.CancelledError
            write(path, payload)
            if failure == "after":
                raise asyncio.CancelledError
            return
        write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", publish)
        if failure:
            with pytest.raises(asyncio.CancelledError if failure != "write" else storage.EventJournalStateError):
                storage.compact_completed_history(
                    journal, storage.load_stats_current(strict=True),
                    expected_generation=storage.restorable_restore_generation(), force=True,
                )
        else:
            storage.compact_completed_history(
                journal, storage.load_stats_current(strict=True),
                expected_generation=storage.restorable_restore_generation(), force=True,
            )
    storage._journal_history_cache = storage._journal_progress_cache = None
    recovered = storage.load_event_journal()
    validate_recovery_set(recovered, cur)
    assert storage.notification_progress_file().read_bytes() == progress
    assert recovered["outbox"] == journal["outbox"]
    assert event_count(recovered) == 8
    assert known_history_ids(recovered) == set(range(1, 10))
    if failure in {"write", "before"}:
        assert storage.EVENT_JOURNAL_FILE.read_bytes() == original
        assert recovered == journal
    else:
        assert prefix_seq(recovered) == 5
        assert [event["seq"] for event in recovered["events"]] == [6, 7, 8]
        assert event_at_seq(recovered, 6) == journal["events"][5]
        with pytest.raises(ValueError, match="source_seq"):
            event_at_seq(recovered, 5)
        assert json.loads(storage.EVENT_JOURNAL_FILE.read_bytes())["version"] == 6


@pytest.mark.parametrize("change", [
    "version", "through", "checksum", "ids", "duplicate", "source_id",
    "time", "canonical", "unknown", "binding", "cursor", "suffix", "projection", "null_base", "bad_version",
])
def test_source_runtime_recovery_rejects_corrupt_or_mismatched_base(
    backup_env, source_history_factory, change,
):
    from event_journal_schema import validate_recovery_set
    from source_history import content_hash
    journal, cur = source_history_factory()
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    journal = storage.compact_completed_history(
        journal, storage.load_stats_current(strict=True),
        expected_generation=storage.restorable_restore_generation(), force=True,
    )
    history = json.loads(storage.EVENT_JOURNAL_FILE.read_bytes())
    base = history["source_base"]
    if change == "version":
        base["version"] = True
    elif change == "null_base":
        history["source_base"] = None
    elif change == "bad_version":
        history["version"] = []
    elif change == "through":
        base["through_seq"] -= 1
    elif change == "checksum":
        base["checksum"] = "a" * 64
    elif change == "ids":
        base["ids"][0][1] = "invalid"
    elif change == "duplicate":
        base["ids"][1] = base["ids"][0]
    elif change == "source_id":
        next(iter(base["periods"].values()))[0]["history_id"] = 98765
    elif change == "time":
        next(iter(base["periods"].values()))[0]["event_at"] = "bad"
    elif change == "canonical":
        source = next(iter(base["periods"].values()))
        source.append(deepcopy(source[0]))
    elif change == "unknown":
        base["unknown"]["missing"] = True
    elif change == "binding":
        base["binding"]["legacy_hash"] = "a" * 64
    elif change == "cursor":
        progress = json.loads(storage.notification_progress_file().read_bytes())
        progress["outbox"]["completed_seq"] -= 1
        storage.notification_progress_file().write_text(json.dumps(progress), encoding="utf-8")
    elif change == "suffix":
        history["events"] = [journal.get("events", [{}])[0]] if journal["events"] else [{"seq": 1}]
    else:
        cur["event_time"]["periods"]["2026-Q1"]["events"][0]["score"] = 9
    if change != "checksum":
        base["checksum"] = content_hash({key: value for key, value in base.items() if key != "checksum"})
    raw = json.dumps(history).encode("utf-8")
    storage.EVENT_JOURNAL_FILE.write_bytes(raw)
    with pytest.raises(storage.EventJournalStateError):
        validate_recovery_set(storage.load_event_journal(), cur)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == raw


@pytest.mark.parametrize("reason", ["threshold", "overhead"])
def test_source_compaction_skips_small_or_nonsaving_candidate_without_publication(
    backup_env, source_history_factory, monkeypatch, reason,
):
    from event_time_stats import compact_source_history
    from notification_progress_schema import compact_json

    journal, cur = source_history_factory(
        count=8 if reason == "threshold" else 1,
        padding=2000 if reason == "threshold" else 0,
    )
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    cur = storage.load_stats_current(strict=True)
    candidate = compact_source_history(journal, cur, journal["processed_seq"])
    assert (len(compact_json(candidate).encode()) < len(compact_json(journal).encode())) == (reason == "threshold")
    paths = (storage.EVENT_JOURNAL_FILE, storage.notification_progress_file(), storage.STATS_CURRENT_FILE)
    before = {path: path.read_bytes() for path in paths}

    def unexpected_write(*args):
        raise AssertionError("inert maintenance published bytes")

    monkeypatch.setattr("storage._atomic_write", unexpected_write)
    assert storage.compact_completed_history(
        journal, cur, expected_generation=storage.restorable_restore_generation(),
        force=reason == "overhead",
    ) == journal
    assert {path: path.read_bytes() for path in paths} == before


def test_source_compaction_pending_barrier_bounded_repeated_and_fresh_ack(
    backup_env, source_history_factory, monkeypatch,
):
    from notification_outbox import (
        begin_attempt,
        complete_attempt,
    )
    from source_history import prefix_seq
    journal, cur = source_history_factory(count=9, pending_from=6)
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    cur = storage.load_stats_current(strict=True)
    monkeypatch.setattr("storage.SOURCE_COMPACTION_EVENTS", 2)
    stale = storage.load_event_journal()
    recipient = deepcopy(stale["outbox"]["records"][0]["recipients"]["10"])
    begin_attempt(recipient, 1001)
    journal = storage.save_notification_recipient(
        storage.load_notification_journal(), 6, "10", recipient,
    )
    with pytest.raises(storage.EventJournalStateError, match="compaction_changed"):
        storage.compact_completed_history(
            stale, cur, expected_generation=storage.restorable_restore_generation(), force=True,
        )
    before = deepcopy(journal["outbox"])
    for expected in [2, 4, 5]:
        journal = storage.compact_completed_history(
            journal, cur, expected_generation=storage.restorable_restore_generation(), force=True,
        )
        assert prefix_seq(journal) == expected
        assert journal["outbox"] == before
    complete_attempt(recipient, "confirmed_success", 1002)
    fresh = storage.save_notification_recipient(
        storage.load_notification_journal(), 6, "10", recipient,
    )
    assert fresh["outbox"]["records"][0]["seq"] == 6
    assert fresh["outbox"]["records"][0]["recipients"]["10"]["status"] == "delivered"
    assert fresh["outbox"]["records"][1:] == before["records"][1:]
    assert storage.compact_completed_history(
        fresh, cur, expected_generation=storage.restorable_restore_generation(), force=True,
    ) == fresh


@pytest.mark.parametrize("failure", [None, "write", "before", "after"])
def test_index_retention_migrates_without_payload_deletion_and_recovers_exact_bytes(
    backup_env, source_index_factory, monkeypatch, failure,
):
    from event_journal_schema import validate_recovery_set

    old, cur = source_index_factory()
    storage.save_event_journal(old)
    storage.save_stats_current(cur, strict=True)
    paths = (storage.EVENT_JOURNAL_FILE, storage.notification_progress_file(), storage.STATS_CURRENT_FILE)
    original = {path: path.read_bytes() for path in paths}
    assert json.loads(original[storage.EVENT_JOURNAL_FILE])["version"] == 5
    assert storage.load_event_journal() == old
    assert {path: path.read_bytes() for path in paths} == original
    write = storage._atomic_write

    def publish(path, payload):
        if path == storage.EVENT_JOURNAL_FILE:
            if failure == "write":
                raise OSError("index publication")
            if failure == "before":
                raise asyncio.CancelledError
            write(path, payload)
            if failure == "after":
                raise asyncio.CancelledError
        else:
            raise AssertionError("fingerprint maintenance wrote progress/current")

    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", publish)
        if failure:
            with pytest.raises(storage.EventJournalStateError if failure == "write" else asyncio.CancelledError):
                storage.compact_completed_history(
                    old, storage.load_stats_current(strict=True), expected_generation=storage.restorable_restore_generation(),
                )
        else:
            storage.compact_completed_history(
                old, storage.load_stats_current(strict=True), expected_generation=storage.restorable_restore_generation(),
            )
    storage._journal_history_cache = storage._journal_progress_cache = None
    recovered = storage.load_event_journal()
    validate_recovery_set(recovered, cur)
    for path in paths[1:]:
        assert path.read_bytes() == original[path]
    assert recovered["events"] == old["events"]
    assert recovered["outbox"] == old["outbox"]
    assert [row[0] for row in recovered["source_base"]["ids"]] == [row[0] for row in old["source_base"]["ids"]]
    if failure in {"write", "before"}:
        assert recovered == old
        assert storage.EVENT_JOURNAL_FILE.read_bytes() == original[storage.EVENT_JOURNAL_FILE]
    else:
        assert json.loads(storage.EVENT_JOURNAL_FILE.read_bytes())["version"] == 6
        assert recovered["source_base"]["ids"][:4] == [[row[0], None] for row in old["source_base"]["ids"][:4]]
        assert recovered["source_base"]["ids"][4:] == old["source_base"]["ids"][4:]
        assert storage.compact_completed_history(
            recovered, cur, expected_generation=storage.restorable_restore_generation(),
        ) == recovered


def test_index_retention_stale_snapshot_rejects_and_fresh_recipient_ack_survives(
    backup_env, source_index_factory,
):
    from notification_outbox import (
        begin_attempt,
        complete_attempt,
    )

    old, cur = source_index_factory()
    storage.save_event_journal(old)
    storage.save_stats_current(cur, strict=True)
    cur = storage.load_stats_current(strict=True)
    recipient = deepcopy(old["outbox"]["records"][0]["recipients"]["10"])
    begin_attempt(recipient, 1001)
    fresh = storage.save_notification_recipient(storage.load_notification_journal(), 4101, "10", recipient)
    with pytest.raises(storage.EventJournalStateError, match="compaction_changed"):
        storage.compact_completed_history(old, cur, expected_generation=storage.restorable_restore_generation())
    maintained = storage.compact_completed_history(fresh, cur, expected_generation=storage.restorable_restore_generation())
    complete_attempt(recipient, "confirmed_success", 1002)
    acknowledged = storage.save_notification_recipient(storage.load_notification_journal(), 4101, "10", recipient)
    assert acknowledged["source_base"] == maintained["source_base"]
    assert acknowledged["outbox"]["records"][0]["recipients"]["10"]["status"] == "delivered"
    assert acknowledged["outbox"]["records"][1] == old["outbox"]["records"][1]
    with pytest.raises(storage.EventJournalStateError, match="compaction_changed"):
        storage.compact_completed_history(old, cur, expected_generation=storage.restorable_restore_generation() - 1)
