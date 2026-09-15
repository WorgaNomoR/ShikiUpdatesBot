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
import threading
import time
import zipfile
import zlib
from unittest.mock import (
    AsyncMock,
    Mock,
)
from uuid import uuid4

import aiohttp
import pytest

import backup
import fact_bank
import handlers
import storage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        "legacy_pending",
        "legacy_complete",
        "current_partial",
        "current_complete",
        "rich_partial",
        "rich_complete",
        "empty",
    ],
)
async def test_import_roundtrips_supported_quarter_delivery_plans(backup_env, schema):
    cur = storage._empty_stats_current("2026-Q3")
    if schema.startswith("legacy"):
        pending = {
            "old_period": "2026-Q2", "new_period": "2026-Q3",
            "report_messages": ["frozen first", "frozen second"],
            "report_sent": schema == "legacy_complete",
        }
    elif schema.startswith("rich"):
        units = [
            {
                "transport": "rich",
                "content": {
                    "blocks": [{"type": "paragraph", "text": "frozen rich"}],
                    "skip_entity_detection": True,
                },
                "fallback_html": ["frozen HTML"],
                "fallback_disable_preview": False,
            },
            {
                "transport": "html",
                "content": "frozen continuation",
                "disable_preview": False,
            },
        ]
        pending = storage.new_quarter_delivery_plan(
            "2026-Q2",
            "2026-Q3",
            units,
        )
        pending["next_unit"] = 1 if schema == "rich_partial" else 2
    else:
        pending = storage.new_quarter_delivery(
            "2026-Q2", "2026-Q3", [] if schema == "empty" else ["frozen first", "frozen second"],
        )
        pending["next_unit"] = {"current_partial": 1, "current_complete": 2, "empty": 0}[schema]
    cur["pending_quarter_delivery"] = pending
    generation = storage.restorable_restore_generation()
    result = await backup.restore_backup_zip(_zip_bytes({"stats_current.json": json.dumps(cur)}))
    assert result["restored"] == ["stats_current.json"]
    assert storage.load_stats_current(strict=True) == cur
    assert storage.restorable_restore_generation() == generation + 1
    # Экспорт сохраняет ту же схему и frozen payload, не создаёт новый план.
    payload, _ = await backup._build_backup_zip()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert json.loads(archive.read("stats_current.json")) == cur


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["unknown_version", "progress", "messages", "lineage", "events", "legacy"])
async def test_malformed_quarter_import_rejects_entire_candidate(backup_env, damage, caplog):
    caplog.set_level("INFO", logger="shikiupdatesbot")
    current = storage._empty_stats_current("2026-Q2")
    storage.save_stats_current(current, strict=True)
    cur = storage._empty_stats_current("2026-Q3")
    cur["pending_quarter_delivery"] = storage.new_quarter_delivery("2026-Q2", "2026-Q3", ["private report"])
    pending = cur["pending_quarter_delivery"]
    if damage == "unknown_version":
        pending["version"] = 99
    elif damage == "progress":
        pending["next_unit"] = True
    elif damage == "messages":
        pending["report_messages"] = [""]
    elif damage == "lineage":
        cur["period"] = "2026-Q4"
    elif damage == "events":
        cur.pop("events")
    else:
        cur["pending_quarter_delivery"] = {
            "old_period": "2026-Q2", "new_period": "2026-Q3",
            "report_messages": [None], "report_sent": False,
        }
    generation = storage.restorable_restore_generation()
    with pytest.raises(storage.QuarterDeliveryStateError) as excinfo:
        await backup.restore_backup_zip(_zip_bytes({
            "quarters/2026-Q1.json": '{"period": "2026-Q1"}',
            "stats_current.json": json.dumps(cur),
        }))
    assert "private report" not in str(excinfo.value)
    assert "private report" not in caplog.text
    assert storage.load_stats_current() == current
    assert not (backup_env / "quarters" / "2026-Q1.json").exists()
    assert storage.restorable_restore_generation() == generation


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["old", "2026-Q2 "])
async def test_import_rejects_invalid_period_without_pending_before_publication(backup_env, period):
    current = storage._empty_stats_current("2026-Q2")
    storage.save_stats_current(current)
    generation = storage.restorable_restore_generation()
    with pytest.raises(storage.QuarterDeliveryStateError, match="^period_format$"):
        await backup.restore_backup_zip(_zip_bytes({
            "quarters/2026-Q1.json": '{"period": "2026-Q1"}',
            "stats_current.json": json.dumps({"period": period, "events": []}),
        }))
    assert storage.load_stats_current() == current
    assert not (backup_env / "quarters" / "2026-Q1.json").exists()
    assert storage.restorable_restore_generation() == generation

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


