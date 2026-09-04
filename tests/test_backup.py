# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Тесты ветки backup: /backup (экспорт/импорт zip) + авто-бэкап состояния.

Дисциплина: каждый тест падает на непропатченном коде и проходит на
пропатченном. Полные aiogram-объекты — через unittest.mock; узкая поверхность —
ручными стабами. Файлы DATA_DIR редиректятся в tmp_path фикстурой backup_env.
"""
import asyncio
import io
import json
import lzma
import time
import zipfile
import zlib
from unittest.mock import (
    AsyncMock,
    Mock,
)

import aiohttp
import pytest

import backup
import fact_bank
import handlers
import storage

# ─────────────────────────────────────────────────────────────
#  Хелперы
# ─────────────────────────────────────────────────────────────


def _zip_bytes(members: dict[str, str]) -> bytes:
    """Собрать zip из {arcname: text-content} в bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _quarter_payload(size: int, period: str) -> str:
    """Собрать валидный квартальный JSON ровно заданного UTF-8-размера."""
    prefix = f'{{"period":"{period}","padding":"'
    suffix = '"}'
    return prefix + ("x" * (size - len(prefix) - len(suffix))) + suffix


def _known_users_payload(user_id=7, name="Neo") -> str:
    """Собрать валидный строгий реестр пользователей."""
    return json.dumps(
        {
            "users": {
                str(user_id): {
                    "display_name": name,
                    "username": "the_one",
                    "first_seen_at": "2026-09-03T10:20:30Z",
                }
            }
        },
        ensure_ascii=False,
    )


def _save_subscriber_schedule(
    subscribers: dict[int, str] | None = None,
    *,
    last_backup_at: object = None,
    weekly_started_at: object = None,
    pending: dict | None = None,
) -> None:
    """Опубликовать канонический subscriber-state для scheduler-тестов."""
    storage.save_subscriber_state(
        storage.SubscriberState(
            subscribers or {},
            {
                "version": 1,
                "last_backup_at": last_backup_at,
                "weekly_started_at": weekly_started_at,
                "pending": pending,
            },
        )
    )


async def _cancel_after_started(awaitable, started: asyncio.Event) -> None:
    """Отменить awaitable после подтверждённого входа в проверяемую операцию."""
    task = asyncio.create_task(awaitable)
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _corrupt_stored_member(raw: bytes, name: str) -> bytes:
    """Повредить данные ZIP-члена, сохранив центральный каталог и старый CRC."""
    damaged = bytearray(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        info = zf.getinfo(name)
        offset = info.header_offset
        name_length = int.from_bytes(damaged[offset + 26:offset + 28], "little")
        extra_length = int.from_bytes(damaged[offset + 28:offset + 30], "little")
        data_offset = offset + 30 + name_length + extra_length
        damaged[data_offset] ^= 0xFF
    return bytes(damaged)


# ─────────────────────────────────────────────────────────────
#  Сборка архива
# ─────────────────────────────────────────────────────────────

def test_build_backup_zip_excludes_tmp_and_keeps_structure(backup_env):
    (backup_env / "subscribers.json").write_text('{"subscribers": {}}', encoding="utf-8")
    (backup_env / "blocked_users.json").write_text(
        '{"blocked_user_ids": [7]}',
        encoding="utf-8",
    )
    (backup_env / "stats_current.json").write_text('{"period": "2026-Q2"}', encoding="utf-8")
    (backup_env / "known_users.json").write_text(
        _known_users_payload(),
        encoding="utf-8",
    )
    (backup_env / "user_alerts.json").write_text('{"enabled": false}', encoding="utf-8")
    (backup_env / "subscribers.json.tmp").write_text("garbage", encoding="utf-8")
    restore_stage = backup_env / ".restore-interrupted.tmp" / "new"
    storage._atomic_write(restore_stage / "subscribers.json", "staged")
    (backup_env / "quarters" / "2026-Q1.json").write_text('{"period": "2026-Q1"}', encoding="utf-8")

    raw = backup._build_backup_zip()
    names = set(zipfile.ZipFile(io.BytesIO(raw)).namelist())

    assert "subscribers.json" in names
    assert "blocked_users.json" in names
    assert "stats_current.json" in names
    assert "known_users.json" in names
    assert "user_alerts.json" in names
    assert "quarters/2026-Q1.json" in names          # вложенность сохранена
    assert "subscribers.json.tmp" not in names       # *.tmp исключён
    assert not any(name.startswith(".restore-") for name in names)


# ─────────────────────────────────────────────────────────────
#  Белый список импорта / zip-slip
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "blocked_users.json",
    "known_users.json",
    "subscribers.json",
    "stats_current.json",
    "update_state.json",
    "user_alerts.json",
    "quarters/2026-Q1.json",
    "quarters/2025-Q4.json",
])
def test_is_allowed_import_member_accepts_whitelist(name):
    assert backup._is_allowed_import_member(name) is True


@pytest.mark.parametrize("name", [
    "seen_ids.json",                 # регенерируется — не восстанавливаем
    "seen_favourites.json",
    "stats_all.json",
    "quarters/evil.txt",             # не .json
    "quarters/sub/deep.json",        # глубже одного уровня
    "../etc/passwd",                 # zip-slip
    "/abs/path.json",                # абсолютный
    "quarters/../subscribers.json",  # '..'-сегмент
    "weird\\back.json",              # бэкслеш
    "",                              # пусто
    "nested/",                       # каталог
])
def test_is_allowed_import_member_rejects_junk_and_zip_slip(name):
    assert backup._is_allowed_import_member(name) is False


@pytest.mark.asyncio
async def test_restore_accepts_exact_archive_boundaries(backup_env):
    member_size = backup._IMPORT_MEMBER_MAX_BYTES
    members = {
        f"quarters/2026-Q{quarter}.json": _quarter_payload(
            member_size,
            f"2026-Q{quarter}",
        )
        for quarter in range(1, 5)
    }
    for index in range(backup._IMPORT_ARCHIVE_MAX_MEMBERS - len(members)):
        members[f"ignored-{index}.txt"] = "x"

    result = await backup.restore_backup_zip(_zip_bytes(members))

    assert len(result["restored"]) == 4
    assert len(result["skipped"]) == backup._IMPORT_ARCHIVE_MAX_MEMBERS - 4