def _backup_worker_threads() -> list[threading.Thread]:
    """Вернуть только worker-потоки текущего backup pipeline."""
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("shikibot-backup")
    ]


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

@pytest.mark.asyncio
async def test_build_backup_zip_excludes_tmp_and_keeps_structure(backup_env):
    (backup_env / "subscribers.json").write_text('{"subscribers": {}}', encoding="utf-8")
    (backup_env / "blocked_users.json").write_text(
        '{"blocked_user_ids": [7]}',
        encoding="utf-8",
    )
    (backup_env / "stats_current.json").write_text('{"period": "2026-Q2"}', encoding="utf-8")
    (backup_env / "stats_all.json").write_text(
        '{"anime": {"titles": {"1": {"comment": "заметка"}}}, "manga": {}}',
        encoding="utf-8",
    )
    (backup_env / "known_users.json").write_text(
        _known_users_payload(),
        encoding="utf-8",
    )
    (backup_env / "user_alerts.json").write_text('{"enabled": false}', encoding="utf-8")
    (backup_env / "subscribers.json.tmp").write_text("garbage", encoding="utf-8")
    restore_stage = backup_env / ".restore-interrupted.tmp" / "new"
    storage._atomic_write(restore_stage / "subscribers.json", "staged")
    (backup_env / "quarters" / "2026-Q1.json").write_text('{"period": "2026-Q1"}', encoding="utf-8")

    raw, _ = await backup._build_backup_zip()
    names = set(zipfile.ZipFile(io.BytesIO(raw)).namelist())

    assert "subscribers.json" in names
    assert "blocked_users.json" in names
    assert "stats_current.json" in names
    assert "stats_all.json" in names
    assert "known_users.json" in names
    assert "user_alerts.json" in names
    assert "quarters/2026-Q1.json" in names          # вложенность сохранена
    assert "subscribers.json.tmp" not in names       # *.tmp исключён
    assert not any(name.startswith(".restore-") for name in names)


@pytest.mark.asyncio
async def test_slow_restorable_capture_keeps_event_loop_live_and_blocks_writer(
    backup_env,
    monkeypatch,
):
    old = b'{"period":"2026-Q2","events":[]}'
    new = '{"period":"2026-Q2","events":[{"id":"new"}]}'
    (backup_env / "stats_current.json").write_bytes(old)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_read = backup._read_backup_members

    def slow_read(cancelled, manifest, initial_total):
        if any(member.restorable for member in manifest):
            loop.call_soon_threadsafe(started.set)
            while not release.wait(0.005):
                backup._raise_if_backup_cancelled(cancelled)
        return real_read(cancelled, manifest, initial_total)

    monkeypatch.setattr(backup, "_read_backup_members", slow_read)
    build_task = asyncio.create_task(backup._build_backup_zip())
    await started.wait()

    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0)
        ticks += 1

    async def publish_new_state():
        async with storage.restorable_state_transaction():
            storage._atomic_write(backup_env / "stats_current.json", new)

    writer = asyncio.create_task(publish_new_state())
    await asyncio.sleep(0.02)
    assert ticks == 5
    assert writer.done() is False

    release.set()
    raw, _ = await build_task
    await writer

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("stats_current.json") == old
    assert (backup_env / "stats_current.json").read_text(encoding="utf-8") == new