@pytest.mark.asyncio
async def test_restore_rejects_too_many_members_before_publication(backup_env):
    members = {
        "subscribers.json": '{"subscribers": {"1": "Alice"}}',
        **{
            f"ignored-{index}.txt": "x"
            for index in range(backup._IMPORT_ARCHIVE_MAX_MEMBERS)
        },
    }

    with pytest.raises(ValueError, match="больше 256"):
        await backup.restore_backup_zip(_zip_bytes(members))

    assert not (backup_env / "subscribers.json").exists()


@pytest.mark.asyncio
async def test_restore_rejects_oversized_member_before_publication(backup_env):
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"1": "Alice"}}',
        "quarters/oversized.json": _quarter_payload(
            backup._IMPORT_MEMBER_MAX_BYTES + 1,
            "oversized",
        ),
    })

    with pytest.raises(ValueError, match="больше 8 МиБ"):
        await backup.restore_backup_zip(raw)

    assert not (backup_env / "subscribers.json").exists()


@pytest.mark.asyncio
async def test_restore_rejects_oversized_total_before_publication(backup_env):
    member_size = backup._IMPORT_MEMBER_MAX_BYTES
    members = {
        f"quarters/2026-Q{quarter}.json": _quarter_payload(
            member_size,
            f"2026-Q{quarter}",
        )
        for quarter in range(1, 5)
    }
    members["quarters/extra.json"] = _quarter_payload(1_024, "extra")

    with pytest.raises(ValueError, match="больше 32 МиБ"):
        await backup.restore_backup_zip(_zip_bytes(members))

    assert not list((backup_env / "quarters").glob("*.json"))


# ─────────────────────────────────────────────────────────────
#  Восстановление
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restore_round_trip(backup_env):
    raw = _zip_bytes({
        "blocked_users.json": '{"blocked_user_ids": [456]}',
        "subscribers.json": '{"subscribers": {"123": "Alice"}}',
        "stats_current.json": '{"period": "2026-Q2", "events": []}',
        "update_state.json": (
            '{"last_checked_at": null, "latest_main_version": "v1.3.0", '
            '"latest_version": "v1.2.0", '
            '"release_url": "https://release", "last_notified_version": "v1.2.0"}'
        ),
        "quarters/2026-Q1.json": '{"period": "2026-Q1"}',
        "seen_ids.json": '{"seen_ids": [1, 2, 3]}',   # должен быть отброшен
    })
    result = await backup.restore_backup_zip(raw)

    assert set(result["restored"]) == {
        "blocked_users.json",
        "subscribers.json",
        "stats_current.json",
        "update_state.json",
        "quarters/2026-Q1.json",
    }
    assert "seen_ids.json" in result["skipped"]
    # файлы реально записаны
    assert storage.load_subscribers() == {123: "Alice"}
    assert storage.load_blocked_users() == {456}
    assert storage.load_update_state()["last_notified_version"] == "v1.2.0"
    assert storage.load_update_state()["latest_main_version"] == "v1.3.0"
    assert (backup_env / "quarters" / "2026-Q1.json").exists()
    assert not (backup_env / "seen_ids.json").exists()


@pytest.mark.asyncio
async def test_restore_roundtrips_subscription_pending_schedule(backup_env):
    expected = storage.SubscriberState(
        {123: "Alice"},
        {
            "version": 1,
            "last_backup_at": 123.0,
            "weekly_started_at": 100.0,
            "pending": {
                "subscriptions": 3,
                "unsubscriptions": 1,
                "counts_known": True,
                "token": "restored-batch",
            },
        },
    )

    result = await backup.restore_backup_zip(
        _zip_bytes({"subscribers.json": storage.subscriber_state_json(expected)})
    )

    assert result["restored"] == ["subscribers.json"]
    restored = storage.load_subscriber_state(strict_subscribers=True)
    assert restored.subscribers == expected.subscribers
    assert restored.backup_schedule == expected.backup_schedule


@pytest.mark.asyncio
async def test_legacy_restore_migrates_weekly_anchor_from_stats_current(
    backup_env,
    monkeypatch,
):
    monkeypatch.setattr(backup.time, "time", lambda: 2_000_000_000.0)

    await backup.restore_backup_zip(
        _zip_bytes(
            {
                "subscribers.json": '{"subscribers": {"123": "Alice"}}',
                "stats_current.json": (
                    '{"period": "2026-Q2", "events": [], '
                    '"last_backup_at": 1900000000.0}'
                ),
            }
        )
    )

    schedule = storage.load_subscription_backup_state()
    assert schedule["last_backup_at"] is None
    assert schedule["weekly_started_at"] == 1_900_000_000.0
    assert schedule["pending"] is None


@pytest.mark.asyncio
async def test_restore_skips_corrupt_json(backup_env):
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"1": "Bob"}}',
        "stats_current.json": "{ это не json",   # битый — пропускаем
    })
    result = await backup.restore_backup_zip(raw)
    assert "subscribers.json" in result["restored"]
    assert "stats_current.json" in result["skipped"]
    assert not (backup_env / "stats_current.json").exists()


@pytest.mark.asyncio
async def test_restore_bad_zip_raises(backup_env):
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(b"this is not a zip")


@pytest.mark.asyncio
async def test_restore_skips_corrupt_crc_member(backup_env):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("subscribers.json", '{"subscribers": {"1": "Bob"}}')
        zf.writestr("stats_current.json", '{"period": "2026-Q2", "events": []}')
    raw = _corrupt_stored_member(buf.getvalue(), "subscribers.json")

    result = await backup.restore_backup_zip(raw)

    assert "subscribers.json" in result["skipped"]
    assert result["restored"] == ["stats_current.json"]
    assert not (backup_env / "subscribers.json").exists()
    assert json.loads(
        (backup_env / "stats_current.json").read_text(encoding="utf-8")
    ) == {"period": "2026-Q2", "events": []}


@pytest.mark.parametrize(
    "read_error",
    [
        RuntimeError("encrypted member"),
        NotImplementedError("unsupported compression"),
        OSError("read failed"),
        EOFError("truncated member"),
        zlib.error("invalid deflate stream"),
        lzma.LZMAError("invalid lzma stream"),
    ],
    ids=[
        "encrypted",
        "unsupported-compression",
        "os-error",
        "unexpected-eof",
        "deflate-error",
        "lzma-error",
    ],
)
@pytest.mark.asyncio
async def test_restore_skips_unreadable_zip_member(backup_env, monkeypatch, read_error):
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"1": "Bob"}}',
        "stats_current.json": '{"period": "2026-Q2", "events": []}',
    })
    real_read = zipfile.ZipFile.read

    def fail_selected_member(zf, name, *args, **kwargs):
        if name.filename == "subscribers.json":
            raise read_error
        return real_read(zf, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", fail_selected_member)

    result = await backup.restore_backup_zip(raw)

    assert "subscribers.json" in result["skipped"]
    assert result["restored"] == ["stats_current.json"]
    assert not (backup_env / "subscribers.json").exists()
    assert json.loads(
        (backup_env / "stats_current.json").read_text(encoding="utf-8")
    ) == {"period": "2026-Q2", "events": []}


@pytest.mark.parametrize("change", ["missing", "extra"])
@pytest.mark.asyncio
async def test_restore_rejects_inexact_update_state_schema(backup_env, change):
    state = {
        "last_checked_at": None,
        "latest_main_version": "v1.3.0",
        "latest_version": "v1.2.0",
        "release_url": "https://release",
        "last_notified_version": "v1.2.0",
    }
    if change == "missing":
        state.pop("release_url")
    else:
        state["unexpected"] = "value"

    raw = _zip_bytes({"update_state.json": json.dumps(state)})

    with pytest.raises(ValueError, match="нет валидных файлов"):
        await backup.restore_backup_zip(raw)
    assert not (backup_env / "update_state.json").exists()


@pytest.mark.parametrize(
    "key",
    [
        "last_checked_at",
        "latest_main_version",
        "latest_version",
        "release_url",
        "last_notified_version",
    ],
)
@pytest.mark.asyncio
async def test_restore_rejects_non_string_update_state_value(backup_env, key):
    state = {
        "last_checked_at": None,
        "latest_main_version": "v1.3.0",
        "latest_version": "v1.2.0",
        "release_url": "https://release",
        "last_notified_version": "v1.2.0",
    }
    state[key] = 42
    raw = _zip_bytes({"update_state.json": json.dumps(state)})

    with pytest.raises(ValueError, match="нет валидных файлов"):
        await backup.restore_backup_zip(raw)
    assert not (backup_env / "update_state.json").exists()


@pytest.mark.asyncio
async def test_restore_accepts_legacy_update_state_and_backfills_main(backup_env):
    legacy = {
        "last_checked_at": None,
        "latest_version": "v1.2.0",
        "release_url": "https://release",
        "last_notified_version": "v1.2.0",
    }

    result = await backup.restore_backup_zip(
        _zip_bytes({"update_state.json": json.dumps(legacy)})
    )

    assert result["restored"] == ["update_state.json"]
    state = storage.load_update_state()
    assert state["latest_main_version"] is None
    assert state["latest_version"] == "v1.2.0"


@pytest.mark.asyncio
async def test_restore_rolls_back_first_file_when_second_publish_fails(
    backup_env,
    monkeypatch,
):
    storage._atomic_write(
        backup_env / "subscribers.json",
        '{"subscribers": {"1": "Old"}}',
    )
    storage._atomic_write(
        backup_env / "stats_current.json",
        '{"period": "old", "events": []}',
    )
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"2": "New"}}',
        "stats_current.json": '{"period": "new", "events": []}',
    })
    real_publish = backup._publish_staged_file
    calls = 0

    def fail_second_publish(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk failure")
        real_publish(source, target)

    monkeypatch.setattr(backup, "_publish_staged_file", fail_second_publish)

    with pytest.raises(ValueError, match="исходное состояние восстановлено"):
        await backup.restore_backup_zip(raw)

    assert storage.load_subscribers() == {1: "Old"}
    assert json.loads((backup_env / "stats_current.json").read_text(encoding="utf-8")) == {
        "period": "old",
        "events": [],
    }


@pytest.mark.asyncio
async def test_restore_removes_new_file_when_second_publish_fails(
    backup_env,
    monkeypatch,
):
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"2": "New"}}',
        "stats_current.json": '{"period": "new", "events": []}',
    })
    real_publish = backup._publish_staged_file
    calls = 0

    def fail_second_publish(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk failure")
        real_publish(source, target)

    monkeypatch.setattr(backup, "_publish_staged_file", fail_second_publish)

    with pytest.raises(ValueError, match="исходное состояние восстановлено"):
        await backup.restore_backup_zip(raw)

    assert not (backup_env / "subscribers.json").exists()
    assert not (backup_env / "stats_current.json").exists()


@pytest.mark.asyncio
async def test_restore_no_valid_members_raises(backup_env):
    raw = _zip_bytes({"seen_ids.json": "{}", "junk.txt": "x"})
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(raw)


@pytest.mark.asyncio
async def test_restore_partial_corrupt_does_not_write_before_validation(backup_env):
    # битый stats_current не должен оставить полузаписанный файл
    raw = _zip_bytes({"stats_current.json": "{bad"})
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(raw)
    assert not (backup_env / "stats_current.json").exists()


@pytest.mark.asyncio
async def test_restore_roundtrips_known_users_and_alert_settings(backup_env):
    raw = _zip_bytes(
        {
            "known_users.json": _known_users_payload(),
            "user_alerts.json": '{"enabled": false}',
        }
    )

    result = await backup.restore_backup_zip(raw)

    assert set(result["restored"]) == {"known_users.json", "user_alerts.json"}
    assert storage.get_known_user(7) == storage.KnownUser(
        7,
        "Neo",
        "the_one",
        "2026-09-03T10:20:30Z",
    )
    assert storage.load_user_alerts_enabled() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("known_users.json", '{"users": []}'),
        ("user_alerts.json", '{"enabled": "yes"}'),
    ],
)
async def test_malformed_registry_restore_member_rejects_entire_candidate(
    backup_env,
    name,
    payload,
):
    storage.save_known_users(
        {
            8: storage.KnownUser(
                8,
                "Existing",
                None,
                "2026-09-03T10:20:30Z",
            )
        }
    )
    storage._atomic_write(backup_env / "stats_current.json", '{"period": "old", "events": []}')
    raw = _zip_bytes(
        {
            "stats_current.json": '{"period": "new", "events": []}',
            name: payload,
        }
    )

    with pytest.raises(ValueError, match=name):
        await backup.restore_backup_zip(raw)

    assert storage.get_known_user(8) is not None
    assert json.loads(
        (backup_env / "stats_current.json").read_text(encoding="utf-8")
    )["period"] == "old"