@pytest.mark.asyncio
async def test_first_run_stats_current_waits_for_snapshot_transaction(
    backup_env,
    monkeypatch,
):
    (backup_env / "blocked_users.json").write_text(
        '{"blocked_user_ids":[]}',
        encoding="utf-8",
    )
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_read = backup._read_backup_members

    def slow_read(cancelled, manifest, initial_total):
        if any(member.restorable for member in manifest):
            loop.call_soon_threadsafe(started.set)
            while not release.wait(0.005):
                backup._raise_if_backup_cancelled(cancelled)
        return real_read(cancelled, manifest, initial_total)

    monkeypatch.setattr(backup, "_read_backup_members", slow_read)
    build_task = asyncio.create_task(backup._build_backup_zip())
    await started.wait()
    initialization = asyncio.create_task(
        handlers._load_stats_current_transactional()
    )
    await asyncio.sleep(0.02)

    assert initialization.done() is False
    assert (backup_env / "stats_current.json").exists() is False

    release.set()
    await build_task
    current = await initialization

    assert current["period"]
    assert (backup_env / "stats_current.json").is_file()


@pytest.mark.asyncio
async def test_slow_compression_releases_lock_and_keeps_coherent_snapshot(
    backup_env,
    monkeypatch,
):
    old = b'{"period":"2026-Q2","events":[]}'
    new = '{"period":"2026-Q2","events":[{"id":"new"}]}'
    (backup_env / "stats_current.json").write_bytes(old)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_compress = backup._compress_backup_zip

    def slow_compress(cancelled, members):
        loop.call_soon_threadsafe(started.set)
        while not release.wait(0.005):
            backup._raise_if_backup_cancelled(cancelled)
        return real_compress(cancelled, members)

    monkeypatch.setattr(backup, "_compress_backup_zip", slow_compress)
    build_task = asyncio.create_task(backup._build_backup_zip())
    await started.wait()

    async with storage.restorable_state_transaction():
        storage._atomic_write(backup_env / "stats_current.json", new)
    await asyncio.sleep(0)
    assert build_task.done() is False

    release.set()
    raw, _ = await build_task

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("stats_current.json") == old
    assert (backup_env / "stats_current.json").read_text(encoding="utf-8") == new


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["capture", "compression"])
async def test_backup_cancellation_drains_worker(
    backup_env,
    monkeypatch,
    stage,
):
    (backup_env / "stats_current.json").write_text(
        '{"period":"2026-Q2","events":[]}',
        encoding="utf-8",
    )
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def wait_for_cancel(cancelled, *args):
        loop.call_soon_threadsafe(started.set)
        while not cancelled.wait(0.005):
            pass
        backup._raise_if_backup_cancelled(cancelled)

    target = "_read_backup_members" if stage == "capture" else "_compress_backup_zip"
    monkeypatch.setattr(backup, target, wait_for_cancel)
    task = asyncio.create_task(backup._build_backup_zip())
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _backup_worker_threads() == []


@pytest.mark.asyncio
async def test_backup_cancelled_before_start_schedules_no_worker(backup_env, monkeypatch):
    scan = Mock()
    monkeypatch.setattr(backup, "_scan_backup_manifest", scan)
    task = asyncio.create_task(backup._build_backup_zip())
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    scan.assert_not_called()
    assert _backup_worker_threads() == []


@pytest.mark.asyncio
async def test_restore_during_compression_invalidates_snapshot_before_upload(
    backup_env,
    monkeypatch,
):
    storage.save_update_state(storage._empty_update_state())
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_compress = backup._compress_backup_zip

    def slow_compress(cancelled, members):
        loop.call_soon_threadsafe(started.set)
        while not release.wait(0.005):
            backup._raise_if_backup_cancelled(cancelled)
        return real_compress(cancelled, members)

    monkeypatch.setattr(backup, "_compress_backup_zip", slow_compress)
    bot = AsyncMock()
    send_task = asyncio.create_task(backup.send_backup(bot, "x"))
    await started.wait()
    generation = storage.restorable_restore_generation()

    await backup.restore_backup_zip(
        _zip_bytes({
            "update_state.json": json.dumps(storage._empty_update_state()),
        })
    )
    release.set()

    assert await send_task is False
    assert storage.restorable_restore_generation() == generation + 1
    bot.send_document.assert_not_awaited()
    assert _backup_worker_threads() == []


@pytest.mark.asyncio
async def test_backup_resource_limits_are_inclusive(backup_env, monkeypatch):
    monkeypatch.setattr(backup, "_BACKUP_ARCHIVE_MAX_MEMBERS", 2)
    monkeypatch.setattr(backup, "_BACKUP_RESTORABLE_MEMBER_MAX_BYTES", 4)
    monkeypatch.setattr(backup, "_BACKUP_TOTAL_MAX_BYTES", 8)
    (backup_env / "blocked_users.json").write_bytes(b"1234")
    (backup_env / "stats_all.json").write_bytes(b"5678")

    raw, _ = await backup._build_backup_zip()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("blocked_users.json") == b"1234"
        assert archive.read("stats_all.json") == b"5678"

    (backup_env / "extra.json").write_bytes(b"x")
    with pytest.raises(ValueError, match="больше 2 файлов"):
        await backup._build_backup_zip()

    (backup_env / "extra.json").unlink()
    (backup_env / "blocked_users.json").write_bytes(b"12345")
    with pytest.raises(ValueError, match="восстанавливаемый файл больше"):
        await backup._build_backup_zip()

    (backup_env / "blocked_users.json").write_bytes(b"1234")
    (backup_env / "stats_all.json").write_bytes(b"56789")
    with pytest.raises(ValueError, match="суммарный размер backup больше"):
        await backup._build_backup_zip()


def test_completed_zip_limit_is_inclusive(monkeypatch):
    cancelled = threading.Event()
    members = (backup._BackupMember("payload.bin", bytes(range(256)) * 16),)
    monkeypatch.setattr(backup, "_BACKUP_ZIP_MAX_BYTES", 1024 * 1024)
    raw = backup._compress_backup_zip(cancelled, members)

    monkeypatch.setattr(backup, "_BACKUP_ZIP_MAX_BYTES", len(raw))
    assert len(backup._compress_backup_zip(cancelled, members)) == len(raw)

    monkeypatch.setattr(backup, "_BACKUP_ZIP_MAX_BYTES", len(raw) - 1)
    with pytest.raises(ValueError, match="готовый backup ZIP больше"):
        backup._compress_backup_zip(cancelled, members)


def test_export_limits_match_import_and_telegram_boundaries():
    assert backup._BACKUP_ARCHIVE_MAX_MEMBERS == backup._IMPORT_ARCHIVE_MAX_MEMBERS
    assert (
        backup._BACKUP_RESTORABLE_MEMBER_MAX_BYTES
        == backup._IMPORT_MEMBER_MAX_BYTES
    )
    assert backup._BACKUP_TOTAL_MAX_BYTES == backup._IMPORT_TOTAL_MAX_BYTES
    assert backup._BACKUP_ZIP_MAX_BYTES == backup.IMPORT_DOCUMENT_MAX_BYTES


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
async def test_restore_skips_exported_stats_all_and_keeps_current_file(backup_env):
    current = '{"anime": {"titles": {"1": {"comment": "текущий"}}}, "manga": {}}'
    (backup_env / "stats_all.json").write_text(current, encoding="utf-8")
    raw = _zip_bytes({
        "stats_all.json": '{"anime": {"titles": {"1": {"comment": "архив"}}}}',
        "stats_current.json": '{"period": "2026-Q2", "events": []}',
    })

    result = await backup.restore_backup_zip(raw)

    assert result["restored"] == ["stats_current.json"]
    assert result["skipped"] == ["stats_all.json"]
    assert (backup_env / "stats_all.json").read_text(encoding="utf-8") == current


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
                "token": uuid4().hex,
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
        '{"period": "2026-Q1", "events": []}',
    )
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"2": "New"}}',
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

    assert storage.load_subscribers() == {1: "Old"}
    assert json.loads((backup_env / "stats_current.json").read_text(encoding="utf-8")) == {
        "period": "2026-Q1",
        "events": [],
    }