@pytest.mark.asyncio
async def test_restore_rolls_back_known_users_with_common_transaction(
    backup_env,
    monkeypatch,
):
    storage.save_known_users(
        {
            8: storage.KnownUser(
                8,
                "Existing",
                None,
                "2026-09-03T10:20:30Z",
            )
        }
    )
    storage._atomic_write(backup_env / "user_alerts.json", '{"enabled": true}')
    raw = _zip_bytes(
        {
            "known_users.json": _known_users_payload(7, "New"),
            "user_alerts.json": '{"enabled": false}',
        }
    )
    real_publish = backup._publish_staged_file
    calls = 0

    def fail_second_publish(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        real_publish(source, target)

    monkeypatch.setattr(backup, "_publish_staged_file", fail_second_publish)

    with pytest.raises(ValueError, match="исходное состояние восстановлено"):
        await backup.restore_backup_zip(raw)

    assert storage.get_known_user(8) is not None
    assert storage.get_known_user(7) is None
    assert storage.load_user_alerts_enabled() is True


# ─────────────────────────────────────────────────────────────
#  send_backup
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_backup_success_sends_to_owner_with_tag(backup_env):
    (backup_env / "subscribers.json").write_text('{"subscribers": {}}', encoding="utf-8")
    bot = AsyncMock()
    ok = await backup.send_backup(bot, f"тест {backup.BACKUP_TAG}")
    assert ok is True
    bot.send_document.assert_awaited_once()
    args, kwargs = bot.send_document.call_args
    assert args[0] == handlers.OWNER_ID                  # доставка владельцу
    assert backup.BACKUP_TAG in kwargs["caption"]
    assert isinstance(kwargs["document"], backup.BufferedInputFile)


@pytest.mark.asyncio
async def test_send_backup_swallows_send_errors(backup_env):
    bot = AsyncMock()
    bot.send_document.side_effect = RuntimeError("telegram down")
    ok = await backup.send_backup(bot, "x")
    assert ok is False   # сбой не пробрасывается


@pytest.mark.asyncio
async def test_send_backup_retries_transient_upload_with_fresh_file(
    backup_env,
    monkeypatch,
):
    build = Mock(return_value=b"zip-data")
    monkeypatch.setattr(backup, "_build_backup_zip", build)
    monkeypatch.setattr("telegram_delivery._sleep", AsyncMock())
    documents = []

    async def _send_document(*args, **kwargs):
        documents.append(kwargs["document"])
        if len(documents) == 1:
            raise aiohttp.ClientOSError(104, "Connection reset by peer")

    bot = AsyncMock()
    bot.send_document.side_effect = _send_document

    assert await backup.send_backup(bot, "x") is True
    build.assert_called_once_with()
    assert bot.send_document.await_count == 2
    assert documents[0] is not documents[1]
    assert backup._last_backup_sent_at is not None


@pytest.mark.asyncio
async def test_send_backup_exhausted_retries_do_not_advance_clock(
    backup_env,
    monkeypatch,
):
    monkeypatch.setattr("telegram_delivery._sleep", AsyncMock())
    bot = AsyncMock()
    bot.send_document.side_effect = aiohttp.ClientOSError(
        104,
        "Connection reset by peer",
    )

    assert await backup.send_backup(bot, "x") is False
    assert bot.send_document.await_count == 3
    assert backup._last_backup_sent_at is None


@pytest.mark.asyncio
async def test_send_backup_build_failure_is_not_retried(backup_env, monkeypatch):
    build = Mock(side_effect=OSError("archive failed"))
    monkeypatch.setattr(backup, "_build_backup_zip", build)
    bot = AsyncMock()

    assert await backup.send_backup(bot, "x") is False
    build.assert_called_once_with()
    bot.send_document.assert_not_awaited()


# ─────────────────────────────────────────────────────────────
#  Авто-бэкап на под/отписку
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_eligible_subscription_sends_and_clears_pending(
    backup_env,
    monkeypatch,
):
    await storage.mutate_subscription(7, "Neo", subscribed=True)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)
    bot = AsyncMock()

    assert await backup._backup_after_subscription(bot) is True

    sent.assert_awaited_once()
    caption = sent.call_args.args[1]
    assert "Подписок: <b>1</b>" in caption
    assert "Отписок: <b>0</b>" in caption
    assert "Сейчас подписчиков: <b>1</b>" in caption
    assert backup.BACKUP_TAG in caption
    schedule = storage.load_subscription_backup_state()
    assert isinstance(schedule["last_backup_at"], float)
    assert schedule["pending"] is None


@pytest.mark.asyncio
async def test_subscription_changes_aggregate_while_not_due(backup_env, monkeypatch):
    now = time.time()
    _save_subscriber_schedule(
        {5: "Trinity"},
        last_backup_at=now,
        weekly_started_at=now,
    )
    await storage.mutate_subscription(7, "Neo", subscribed=True)
    await storage.mutate_subscription(8, "Morpheus", subscribed=True)
    await storage.mutate_subscription(5, "Trinity", subscribed=False)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)

    assert await backup._backup_after_subscription(AsyncMock()) is False

    sent.assert_not_awaited()
    pending = storage.load_subscription_backup_state()["pending"]
    assert pending["subscriptions"] == 2
    assert pending["unsubscriptions"] == 1
    assert pending["counts_known"] is True


@pytest.mark.asyncio
async def test_failed_subscription_delivery_keeps_state_for_retry(
    backup_env,
    monkeypatch,
):
    await storage.mutate_subscription(7, "Neo", subscribed=True)
    sent = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr("backup.send_backup", sent)

    assert await backup._backup_after_subscription(AsyncMock()) is False
    failed = storage.load_subscription_backup_state()
    assert failed["last_backup_at"] is None
    assert failed["pending"]["subscriptions"] == 1

    assert await backup._backup_after_subscription(AsyncMock()) is True
    retried = storage.load_subscription_backup_state()
    assert retried["pending"] is None
    assert isinstance(retried["last_backup_at"], float)