@pytest.mark.asyncio
async def test_restore_removes_new_file_when_second_publish_fails(
    backup_env,
    monkeypatch,
):
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"2": "New"}}',
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
    storage._atomic_write(backup_env / "stats_current.json", '{"period": "2026-Q1", "events": []}')
    raw = _zip_bytes(
        {
            "stats_current.json": '{"period": "2026-Q2", "events": []}',
            name: payload,
        }
    )

    with pytest.raises(ValueError, match=name):
        await backup.restore_backup_zip(raw)

    assert storage.get_known_user(8) is not None
    assert json.loads(
        (backup_env / "stats_current.json").read_text(encoding="utf-8")
    )["period"] == "2026-Q1"


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
    build = AsyncMock(
        return_value=(b"zip-data", storage.restorable_restore_generation())
    )
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
    build.assert_awaited_once_with()
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
    build = AsyncMock(side_effect=OSError("archive failed"))
    monkeypatch.setattr(backup, "_build_backup_zip", build)
    bot = AsyncMock()

    assert await backup.send_backup(bot, "x") is False
    build.assert_awaited_once_with()
    bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_and_retry_sleep_do_not_hold_restorable_lock(
    backup_env,
    monkeypatch,
):
    (backup_env / "stats_current.json").write_text(
        '{"period":"2026-Q2","events":[]}',
        encoding="utf-8",
    )
    attempts = 0

    async def send_document(*args, **kwargs):
        nonlocal attempts
        async with storage.restorable_state_transaction():
            pass
        attempts += 1
        if attempts == 1:
            raise aiohttp.ClientOSError(104, "Connection reset by peer")

    async def retry_sleep(_delay):
        async with storage.restorable_state_transaction():
            pass

    bot = AsyncMock()
    bot.send_document.side_effect = send_document
    monkeypatch.setattr("telegram_delivery._sleep", retry_sleep)

    assert await asyncio.wait_for(backup.send_backup(bot, "x"), timeout=5) is True
    assert attempts == 2


@pytest.mark.asyncio
async def test_restore_during_upload_invalidates_confirmed_send(
    backup_env,
):
    storage.save_update_state(storage._empty_update_state())

    async def send_document(*args, **kwargs):
        await backup.restore_backup_zip(
            _zip_bytes({
                "update_state.json": json.dumps(storage._empty_update_state()),
            })
        )

    bot = AsyncMock()
    bot.send_document.side_effect = send_document

    assert await backup.send_backup(bot, "x") is False
    bot.send_document.assert_awaited_once()
    assert backup._last_backup_sent_at is None


@pytest.mark.asyncio
async def test_cancellation_during_upload_has_no_worker_or_acknowledgement(backup_env):
    (backup_env / "stats_current.json").write_text(
        '{"period":"2026-Q2","events":[]}',
        encoding="utf-8",
    )
    started = asyncio.Event()

    async def send_document(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    bot = AsyncMock()
    bot.send_document.side_effect = send_document
    task = asyncio.create_task(backup.send_backup(bot, "x"))
    await started.wait()
    assert _backup_worker_threads() == []

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert backup._last_backup_sent_at is None
    assert _backup_worker_threads() == []


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
async def test_subscription_caller_builds_and_sends_real_archive(backup_env):
    await storage.mutate_subscription(7, "Neo", subscribed=True)
    bot = AsyncMock()

    assert await backup._backup_after_subscription(bot) is True

    document = bot.send_document.await_args.kwargs["document"]
    with zipfile.ZipFile(io.BytesIO(document.data)) as archive:
        assert "subscribers.json" in archive.namelist()
    assert "Накопленные изменения подписок" in bot.send_document.await_args.kwargs["caption"]


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
                "token": uuid4().hex,
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
async def test_unrelated_restore_during_subscription_send_keeps_pending(
    backup_env,
    monkeypatch,
):
    await storage.mutate_subscription(7, "Neo", subscribed=True)

    async def send_and_restore(_bot, _caption):
        await backup.restore_backup_zip(
            _zip_bytes({
                "update_state.json": json.dumps(storage._empty_update_state()),
            })
        )
        return True

    monkeypatch.setattr("backup.send_backup", send_and_restore)

    assert await backup._backup_after_subscription(AsyncMock()) is False
    schedule = storage.load_subscription_backup_state()
    assert schedule["last_backup_at"] is None
    assert schedule["pending"]["subscriptions"] == 1


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
        "token": uuid4().hex,
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
async def test_weekly_caller_builds_and_sends_real_archive(backup_env):
    old = time.time() - backup.WEEKLY_BACKUP_INTERVAL - 100
    cur = {"period": "2026-Q2", "events": []}
    _save_subscriber_schedule(last_backup_at=old, weekly_started_at=old)
    bot = AsyncMock()

    assert await backup._weekly_backup_if_due(bot, cur) is cur

    document = bot.send_document.await_args.kwargs["document"]
    with zipfile.ZipFile(io.BytesIO(document.data)) as archive:
        assert "subscribers.json" in archive.namelist()
    assert "Еженедельный бэкап" in bot.send_document.await_args.kwargs["caption"]


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
async def test_unrelated_restore_during_weekly_send_keeps_old_timestamp(
    backup_env,
    monkeypatch,
):
    old = time.time() - backup.WEEKLY_BACKUP_INTERVAL - 100
    cur = {"period": "2026-Q2", "events": []}
    _save_subscriber_schedule(last_backup_at=old, weekly_started_at=old)

    async def send_and_restore(_bot, _caption):
        await backup.restore_backup_zip(
            _zip_bytes({
                "update_state.json": json.dumps(storage._empty_update_state()),
            })
        )
        return True

    monkeypatch.setattr("backup.send_backup", send_and_restore)

    assert await backup._weekly_backup_if_due(AsyncMock(), cur) is cur
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
        "token": uuid4().hex,
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
            "token": uuid4().hex,
        },
    )
    before = storage.load_subscription_backup_state()
    monkeypatch.setattr("backup._last_backup_sent_at", None)
    bot = AsyncMock()

    await backup._shutdown_backup(bot)

    bot.send_document.assert_awaited_once()
    caption = bot.send_document.await_args.kwargs["caption"]
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


@pytest.mark.asyncio
async def test_real_shutdown_timeout_drains_backup_worker(backup_env, monkeypatch):
    monkeypatch.setattr("backup._last_backup_sent_at", None)
    monkeypatch.setattr(backup, "SHUTDOWN_BACKUP_TIMEOUT", 0.02)
    started = threading.Event()

    def slow_scan(cancelled):
        started.set()
        while not cancelled.wait(0.005):
            pass
        backup._raise_if_backup_cancelled(cancelled)

    monkeypatch.setattr(backup, "_scan_backup_manifest", slow_scan)
    bot = AsyncMock()
    before = time.monotonic()

    await backup._shutdown_backup(bot)

    assert started.is_set()
    assert time.monotonic() - before < 1
    bot.send_document.assert_not_awaited()
    assert _backup_worker_threads() == []


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
async def test_restore_blocked_users_alone_preserves_legacy_weekly_anchor(
    backup_env,
    monkeypatch,
):
    legacy_anchor = 1_900_000_000.0
    monkeypatch.setattr(backup.time, "time", lambda: 2_000_000_000.0)
    storage.SUBS_FILE.write_text(
        '{"subscribers": {"7": "Blocked", "8": "Allowed"}}',
        encoding="utf-8",
    )
    storage.save_stats_current(
        {
            "period": "2026-Q2",
            "events": [],
            "last_backup_at": legacy_anchor,
        }
    )

    result = await backup.restore_backup_zip(
        _zip_bytes({"blocked_users.json": '{"blocked_user_ids": [7]}'})
    )

    assert set(result["restored"]) == {"blocked_users.json", "subscribers.json"}
    assert storage.load_subscribers() == {8: "Allowed"}
    schedule = storage.load_subscription_backup_state()
    assert schedule["last_backup_at"] is None
    assert schedule["weekly_started_at"] == legacy_anchor
    assert schedule["pending"] is None


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


@pytest.mark.asyncio
async def test_backup_export_includes_facts_json(backup_env):
    (backup_env / "facts.json").write_text(
        _facts_payload("exported-fact"),
        encoding="utf-8",
    )

    raw, _ = await backup._build_backup_zip()
    names = set(zipfile.ZipFile(io.BytesIO(raw)).namelist())

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