@pytest.mark.asyncio
async def test_subscription_change_during_send_remains_pending(backup_env, monkeypatch):
    await storage.mutate_subscription(7, "Neo", subscribed=True)

    async def send_and_change(_bot, _caption):
        await storage.mutate_subscription(8, "Trinity", subscribed=True)
        return True

    monkeypatch.setattr("backup.send_backup", send_and_change)

    assert await backup._backup_after_subscription(AsyncMock()) is True

    state = storage.load_subscription_backup_state()
    assert isinstance(state["last_backup_at"], float)
    assert state["pending"]["subscriptions"] == 1
    assert state["pending"]["unsubscriptions"] == 0
    assert state["pending"]["counts_known"] is True


@pytest.mark.asyncio
async def test_subscription_send_does_not_hold_restorable_lock(backup_env, monkeypatch):
    await storage.mutate_subscription(7, "Neo", subscribed=True)

    async def send_while_locking(_bot, _caption):
        async with storage.restorable_state_transaction():
            return True

    monkeypatch.setattr("backup.send_backup", send_while_locking)

    assert await asyncio.wait_for(
        backup._backup_after_subscription(AsyncMock()),
        timeout=5,
    ) is True


@pytest.mark.asyncio
async def test_concurrent_subscription_delivery_sends_one_backup(
    backup_env,
    monkeypatch,
):
    await storage.mutate_subscription(7, "Neo", subscribed=True)
    started = asyncio.Event()
    release = asyncio.Event()

    async def send_once(_bot, _caption):
        started.set()
        await release.wait()
        return True

    sent = AsyncMock(side_effect=send_once)
    monkeypatch.setattr("backup.send_backup", sent)
    first = asyncio.create_task(backup._backup_after_subscription(AsyncMock()))
    await started.wait()
    second = asyncio.create_task(backup._backup_after_subscription(AsyncMock()))
    release.set()

    assert await asyncio.gather(first, second) == [True, False]
    sent.assert_awaited_once()


@pytest.mark.asyncio
async def test_subscription_pending_survives_restart_until_due(
    backup_env,
    monkeypatch,
):
    now = time.time()
    _save_subscriber_schedule(last_backup_at=now, weekly_started_at=now)
    await storage.mutate_subscription(7, "Neo", subscribed=True)
    monkeypatch.setattr(backup.time, "time", lambda: now + 60)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)

    assert await backup._backup_after_subscription(AsyncMock()) is False
    assert storage.load_subscriber_state().backup_schedule["pending"] is not None

    monkeypatch.setattr(
        backup.time,
        "time",
        lambda: now + backup.SUBSCRIPTION_BACKUP_INTERVAL,
    )
    assert await backup._backup_after_subscription(AsyncMock()) is True
    assert storage.load_subscriber_state().backup_schedule["pending"] is None


@pytest.mark.asyncio
async def test_restore_during_subscription_send_is_not_acknowledged(
    backup_env,
    monkeypatch,
):
    await storage.mutate_subscription(7, "Neo", subscribed=True)
    restored = storage.SubscriberState(
        {8: "Trinity"},
        {
            "version": 1,
            "last_backup_at": 100.0,
            "weekly_started_at": 100.0,
            "pending": {
                "subscriptions": 4,
                "unsubscriptions": 2,
                "counts_known": True,
                "token": "restored-batch",
            },
        },
    )

    async def send_and_restore(_bot, _caption):
        await backup.restore_backup_zip(
            _zip_bytes({"subscribers.json": storage.subscriber_state_json(restored)})
        )
        return True

    monkeypatch.setattr("backup.send_backup", send_and_restore)

    assert await backup._backup_after_subscription(AsyncMock()) is False
    state = storage.load_subscriber_state(strict_subscribers=True)
    assert state.subscribers == {8: "Trinity"}
    assert state.backup_schedule == restored.backup_schedule


@pytest.mark.asyncio
@pytest.mark.parametrize("last_backup_at", [None, 100.0, 2_000_000.0])
async def test_missing_stale_and_future_timestamp_make_pending_eligible(
    backup_env,
    monkeypatch,
    last_backup_at,
):
    now = 1_000_000.0
    pending = {
        "subscriptions": 1,
        "unsubscriptions": 0,
        "counts_known": True,
        "token": "batch",
    }
    _save_subscriber_schedule(
        {7: "Neo"},
        last_backup_at=last_backup_at,
        weekly_started_at=100.0,
        pending=pending,
    )
    monkeypatch.setattr(backup.time, "time", lambda: now)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)

    assert await backup._backup_after_subscription(AsyncMock()) is True
    sent.assert_awaited_once()
    assert storage.load_subscription_backup_state()["last_backup_at"] == now


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken_schedule",
    [
        {
            "version": 1,
            "last_backup_at": "yesterday",
            "weekly_started_at": 100.0,
            "pending": None,
        },
        {
            "version": 1,
            "last_backup_at": 100.0,
            "weekly_started_at": 100.0,
        },
        {
            "version": 1,
            "last_backup_at": 100.0,
            "weekly_started_at": 100.0,
            "pending": {"subscriptions": 1},
        },
    ],
)
async def test_malformed_schedule_sends_honest_recovery_backup(
    backup_env,
    monkeypatch,
    broken_schedule,
):
    storage.SUBS_FILE.write_text(
        json.dumps(
            {
                "subscribers": {"7": "Neo"},
                "backup_schedule": broken_schedule,
            }
        ),
        encoding="utf-8",
    )
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)

    assert await backup._backup_after_subscription(AsyncMock()) is True
    assert "Точные количества прошлых изменений недоступны" in sent.call_args.args[1]
    assert storage.load_subscription_backup_state()["pending"] is None


@pytest.mark.asyncio
async def test_missing_legacy_schedule_does_not_invent_pending_change(
    backup_env,
    monkeypatch,
):
    storage.SUBS_FILE.write_text(
        '{"subscribers": {"7": "Neo"}}',
        encoding="utf-8",
    )
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)

    assert await backup._backup_after_subscription(AsyncMock()) is False
    sent.assert_not_awaited()
    assert storage.load_subscriber_state(strict_subscribers=True).schedule_missing is False


# ─────────────────────────────────────────────────────────────
#  Еженедельный авто-бэкап
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_weekly_backup_first_time_sets_anchor_without_sending(
    backup_env,
    monkeypatch,
):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)
    cur = {"period": "2026-Q2", "events": []}   # нет last_backup_at
    storage.save_stats_current(cur)

    out = await backup._weekly_backup_if_due(AsyncMock(), cur)

    sent.assert_not_awaited()
    assert out is cur
    schedule = storage.load_subscription_backup_state()
    assert schedule["last_backup_at"] is None
    assert isinstance(schedule["weekly_started_at"], float)


@pytest.mark.asyncio
async def test_weekly_backup_not_due_does_nothing(backup_env, monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)
    ts = time.time()
    cur = {"period": "2026-Q2", "events": []}
    _save_subscriber_schedule(last_backup_at=ts, weekly_started_at=ts)

    out = await backup._weekly_backup_if_due(AsyncMock(), cur)

    sent.assert_not_awaited()
    assert out is cur
    assert storage.load_subscription_backup_state()["last_backup_at"] == ts


@pytest.mark.asyncio
async def test_weekly_backup_due_sends_and_updates(backup_env, monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)
    old = time.time() - backup.WEEKLY_BACKUP_INTERVAL - 100
    cur = {"period": "2026-Q2", "events": []}
    _save_subscriber_schedule(last_backup_at=old, weekly_started_at=old)

    out = await backup._weekly_backup_if_due(AsyncMock(), cur)

    sent.assert_awaited_once()
    assert out is cur
    assert storage.load_subscription_backup_state()["last_backup_at"] > old


@pytest.mark.asyncio
async def test_weekly_backup_due_send_fails_keeps_old_timestamp(backup_env, monkeypatch):
    monkeypatch.setattr("backup.send_backup", AsyncMock(return_value=False))
    old = time.time() - backup.WEEKLY_BACKUP_INTERVAL - 100
    cur = {"period": "2026-Q2", "events": []}
    _save_subscriber_schedule(last_backup_at=old, weekly_started_at=old)

    out = await backup._weekly_backup_if_due(AsyncMock(), cur)

    assert out is cur
    assert storage.load_subscription_backup_state()["last_backup_at"] == old


@pytest.mark.asyncio
async def test_subscription_success_prevents_immediate_weekly_duplicate(
    backup_env,
    monkeypatch,
):
    await storage.mutate_subscription(7, "Neo", subscribed=True)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)

    assert await backup._backup_after_subscription(AsyncMock()) is True
    await backup._weekly_backup_if_due(
        AsyncMock(),
        {"period": "2026-Q2", "events": []},
    )

    sent.assert_awaited_once()


# ─────────────────────────────────────────────────────────────
#  Бэкап при остановке (SIGTERM) + monotonic-метка для дебаунса
# ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_backup_clock(monkeypatch):
    """Сбрасываем monotonic-метку последнего бэкапа между тестами (изоляция)."""
    monkeypatch.setattr("backup._last_backup_sent_at", None)


@pytest.mark.asyncio
async def test_send_backup_sets_last_backup_clock(backup_env):
    bot = AsyncMock()
    assert backup._last_backup_sent_at is None
    await backup.send_backup(bot, f"x {backup.BACKUP_TAG}")
    assert isinstance(backup._last_backup_sent_at, float)


@pytest.mark.asyncio
async def test_manual_backup_does_not_change_automatic_schedule(backup_env):
    pending = {
        "subscriptions": 2,
        "unsubscriptions": 1,
        "counts_known": True,
        "token": "batch",
    }
    _save_subscriber_schedule(
        {7: "Neo"},
        last_backup_at=123.0,
        weekly_started_at=100.0,
        pending=pending,
    )
    before = storage.load_subscription_backup_state()

    assert await backup.send_backup(AsyncMock(), f"Вручную\n\n{backup.BACKUP_TAG}")

    assert storage.load_subscription_backup_state() == before


@pytest.mark.asyncio
async def test_shutdown_backup_sends_when_no_recent(backup_env, monkeypatch):
    _save_subscriber_schedule(
        last_backup_at=123.0,
        weekly_started_at=100.0,
        pending={
            "subscriptions": 1,
            "unsubscriptions": 0,
            "counts_known": True,
            "token": "batch",
        },
    )
    before = storage.load_subscription_backup_state()
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)
    monkeypatch.setattr("backup._last_backup_sent_at", None)
    await backup._shutdown_backup(AsyncMock())
    sent.assert_awaited_once()
    caption = sent.call_args.args[1]
    assert backup.BACKUP_TAG in caption
    assert "SIGTERM" in caption
    assert storage.load_subscription_backup_state() == before


@pytest.mark.asyncio
async def test_shutdown_backup_debounced_when_recent(backup_env, monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)
    monkeypatch.setattr("backup._last_backup_sent_at", time.monotonic())
    await backup._shutdown_backup(AsyncMock())
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_backup_timeout_is_swallowed(backup_env, monkeypatch):
    monkeypatch.setattr("backup._last_backup_sent_at", None)
    started = asyncio.Event()
    wait_for_calls = 0

    async def _slow(_bot, _caption):
        started.set()
        await asyncio.Event().wait()
        return True

    async def _cancel_on_timeout(awaitable, timeout):
        nonlocal wait_for_calls
        wait_for_calls += 1
        assert timeout == backup.SHUTDOWN_BACKUP_TIMEOUT
        await _cancel_after_started(awaitable, started)
        raise TimeoutError

    monkeypatch.setattr("backup.send_backup", _slow)
    monkeypatch.setattr(backup.asyncio, "wait_for", _cancel_on_timeout)
    await backup._shutdown_backup(AsyncMock())   # не должно бросить
    assert wait_for_calls == 1


@pytest.mark.asyncio
async def test_shutdown_backup_timeout_cancels_retry_sequence(backup_env, monkeypatch):
    monkeypatch.setattr("backup._last_backup_sent_at", None)
    first_attempt = asyncio.Event()
    wait_for_calls = 0

    async def _fail_send(*args, **kwargs):
        first_attempt.set()
        raise aiohttp.ClientOSError(104, "Connection reset by peer")

    async def _cancel_on_timeout(awaitable, timeout):
        nonlocal wait_for_calls
        wait_for_calls += 1
        assert timeout == backup.SHUTDOWN_BACKUP_TIMEOUT
        await _cancel_after_started(awaitable, first_attempt)
        raise TimeoutError

    bot = AsyncMock()
    bot.send_document.side_effect = _fail_send
    monkeypatch.setattr(backup.asyncio, "wait_for", _cancel_on_timeout)

    await backup._shutdown_backup(bot)

    assert wait_for_calls == 1
    bot.send_document.assert_awaited_once()


# ─────────────────────────────────────────────────────────────
#  Проверка СТРУКТУРЫ при импорте (не только well-formed JSON)
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    '{"foo": "bar"}',                # нет ключа subscribers
    '[1, 2, 3]',                     # список вместо объекта (роняет load_subscribers)
    '{"subscribers": [1, 2, 3]}',    # subscribers не словарь
    '{"subscribers": {"abc": "x"}}', # ключ не приводится к int (не chat_id)
])
@pytest.mark.asyncio
async def test_restore_rejects_malformed_subscribers(backup_env, payload):
    raw = _zip_bytes({"subscribers.json": payload})
    with pytest.raises(ValueError):          # единственный файл невалиден → нечего восстанавливать
        await backup.restore_backup_zip(raw)
    assert not (backup_env / "subscribers.json").exists()


@pytest.mark.asyncio
async def test_restore_rejects_malformed_current_backup_schedule(backup_env):
    current = storage.SubscriberState(
        {1: "Current"},
        {
            "version": 1,
            "last_backup_at": 100.0,
            "weekly_started_at": 100.0,
            "pending": None,
        },
    )
    storage.save_subscriber_state(current)
    before = storage.SUBS_FILE.read_bytes()
    malformed = json.dumps(
        {
            "subscribers": {"2": "Restored"},
            "backup_schedule": {
                "version": 1,
                "last_backup_at": 100.0,
                "weekly_started_at": 100.0,
                "pending": {"subscriptions": 1},
            },
        }
    )

    with pytest.raises(storage.SubscriptionBackupStateError):
        await backup.restore_backup_zip(
            _zip_bytes({"subscribers.json": malformed})
        )

    assert storage.SUBS_FILE.read_bytes() == before


@pytest.mark.asyncio
async def test_restore_skips_bad_shape_keeps_good(backup_env):
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"5": "Ok"}}',
        "stats_current.json": '{"period": "2026-Q2"}',   # нет events-списка → пропуск
    })
    result = await backup.restore_backup_zip(raw)
    assert "subscribers.json" in result["restored"]
    assert "stats_current.json" in result["skipped"]
    assert not (backup_env / "stats_current.json").exists()


@pytest.mark.asyncio
async def test_restore_rejects_quarter_without_period(backup_env):
    raw = _zip_bytes({"quarters/x.json": '{"events": []}'})
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(raw)


def test_valid_import_payload_accepts_canonical_shapes():
    assert backup._valid_import_payload(
        "blocked_users.json",
        {"blocked_user_ids": [1]},
    )
    assert backup._valid_import_payload("subscribers.json", {"subscribers": {"1": "A"}})
    assert backup._valid_import_payload("stats_current.json", {"period": "2026-Q2", "events": []})
    assert backup._valid_import_payload("quarters/2026-Q1.json", {"period": "2026-Q1"})


# ─────────────────────────────────────────────────────────────
#  Замечание ревью: вложенный каталог quarters/ создаётся на свежем томе.
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_creates_missing_quarters_dir(backup_env):
    # эмулируем свежий том: каталога quarters/ ещё нет (кейс из «HIGH RISK» Codacy)
    import shutil
    shutil.rmtree(backup_env / "quarters")
    assert not (backup_env / "quarters").exists()
    raw = _zip_bytes({"quarters/2026-Q1.json": '{"period": "2026-Q1"}'})
    result = await backup.restore_backup_zip(raw)
    assert "quarters/2026-Q1.json" in result["restored"]
    # _atomic_write сам создаёт parent — краша на свежем томе нет
    assert (backup_env / "quarters" / "2026-Q1.json").exists()


# ─────────────────────────────────────────────────────────────
#  Список блокировок в полном кандидате восстановления
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "payload",
    [
        '{"blocked_user_ids": [999]}',
        '{"blocked_user_ids": [7, 7]}',
        '{"blocked_user_ids": [true]}',
        '{"blocked_user_ids": [-1]}',
        '{"blocked_user_ids": "7"}',
        '{"blocked_user_ids": [], "extra": 1}',
    ],
)
@pytest.mark.asyncio
async def test_restore_rejects_malformed_or_owner_blocked_users(backup_env, payload):
    with pytest.raises(ValueError, match="нет валидных файлов"):
        await backup.restore_backup_zip(_zip_bytes({"blocked_users.json": payload}))

    assert not (backup_env / "blocked_users.json").exists()


@pytest.mark.asyncio
async def test_restore_does_not_publish_subscribers_with_invalid_archive_blocked_users(
    backup_env,
):
    raw = _zip_bytes({
        "blocked_users.json": '{"blocked_user_ids": [7, 7]}',
        "subscribers.json": '{"subscribers": {"7": "Must stay blocked"}}',
    })

    with pytest.raises(ValueError, match="подписчики не восстановлены"):
        await backup.restore_backup_zip(raw)

    assert not (backup_env / "subscribers.json").exists()


@pytest.mark.asyncio
async def test_restore_filters_subscribers_against_candidate_blocked_users(backup_env):
    raw = _zip_bytes({
        "blocked_users.json": '{"blocked_user_ids": [7]}',
        "subscribers.json": '{"subscribers": {"7": "Blocked", "8": "Allowed"}}',
    })

    result = await backup.restore_backup_zip(raw)

    assert set(result["restored"]) == {"blocked_users.json", "subscribers.json"}
    assert storage.load_blocked_users() == {7}
    assert storage.load_subscribers() == {8: "Allowed"}


@pytest.mark.asyncio
async def test_restore_blocked_users_alone_removes_current_subscriber(backup_env):
    storage.save_subscribers({7: "Blocked", 8: "Allowed"})

    result = await backup.restore_backup_zip(
        _zip_bytes({"blocked_users.json": '{"blocked_user_ids": [7]}'})
    )

    assert set(result["restored"]) == {"blocked_users.json", "subscribers.json"}
    assert storage.load_subscribers() == {8: "Allowed"}


@pytest.mark.asyncio
async def test_restore_subscribers_alone_respects_current_blocked_users(backup_env):
    storage.save_blocked_users({7})

    result = await backup.restore_backup_zip(
        _zip_bytes({
            "subscribers.json": '{"subscribers": {"7": "Blocked", "8": "Allowed"}}'
        })
    )

    assert result["restored"] == ["subscribers.json"]
    assert storage.load_blocked_users() == {7}
    assert storage.load_subscribers() == {8: "Allowed"}


@pytest.mark.asyncio
async def test_restore_subscribers_fails_closed_when_current_blocked_users_are_corrupt(
    backup_env,
):
    storage._atomic_write(backup_env / "blocked_users.json", "{broken")

    with pytest.raises(ValueError, match="сначала восстанови blocked_users.json"):
        await backup.restore_backup_zip(
            _zip_bytes({"subscribers.json": '{"subscribers": {"8": "Allowed"}}'})
        )

    assert not (backup_env / "subscribers.json").exists()


@pytest.mark.asyncio
async def test_restore_rolls_back_blocked_users_if_subscriber_publication_fails(
    backup_env,
    monkeypatch,
):
    storage.save_blocked_users({1})
    storage.save_subscribers({2: "Old"})
    raw = _zip_bytes({
        "blocked_users.json": '{"blocked_user_ids": [7]}',
        "subscribers.json": '{"subscribers": {"8": "New"}}',
    })
    real_publish = backup._publish_staged_file
    calls = 0

    def fail_second_publish(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk failure")
        real_publish(source, target)

    monkeypatch.setattr(backup, "_publish_staged_file", fail_second_publish)

    with pytest.raises(ValueError, match="исходное состояние восстановлено"):
        await backup.restore_backup_zip(raw)

    assert storage.load_blocked_users() == {1}
    assert storage.load_subscribers() == {2: "Old"}


def _facts_payload(fact_id: str | None, *, version="backup-test") -> str:
    facts = [] if fact_id is None else [{"id": fact_id, "text": "Факт из архива."}]
    return json.dumps(
        {
            "schema_version": 1,
            "bank_version": version if facts else None,
            "facts": facts,
        },
        ensure_ascii=False,
    )


def test_backup_export_includes_facts_json(backup_env):
    (backup_env / "facts.json").write_text(
        _facts_payload("exported-fact"),
        encoding="utf-8",
    )

    names = set(zipfile.ZipFile(io.BytesIO(backup._build_backup_zip())).namelist())

    assert "facts.json" in names
    assert backup._is_allowed_import_member("facts.json") is True


@pytest.mark.asyncio
async def test_valid_fact_restore_is_canonical_and_immediately_active(backup_env):
    result = await backup.restore_backup_zip(
        _zip_bytes({"facts.json": _facts_payload("restored-fact")})
    )

    assert result == {"restored": ["facts.json"], "skipped": []}
    snapshot = fact_bank.get_fact_bank_snapshot()
    assert [fact.id for fact in snapshot.additional_facts] == ["restored-fact"]
    assert (backup_env / "facts.json").read_text(encoding="utf-8") == (
        fact_bank.canonical_active_fact_bank()
    )


@pytest.mark.asyncio
async def test_valid_empty_fact_restore_activates_base_only_state(backup_env):
    current = fact_bank.parse_fact_bank_bytes(
        _facts_payload("current-fact").encode("utf-8")
    )
    fact_bank.activate_restored_fact_bank(current)

    await backup.restore_backup_zip(
        _zip_bytes({"facts.json": _facts_payload(None)})
    )

    snapshot = fact_bank.get_fact_bank_snapshot()
    assert snapshot.additional_facts == ()
    assert snapshot.file_state == fact_bank.FACT_FILE_VALID


@pytest.mark.asyncio
async def test_invalid_fact_restore_rejects_entire_candidate_without_changes(backup_env):
    storage.save_stats_current({"period": "2026-Q1", "events": []})
    current = fact_bank.parse_fact_bank_bytes(
        _facts_payload("current-fact").encode("utf-8")
    )
    fact_bank._atomic_write(backup_env / "facts.json", fact_bank.serialize_fact_bank(current))
    before = fact_bank.reload_fact_bank()

    raw = _zip_bytes({
        "stats_current.json": '{"period": "2026-Q2", "events": []}',
        "facts.json": '{"schema_version": 99, "bank_version": null, "facts": []}',
    })
    with pytest.raises(ValueError, match="facts.json"):
        await backup.restore_backup_zip(raw)

    assert storage.load_stats_current()["period"] == "2026-Q1"
    assert fact_bank.get_fact_bank_snapshot() == before
    assert "current-fact" in (backup_env / "facts.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_fact_restore_propagates_configuration_failure(backup_env, monkeypatch):
    with monkeypatch.context() as fact_config:
        fact_config.setattr(fact_bank, "_base_facts", ())
        with pytest.raises(RuntimeError, match="ещё не настроена"):
            await backup.restore_backup_zip(
                _zip_bytes({"facts.json": _facts_payload("candidate-fact")})
            )


@pytest.mark.asyncio
async def test_legacy_archive_without_facts_leaves_current_bank_unchanged(backup_env):
    current = fact_bank.parse_fact_bank_bytes(
        _facts_payload("current-fact").encode("utf-8")
    )
    fact_bank._atomic_write(backup_env / "facts.json", fact_bank.serialize_fact_bank(current))
    before = fact_bank.reload_fact_bank()

    result = await backup.restore_backup_zip(
        _zip_bytes({"stats_current.json": '{"period": "2026-Q2", "events": []}'})
    )

    assert result["restored"] == ["stats_current.json"]
    assert fact_bank.get_fact_bank_snapshot() == before


@pytest.mark.asyncio
async def test_fact_snapshot_is_unchanged_when_restore_publication_rolls_back(
    backup_env,
    monkeypatch,
):
    current = fact_bank.parse_fact_bank_bytes(
        _facts_payload("current-fact").encode("utf-8")
    )
    fact_bank._atomic_write(backup_env / "facts.json", fact_bank.serialize_fact_bank(current))
    before = fact_bank.reload_fact_bank()
    raw = _zip_bytes({
        "facts.json": _facts_payload("candidate-fact"),
        "stats_current.json": '{"period": "2026-Q2", "events": []}',
    })
    real_publish = backup._publish_staged_file
    calls = 0

    def fail_second_publish(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk failure")
        real_publish(source, target)

    monkeypatch.setattr(backup, "_publish_staged_file", fail_second_publish)

    with pytest.raises(ValueError, match="исходное состояние восстановлено"):
        await backup.restore_backup_zip(raw)

    assert fact_bank.get_fact_bank_snapshot() == before
    assert "current-fact" in (backup_env / "facts.json").read_text(encoding="utf-8")
