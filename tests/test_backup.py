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
import random
import threading
import time
import zipfile
import zlib
from pathlib import Path
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
        "current_uncertain",
        "current_complete",
        "rich_partial",
        "rich_uncertain",
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
        pending["next_unit"] = 1 if schema in {"rich_partial", "rich_uncertain"} else 2
    else:
        pending = storage.new_quarter_delivery(
            "2026-Q2", "2026-Q3", [] if schema == "empty" else ["frozen first", "frozen second"],
        )
        pending["next_unit"] = {"current_partial": 1, "current_uncertain": 1, "current_complete": 2, "empty": 0}[schema]
    if schema.endswith("uncertain"):
        pending["delivery_uncertain"] = True
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
@pytest.mark.parametrize("base_version", [1, 2])
@pytest.mark.parametrize("plan_version", [1, 2, 3])
async def test_index_archive_roundtrip_preserves_old_format_plans_facts_and_schedule(
    backup_env, source_index_factory, base_version, plan_version,
):
    from copy import deepcopy

    from event_journal_schema import validate_recovery_set
    from notification_progress_schema import parse_recovery_journal

    journal, cur = source_index_factory(version=base_version)
    if plan_version == 1:
        plan = storage.new_quarter_delivery("2026-Q1", "2026-Q2", ["frozen one", "frozen two"])
    else:
        units = [{"transport": "html", "content": text, "disable_preview": False} for text in ["frozen one", "frozen two"]]
        revisions = {"2026-Q1": cur["event_time"]["periods"]["2026-Q1"]["revision"]} if plan_version == 3 else None
        plan = storage.new_quarter_delivery_plan("2026-Q1", "2026-Q2", units, event_time_revisions=revisions)
        if plan_version == 3:
            cur["event_time"]["report_ack"] = {"plan_id": plan["plan_id"], "revisions": revisions}
    plan.update(next_unit=1, delivery_uncertain=True)
    cur["pending_quarter_delivery"] = plan
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    storage.save_subscribers({10: "only"})
    storage.notification_memberships()
    state = storage.load_subscriber_state(strict_subscribers=True)
    state_before = deepcopy(state)
    paths = [storage.EVENT_JOURNAL_FILE, storage.notification_progress_file(), storage.STATS_CURRENT_FILE]
    original = {path.name: path.read_bytes() for path in paths}
    archive, generation = await backup._build_backup_zip()
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        history = zipped.read("event_journal.json")
        assert json.loads(history)["version"] == (5 if base_version == 1 else 6)
        assert parse_recovery_journal(history, zipped.read("notification_progress.json")) == journal
    assert {path.name: path.read_bytes() for path in paths} == original
    await backup.restore_backup_zip(archive)
    assert storage.restorable_restore_generation() > generation
    restored = storage.load_event_journal()
    restored_cur = storage.load_stats_current(strict=True)
    validate_recovery_set(restored, restored_cur)
    assert restored == journal
    assert restored_cur["pending_quarter_delivery"] == plan
    assert restored_cur["event_time"] == cur["event_time"]
    assert storage.load_subscriber_state(strict_subscribers=True) == state_before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["window_gap", "checksum", "version", "oversized", "missing_progress"])
async def test_index_archive_invalid_candidate_rejects_before_file_mutation(
    backup_env, source_index_factory, damage,
):
    from source_history import content_hash

    journal, cur = source_index_factory(version=2)
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    original = {path.name: path.read_bytes() for path in backup_env.iterdir() if path.is_file()}
    candidate = dict(original)
    history = json.loads(candidate["event_journal.json"])
    base = history["source_base"]
    if damage == "window_gap":
        base["ids"][100][1] = None
        base["checksum"] = content_hash({k: v for k, v in base.items() if k != "checksum"})
    elif damage == "checksum":
        base["checksum"] = "f" * 64
    elif damage == "version":
        history["version"] = 7
    candidate["event_journal.json"] = json.dumps(history).encode()
    if damage == "oversized":
        candidate["event_journal.json"] = b" " * (8 * 1024 * 1024 + 1)
    elif damage == "missing_progress":
        del candidate["notification_progress.json"]
    generation = storage.restorable_restore_generation()
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(_zip_bytes(candidate))
    assert storage.restorable_restore_generation() == generation
    assert {path.name: path.read_bytes() for path in backup_env.iterdir() if path.is_file()} == original


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
async def test_recovery_backup_ignores_cache_above_total_limit(backup_env):
    cur = storage._empty_stats_current("2026-Q3")
    storage.save_stats_current(cur, strict=True)
    cache = backup_env / "stats_all.json"
    with cache.open("wb") as target:
        target.truncate(backup._BACKUP_TOTAL_MAX_BYTES + 1)

    raw, _ = await backup._build_backup_zip()

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.namelist() == ["stats_current.json"]
        assert archive.read("stats_current.json") == storage.stats_current_json(cur).encode()


@pytest.mark.asyncio
async def test_full_export_preserves_exact_bytes_and_both_modes_restore(backup_env):
    cur = storage._empty_stats_current("2026-Q3")
    storage.save_stats_current(cur, strict=True)
    current_raw = storage.STATS_CURRENT_FILE.read_bytes()
    diagnostics = {
        "stats_all.json": b' {"unusual": [1, 2]}\r\n ',
        "seen_ids.json": b'{"seen_ids": [7,7]}\n',
        "seen_favourites.json": b'{ "seen_favourites": [] }\r\n',
        "diagnostic/raw.bin": bytes(range(256)),
    }
    for name, data in diagnostics.items():
        path = backup_env / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    quarter = backup_env / "quarters" / "2026-Q2.json"
    quarter_raw = b'{"period":"2026-Q2","events":[]}\r\n'
    quarter.write_bytes(quarter_raw)

    for full_export in (True, False):
        quarter_raw = quarter.read_bytes()
        raw, _ = await backup._build_backup_zip(full_export=full_export)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert archive.read("stats_current.json") == current_raw
            assert archive.read("quarters/2026-Q2.json") == quarter_raw
            assert set(archive.namelist()) == {"stats_current.json", "quarters/2026-Q2.json"} | (
                set(diagnostics) if full_export else set()
            )
            for name, data in diagnostics.items():
                if full_export:
                    assert archive.read(name) == data
        result = await backup.restore_backup_zip(raw)
        assert set(result["restored"]) == {"stats_current.json", "quarters/2026-Q2.json"}
        assert storage.load_stats_current(strict=True) == cur
        assert {name: (backup_env / name).read_bytes() for name in diagnostics} == diagnostics


@pytest.mark.asyncio
async def test_full_export_limit_failure_does_not_block_recovery_or_acknowledge(backup_env, monkeypatch):
    await storage.mutate_subscription(7, "Neo", subscribed=True)
    storage.save_stats_current(storage._empty_stats_current("2026-Q3"), strict=True)
    before = storage.SUBS_FILE.read_bytes()
    with (backup_env / "stats_all.json").open("wb") as target:
        target.truncate(backup._BACKUP_TOTAL_MAX_BYTES + 1)
    monkeypatch.setattr("backup._last_backup_sent_at", None)
    bot = AsyncMock()

    with pytest.raises(backup.BackupLimitError, match="32 МиБ"):
        await backup.send_backup(bot, "diagnostics", full_export=True)
    bot.send_document.assert_not_awaited()
    assert backup._last_backup_sent_at is None
    assert storage.SUBS_FILE.read_bytes() == before

    assert await backup.send_backup(bot, "recovery")
    assert storage.SUBS_FILE.read_bytes() == before
    assert "shikibot-backup-" in bot.send_document.await_args.kwargs["document"].filename


@pytest.mark.asyncio
async def test_unreadable_diagnostic_export_failure_leaves_recovery_available(backup_env, monkeypatch):
    storage.save_stats_current(storage._empty_stats_current("2026-Q3"), strict=True)
    diagnostic = backup_env / "stats_all.json"
    diagnostic.write_bytes(b"unreadable cache")
    real_open = Path.open

    def unreadable(path, *args, **kwargs):
        if path == diagnostic:
            raise PermissionError("diagnostic cache")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    bot = AsyncMock()
    assert await backup.send_backup(bot, "full", full_export=True) is False
    bot.send_document.assert_not_awaited()
    assert await backup.send_backup(bot, "recovery")
    bot.send_document.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("full_export", [False, True])
@pytest.mark.parametrize("boundary", ["member", "total", "entries", "zip"])
async def test_both_archive_modes_enforce_real_resource_limits(backup_env, full_export, boundary):
    if boundary == "member":
        with (backup_env / "stats_current.json").open("wb") as target:
            target.truncate(8 * 1024 * 1024 + 1)
    elif boundary == "total":
        for index in range(5):
            with (backup_env / "quarters" / f"{2000 + index}-Q1.json").open("wb") as target:
                target.truncate(7 * 1024 * 1024)
    elif boundary == "entries":
        for index in range(257):
            (backup_env / "quarters" / f"{2000 + index}-Q1.json").write_bytes(b"{}")
    else:
        # Детерминированная несжимаемая синтетика проверяет реальный ZIP, не прогноз.
        data = random.Random(0).randbytes(7 * 1024 * 1024)
        for index in range(3):
            (backup_env / "quarters" / f"{2000 + index}-Q1.json").write_bytes(data)
    with pytest.raises(backup.BackupLimitError):
        await backup._build_backup_zip(full_export=full_export)


@pytest.mark.asyncio
async def test_full_export_allows_diagnostic_member_above_restorable_limit(backup_env):
    data = b"x" * (backup._BACKUP_RESTORABLE_MEMBER_MAX_BYTES + 1)
    (backup_env / "stats_all.json").write_bytes(data)
    raw, _ = await backup._build_backup_zip(full_export=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("stats_all.json") == data


@pytest.mark.asyncio
async def test_archive_capacity_measurements_repeat_identical_inputs(backup_env, source_index_factory):
    """Три замера настоящих архивов; печать позволяет повторить таблицу README."""
    journal, cur = source_index_factory(version=2)
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    storage.save_seen_ids({7, 11})
    storage.save_seen_favourites({"anime:11"})
    for period in ("2025-Q4", "2026-Q1"):
        storage._atomic_write(
            backup_env / "quarters" / f"{period}.json",
            json.dumps({"period": period, "events": []}, ensure_ascii=False, indent=2),
        )
    assert len(storage.load_event_journal()["outbox"]["records"]) == 2
    recovery_bytes = sum(
        path.stat().st_size for path in backup_env.rglob("*")
        if path.is_file() and backup._is_allowed_import_member(path.relative_to(backup_env).as_posix())
    )
    for padding in (1024, 9 * 1024 * 1024, 33 * 1024 * 1024):
        # Cache записан штатным сериализатором; его содержимое намеренно синтетическое.
        storage.save_stats_all({"synthetic_diagnostic": "x" * padding})
        total_bytes = sum(path.stat().st_size for path in backup_env.rglob("*") if path.is_file())
        samples = []
        for _ in range(3):
            recovery, _ = await backup._build_backup_zip()
            with zipfile.ZipFile(io.BytesIO(recovery)) as archive:
                assert sum(member.file_size for member in archive.infolist()) == recovery_bytes
            try:
                exported, _ = await backup._build_backup_zip(full_export=True)
            except backup.BackupLimitError:
                assert total_bytes > backup._BACKUP_TOTAL_MAX_BYTES
                export_size = None
            else:
                with zipfile.ZipFile(io.BytesIO(exported)) as archive:
                    assert sum(member.file_size for member in archive.infolist()) == total_bytes
                export_size = len(exported)
            samples.append((len(recovery), export_size))
        assert samples == [samples[0]] * 3
        print(f"capacity padding={padding} recovery_raw={recovery_bytes} recovery_zip={samples[0][0]} full_raw={total_bytes} full_zip={samples[0][1]} repeats=3")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["subscription", "weekly", "shutdown"])
async def test_automatic_backups_ignore_oversized_diagnostics(backup_env, monkeypatch, kind):
    old = time.time() - backup.WEEKLY_BACKUP_INTERVAL - 100
    _save_subscriber_schedule(last_backup_at=old, weekly_started_at=old)
    if kind == "subscription":
        await storage.mutate_subscription(7, "Neo", subscribed=True)
    cur = storage._empty_stats_current("2026-Q3")
    storage.save_stats_current(cur, strict=True)
    before = storage.load_subscription_backup_state()
    with (backup_env / "stats_all.json").open("wb") as target:
        target.truncate(backup._BACKUP_TOTAL_MAX_BYTES + 1)
    monkeypatch.setattr("backup._last_backup_sent_at", None)
    bot = AsyncMock()

    if kind == "subscription":
        assert await backup._backup_after_subscription(bot)
    elif kind == "weekly":
        assert await backup._weekly_backup_if_due(bot, cur) is cur
    else:
        await backup._shutdown_backup(bot)
    bot.send_document.assert_awaited_once()
    with zipfile.ZipFile(io.BytesIO(bot.send_document.await_args.kwargs["document"].data)) as archive:
        assert set(archive.namelist()) == {"subscribers.json", "stats_current.json"}
    after = storage.load_subscription_backup_state()
    if kind == "shutdown":
        assert after == before
    else:
        assert after["last_backup_at"] > old
        assert after["pending"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_count", [0, 257])
async def test_recovery_backup_filters_diagnostics_before_count_and_open(backup_env, monkeypatch, extra_count):
    storage.save_stats_current(storage._empty_stats_current("2026-Q3"), strict=True)
    names = ["stats_all.json", "seen_ids.json", "seen_favourites.json"]
    names.extend(f"diagnostic-{index}.bin" for index in range(extra_count))
    for name in names:
        (backup_env / name).write_bytes(b"diagnostic")
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.name in names:
            raise AssertionError("Резервная копия не должна открывать диагностику")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    raw, _ = await backup._build_backup_zip()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.namelist() == ["stats_current.json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("full_export", [False, True])
async def test_build_backup_zip_excludes_tmp_and_keeps_structure(backup_env, full_export):
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

    raw, _ = await backup._build_backup_zip(full_export=full_export)
    names = set(zipfile.ZipFile(io.BytesIO(raw)).namelist())

    assert "subscribers.json" in names
    assert "blocked_users.json" in names
    assert "stats_current.json" in names
    assert ("stats_all.json" in names) is full_export
    assert "known_users.json" in names
    assert "user_alerts.json" in names
    assert "quarters/2026-Q1.json" in names          # вложенность сохранена
    assert "subscribers.json.tmp" not in names       # *.tmp исключён
    assert not any(name.startswith(".restore-") for name in names)


@pytest.mark.asyncio
async def test_slow_restorable_capture_keeps_event_loop_live_and_blocks_writer(
    backup_env,
    monkeypatch,
    journal_factory,
):
    journal = journal_factory()
    storage.save_event_journal(journal)
    old = json.dumps(_journal_current(journal)).encode("utf-8")
    new = json.dumps({**_journal_current(journal, applied=1), "events": [{"id": "new"}]})
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
            storage.save_event_journal({**journal, "processed_seq": 1})

    writer = asyncio.create_task(publish_new_state())
    await asyncio.sleep(0.02)
    assert ticks == 5
    assert writer.done() is False

    release.set()
    raw, _ = await build_task
    await writer

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("stats_current.json") == old
        assert json.loads(archive.read("event_journal.json")) == journal
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
    journal_factory,
):
    journal = journal_factory()
    storage.save_event_journal(journal)
    old = json.dumps(_journal_current(journal)).encode("utf-8")
    new = json.dumps({**_journal_current(journal, applied=1), "events": [{"id": "new"}]})
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
        storage.save_event_journal({**journal, "processed_seq": 1})
    await asyncio.sleep(0)
    assert build_task.done() is False

    release.set()
    raw, _ = await build_task

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("stats_current.json") == old
        assert json.loads(archive.read("event_journal.json")) == journal
    assert (backup_env / "stats_current.json").read_text(encoding="utf-8") == new


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["capture", "compression"])
@pytest.mark.parametrize("full_export", [False, True])
async def test_backup_cancellation_drains_worker(
    backup_env,
    monkeypatch,
    stage,
    full_export,
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
    task = asyncio.create_task(backup._build_backup_zip(full_export=full_export))
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
@pytest.mark.parametrize("full_export", [False, True])
async def test_restore_during_compression_invalidates_snapshot_before_upload(
    backup_env,
    monkeypatch,
    full_export,
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
    send_task = asyncio.create_task(backup.send_backup(bot, "x", full_export=full_export))
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
@pytest.mark.parametrize("full_export", [False, True])
async def test_backup_resource_limits_are_inclusive(backup_env, monkeypatch, full_export):
    monkeypatch.setattr(backup, "_BACKUP_ARCHIVE_MAX_MEMBERS", 2)
    monkeypatch.setattr(backup, "_BACKUP_RESTORABLE_MEMBER_MAX_BYTES", 4)
    monkeypatch.setattr(backup, "_BACKUP_TOTAL_MAX_BYTES", 8)
    (backup_env / "blocked_users.json").write_bytes(b"1234")
    second = "stats_all.json" if full_export else "known_users.json"
    extra = "extra.json" if full_export else "user_alerts.json"
    (backup_env / second).write_bytes(b"5678")

    raw, _ = await backup._build_backup_zip(full_export=full_export)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("blocked_users.json") == b"1234"
        assert archive.read(second) == b"5678"

    (backup_env / extra).write_bytes(b"x")
    with pytest.raises(ValueError, match="больше 2 файлов"):
        await backup._build_backup_zip(full_export=full_export)

    (backup_env / extra).unlink()
    (backup_env / "blocked_users.json").write_bytes(b"12345")
    with pytest.raises(ValueError, match="восстанавливаемый файл больше"):
        await backup._build_backup_zip(full_export=full_export)

    (backup_env / "blocked_users.json").write_bytes(b"1234")
    monkeypatch.setattr(backup, "_BACKUP_TOTAL_MAX_BYTES", 7)
    with pytest.raises(ValueError, match="суммарный размер архива больше"):
        await backup._build_backup_zip(full_export=full_export)


def test_completed_zip_limit_is_inclusive(monkeypatch):
    cancelled = threading.Event()
    members = (backup._BackupMember("payload.bin", bytes(range(256)) * 16),)
    monkeypatch.setattr(backup, "_BACKUP_ZIP_MAX_BYTES", 1024 * 1024)
    raw = backup._compress_backup_zip(cancelled, members)

    monkeypatch.setattr(backup, "_BACKUP_ZIP_MAX_BYTES", len(raw))
    assert len(backup._compress_backup_zip(cancelled, members)) == len(raw)

    monkeypatch.setattr(backup, "_BACKUP_ZIP_MAX_BYTES", len(raw) - 1)
    with pytest.raises(ValueError, match="готовый ZIP больше"):
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
@pytest.mark.parametrize("original", [b"\xff\xfe\x00broken", b'{\r\n"events": []\n}\r'])
async def test_restore_rolls_back_first_file_when_second_publish_fails(
    backup_env,
    monkeypatch,
    original,
):
    storage._atomic_write(
        backup_env / "subscribers.json",
        '{"subscribers": {"1": "Old"}}',
    )
    (backup_env / "stats_current.json").write_bytes(original)
    raw = _zip_bytes({
        "stats_current.json": '{"period": "2026-Q2", "events": []}',
        "subscribers.json": '{"subscribers": {"2": "New"}}',
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

    assert (backup_env / "stats_current.json").read_bytes() == original
    assert storage.load_subscribers() == {1: "Old"}


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
async def test_restore_repairs_non_utf8_current_file(backup_env):
    target = backup_env / "stats_current.json"
    target.write_bytes(b"\xff\xfe\x00damaged")
    candidate = {"period": "2026-Q2", "events": []}
    await backup.restore_backup_zip(_zip_bytes({"stats_current.json": json.dumps(candidate)}))
    assert json.loads(target.read_text(encoding="utf-8")) == candidate


@pytest.mark.asyncio
async def test_nested_fact_restore_preserves_good_bank(backup_env):
    current = fact_bank.parse_fact_bank_bytes(_facts_payload("current-fact").encode())
    fact_bank._atomic_write(backup_env / "facts.json", fact_bank.serialize_fact_bank(current))
    before = fact_bank.activate_restored_fact_bank(current)
    original = (backup_env / "facts.json").read_bytes()
    with pytest.raises(ValueError, match="facts.json"):
        await backup.restore_backup_zip(_zip_bytes({"facts.json": "[" * 5000 + "]" * 5000}))
    assert (backup_env / "facts.json").read_bytes() == original
    assert fact_bank.get_fact_bank_snapshot() == before


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
async def test_weekly_backup_persists_legacy_memberships_before_capture(backup_env):
    old = time.time() - backup.WEEKLY_BACKUP_INTERVAL - 100
    cur = {"period": "2026-Q2", "events": []}
    _save_subscriber_schedule({7: "Neo", -100: "Group"}, last_backup_at=old, weekly_started_at=old)
    legacy = json.loads(storage.SUBS_FILE.read_text(encoding="utf-8"))
    legacy.pop("notification_memberships")
    storage.SUBS_FILE.write_text(json.dumps(legacy), encoding="utf-8")
    captures = []

    async def send(*args, **kwargs):
        assert not storage._restorable_state_lock().locked()
        with zipfile.ZipFile(io.BytesIO(kwargs["document"].data)) as archive:
            archived = json.loads(archive.read("subscribers.json"))
        published = storage.load_subscriber_state(strict_subscribers=True)
        captures.append((archived, published.notification_memberships))

    bot = AsyncMock()
    bot.send_document.side_effect = send
    assert await backup._weekly_backup_if_due(bot, cur) is cur
    final = storage.load_subscriber_state(strict_subscribers=True)
    assert final.backup_schedule["last_backup_at"] > old
    assert final.backup_schedule["weekly_started_at"] == old
    assert final.backup_schedule["pending"] is None
    assert final.subscribers == {7: "Neo", -100: "Group"}
    archived, published_memberships = captures[0]
    assert published_memberships == final.notification_memberships
    assert archived["notification_memberships"]["tokens"] == {
        str(cid): token for cid, token in published_memberships.items()
    }
    assert archived["backup_schedule"] == legacy["backup_schedule"]
    assert await backup._weekly_backup_if_due(bot, cur) is cur
    bot.send_document.assert_awaited_once()


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
@pytest.mark.parametrize("full_export", [False, True])
async def test_manual_backup_does_not_change_automatic_schedule(backup_env, monkeypatch, full_export):
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
    monkeypatch.setattr("backup._last_backup_sent_at", None)

    assert await backup.send_backup(AsyncMock(), f"Вручную\n\n{backup.BACKUP_TAG}", full_export=full_export)

    assert storage.load_subscription_backup_state() == before
    assert (backup._last_backup_sent_at is None) is full_export


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

    def slow_scan(cancelled, full_export):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [None, "payload", "revision_binding", "missing_state", "missing_events", "null_events"])
async def test_event_time_recovery_import_preserves_frozen_revisions_or_rejects_candidate(backup_env, journal_factory, damage):
    from event_time_stats import (
        ensure_event_time,
        project_event,
        report_revisions,
        rotate_event_time,
    )

    journal = journal_factory(count=2, processed=1)
    cur = _journal_current(journal, applied=0)
    ensure_event_time(cur)
    project_event(cur, journal, 1)
    cur["event_projection"]["applied_seq"] = 1
    fresh = storage._empty_stats_current("2026-Q3")
    fresh["event_projection"] = dict(cur["event_projection"])
    plan = storage.new_quarter_delivery_plan("2026-Q2", "2026-Q3", [], event_time_revisions=report_revisions(cur))
    fresh["pending_quarter_delivery"] = plan
    rotate_event_time(cur, fresh, plan)
    original = b'{"period":"2026-Q2","events":[]}'
    storage.STATS_CURRENT_FILE.write_bytes(original)
    before_generation = storage.restorable_restore_generation()
    if damage == "payload":
        fresh["event_time"]["periods"]["2026-Q1"]["events"][0]["score"] = 9
    elif damage == "revision_binding":
        fresh["event_time"]["report_ack"]["revisions"]["2026-Q1"] = 0
    elif damage == "missing_state":
        fresh.pop("event_time")
    elif damage == "missing_events":
        fresh.pop("events")
    elif damage == "null_events":
        fresh["events"] = None
    archive = _zip_bytes({"event_journal.json": json.dumps(journal), "stats_current.json": json.dumps(fresh)})
    if damage is not None:
        with pytest.raises(ValueError):
            await backup.restore_backup_zip(archive)
        assert storage.STATS_CURRENT_FILE.read_bytes() == original
        assert storage.restorable_restore_generation() == before_generation
        assert not storage.EVENT_JOURNAL_FILE.exists()
    else:
        await backup.restore_backup_zip(archive)
        assert storage.load_stats_current(strict=True) == fresh
        assert storage.load_event_journal() == journal


def _journal_current(journal, applied=None):
    cur = storage._empty_stats_current("2026-Q2")
    cur["event_projection"] = {
        "journal_id": journal["journal_id"], "baseline_seq": 0,
        "applied_seq": journal["processed_seq"] if applied is None else applied,
    }
    return cur


def _recovery_zip(journal, cur):
    return _zip_bytes({
        "event_journal.json": json.dumps(journal),
        "stats_current.json": json.dumps(cur),
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_score", [None, 8])
async def test_legacy_source_score_removal_roundtrips_without_changing_base(
    backup_env, journal_factory, legacy_score,
):
    from event_time_stats import (
        ensure_event_time,
        project_event,
    )

    journal = journal_factory(processed=1)
    journal["events"][0].update(event_type="score_removed", score=None)
    cur = _journal_current(journal, applied=0)
    cur.update(period="2026-Q1", events=[
        {"id": "11", "media": "anime", "event": "completed", "score": legacy_score},
    ])
    ensure_event_time(cur)
    project_event(cur, journal, 1)
    cur["event_projection"]["applied_seq"] = 1
    assert cur["events"][0]["score"] == 0

    await backup.restore_backup_zip(_recovery_zip(journal, cur))

    restored = storage.load_stats_current(strict=True)
    assert restored == cur
    assert restored["event_time"]["legacy_events"][0]["score"] == legacy_score
    assert storage.load_event_journal() == journal


@pytest.mark.asyncio
async def test_unfinished_acquisition_roundtrips_and_legacy_restore_preserves_it(backup_env, acquisition_factory):
    journal = acquisition_factory()
    cur = _journal_current(journal)
    await backup.restore_backup_zip(_recovery_zip(journal, cur))
    raw, _ = await backup._build_backup_zip()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert json.loads(archive.read("event_journal.json")) == journal
    storage.EVENT_JOURNAL_FILE.write_bytes(b"{damaged")
    await backup.restore_backup_zip(raw)
    assert storage.load_event_journal() == journal
    await backup.restore_backup_zip(_zip_bytes({"stats_current.json": json.dumps({"period": "2026-Q1", "events": []})}))
    assert storage.load_event_journal() == journal
    assert storage.load_stats_current(strict=True)["event_projection"]["applied_seq"] == 0


@pytest.mark.asyncio
async def test_malformed_acquisition_rejects_entire_restore(backup_env, acquisition_factory):
    journal = acquisition_factory()
    cur = _journal_current(journal)
    storage.save_stats_current(cur, strict=True)
    original = storage.STATS_CURRENT_FILE.read_bytes()
    journal["catchup"]["frontier"] = [999]
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(_recovery_zip(journal, cur))
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    assert storage.load_event_journal() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_kind", ["identical", "unrelated", "older", "legacy"])
async def test_restore_during_acquisition_prevents_stale_page_publication(
    backup_env, acquisition_factory, journal_factory, monkeypatch, restore_kind,
):
    import handlers

    journal = acquisition_factory()
    cur = _journal_current(journal)
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    restores = []

    async def fetch(_session, page=1):
        if not restores:
            restores.append(True)
            if restore_kind == "identical":
                raw = _recovery_zip(journal, cur)
            elif restore_kind == "unrelated":
                raw = _zip_bytes({"user_alerts.json": '{"enabled":false}'})
            elif restore_kind == "legacy":
                raw = _zip_bytes({"stats_current.json": '{"period":"2026-Q1","events":[]}'})
            else:
                old = journal_factory(count=0)
                raw = _recovery_zip(old, _journal_current(old))
            await backup.restore_backup_zip(raw)
        return [{"id": 3}, {"id": 4}]

    monkeypatch.setattr("handlers.fetch_history", fetch)
    send = AsyncMock()
    monkeypatch.setattr("handlers.send_to_all_chats", send)
    await handlers.check_and_notify(AsyncMock(), {999}, None)
    assert restores == [True]
    restored = storage.load_event_journal()
    from notification_outbox import migrate_outbox

    expected = journal_factory(count=0) if restore_kind == "older" else journal
    if restore_kind in {"unrelated", "legacy"}:
        expected = migrate_outbox(expected, 0)
    assert restored == expected
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("processed,applied", [(0, 0), (0, 1), (1, 1)])
async def test_journal_complete_recovery_roundtrip(backup_env, journal_factory, processed, applied):
    journal = journal_factory(processed=processed)
    cur = _journal_current(journal, applied)
    await backup.restore_backup_zip(_recovery_zip(journal, cur))
    assert storage.load_event_journal() == journal
    assert storage.load_stats_current(strict=True) == cur
    raw, _ = await backup._build_backup_zip()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert json.loads(archive.read("event_journal.json")) == journal
        assert json.loads(archive.read("stats_current.json")) == cur
    storage.EVENT_JOURNAL_FILE.write_bytes(b"{damaged")
    await backup.restore_backup_zip(raw)
    assert storage.load_event_journal()["events"][0]["created_at"] == "2026-04-01T02:00:00+03:00"
    assert storage.load_event_journal()["events"][0]["event_at"] == "2026-03-31T23:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_current", "identity", "cursor", "profile", "missing_journal", "malformed"])
async def test_journal_import_rejects_entire_inconsistent_candidate(backup_env, journal_factory, damage):
    journal = journal_factory()
    cur = _journal_current(journal)
    original = b'{"subscribers":{"10":"keep"}}\r\n'
    storage.SUBS_FILE.write_bytes(original)
    if damage == "identity":
        cur["event_projection"]["journal_id"] = "b" * 32
    if damage == "cursor":
        cur["event_projection"]["applied_seq"] = 2
    if damage == "profile":
        journal["profile"] = "Another"
    members = {
        "subscribers.json": '{"subscribers":{"20":"replace"}}',
        "event_journal.json": json.dumps(journal),
        "stats_current.json": json.dumps(cur),
    }
    if damage == "missing_current":
        del members["stats_current.json"]
    if damage == "missing_journal":
        del members["event_journal.json"]
    if damage == "malformed":
        members["event_journal.json"] = '{bad'
    generation = storage.restorable_restore_generation()
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(_zip_bytes(members))
    assert storage.SUBS_FILE.read_bytes() == original
    assert storage.restorable_restore_generation() == generation
    assert not storage.EVENT_JOURNAL_FILE.exists()


@pytest.mark.asyncio
async def test_legacy_quarter_restore_sets_baseline_and_preserves_unfinished_work(backup_env, journal_factory, monkeypatch):
    journal = journal_factory(count=2, processed=1)
    cur = _journal_current(journal, applied=2)
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    exact_journal = storage.EVENT_JOURNAL_FILE.read_bytes()
    legacy = {"period": "2026-Q2", "events": [{"id": "old", "event": "planned"}]}
    await backup.restore_backup_zip(_zip_bytes({"stats_current.json": json.dumps(legacy)}))
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == exact_journal
    restored = storage.load_stats_current(strict=True)
    assert restored["event_projection"] == {"journal_id": journal["journal_id"], "baseline_seq": 1, "applied_seq": 1}
    monkeypatch.setattr("handlers.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("handlers._enqueue_history_event", AsyncMock(wraps=handlers._enqueue_history_event))
    await handlers._drain_history_journal(AsyncMock())
    handlers._enqueue_history_event.assert_awaited_once()
    assert storage.load_event_journal()["processed_seq"] == 2
    projected = storage.load_stats_current(strict=True)
    assert projected["events"] == legacy["events"]
    assert projected["event_time"]["periods"]["2026-Q1"]["events"][0]["id"] == "12"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 3])
async def test_journal_restore_rollback_preserves_exact_damaged_bytes(backup_env, journal_factory, monkeypatch, version):
    journal_before = b"\xffbroken journal\r\n"
    current_before = b"{ broken quarter\r\n"
    storage.EVENT_JOURNAL_FILE.write_bytes(journal_before)
    storage.STATS_CURRENT_FILE.write_bytes(current_before)
    journal = journal_factory()
    if version == 3:
        from notification_outbox import migrate_outbox
        journal = migrate_outbox(journal, 0)
    cur = _journal_current(journal)
    raw = _zip_bytes({
        "event_journal.json": json.dumps(journal),
        "stats_current.json": json.dumps(cur),
        "user_alerts.json": '{"enabled":false}',
    })
    real_publish = backup._publish_staged_file

    def fail_third(source, target):
        if target.name == "user_alerts.json":
            raise OSError("replacement failure")
        real_publish(source, target)

    monkeypatch.setattr(backup, "_publish_staged_file", fail_third)
    generation = storage.restorable_restore_generation()
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(raw)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == journal_before
    assert storage.STATS_CURRENT_FILE.read_bytes() == current_before
    assert not storage.USER_ALERTS_FILE.exists()
    assert storage.restorable_restore_generation() == generation


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_legacy_journal_restore_over_retained_state_migrates_quietly(
    backup_env, journal_factory, monkeypatch, version,
):
    from notification_outbox import retain_outbox

    current = retain_outbox(_compacted_recovery(journal_factory))
    storage.save_event_journal(current)
    storage.save_stats_current(_journal_current(current), strict=True)
    storage.save_subscribers({10: "active"})
    legacy = journal_factory(count=2, processed=1)
    if version == 2:
        legacy.update(version=2, catchup=None)
    await backup.restore_backup_zip(_recovery_zip(legacy, _journal_current(legacy)))
    monkeypatch.setattr("handlers.asyncio.sleep", AsyncMock())
    bot = AsyncMock()
    await handlers._drain_history_journal(bot)
    bot.send_message.assert_not_awaited()
    recovered = storage.load_event_journal()
    assert recovered["outbox"]["baseline_seq"] == 1
    assert recovered["outbox"]["enqueued_seq"] == recovered["processed_seq"] == 2
    assert [r["seq"] for r in recovered["outbox"]["records"]] == [2]
    assert not recovered["outbox"].get("plans")
    assert recovered["outbox"]["records"][0]["recipients"]["10"]["status"] == "pending"
    assert not recovered["outbox"]["records"][0]["recipients"]["10"]["prior_possible"]


def _compacted_recovery(journal_factory):
    from notification_outbox import (
        begin_attempt,
        compact_outbox,
        complete_attempt,
        enqueue,
        migrate_outbox,
    )

    journal = migrate_outbox(journal_factory(count=2), 0)
    for event in journal["events"]:
        journal = enqueue(journal, event, "frozen", {10: "b" * 32}, 1000)
    recipient = journal["outbox"]["records"][0]["recipients"]["10"]
    begin_attempt(recipient, 1000)
    complete_attempt(recipient, "confirmed_success", 1001)
    begin_attempt(journal["outbox"]["records"][1]["recipients"]["10"], 1000)
    return compact_outbox(journal)


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_version", [1, 2, 3])
@pytest.mark.parametrize("retained", [False, True])
async def test_compacted_coherent_backup_preserves_plans_corrections_and_schedule(
    backup_env, journal_factory, plan_version, retained,
):
    from event_time_stats import (
        ensure_event_time,
        project_event,
        report_revisions,
        rotate_event_time,
    )

    journal = _compacted_recovery(journal_factory)
    if retained:
        from notification_outbox import retain_outbox
        journal = retain_outbox(journal)
    cur = _journal_current(journal, applied=0)
    ensure_event_time(cur)
    for seq in (1, 2):
        project_event(cur, journal, seq)
        cur["event_projection"]["applied_seq"] = seq
    fresh = storage._empty_stats_current("2026-Q3")
    fresh["event_projection"] = dict(cur["event_projection"])
    units = [{"transport": "html", "content": "frozen report", "disable_preview": False}]
    if plan_version == 1:
        plan = storage.new_quarter_delivery("2026-Q2", "2026-Q3", ["frozen report"])
    else:
        plan = storage.new_quarter_delivery_plan(
            "2026-Q2", "2026-Q3", units,
            event_time_revisions=report_revisions(cur) if plan_version == 3 else None,
        )
    fresh["pending_quarter_delivery"] = plan
    rotation_plan = plan if plan_version == 3 else storage.new_quarter_delivery_plan(
        "2026-Q2", "2026-Q3", [], event_time_revisions=report_revisions(cur),
    )
    rotate_event_time(cur, fresh, rotation_plan)
    if plan_version != 3:
        fresh["event_time"]["report_ack"] = None
    storage.save_event_journal(journal)
    storage.save_stats_current(fresh, strict=True)
    await storage.mutate_subscription(10, "active", subscribed=True)
    schedule = storage.load_subscriber_state(strict_subscribers=True)
    raw, _ = await backup._build_backup_zip()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        from notification_progress_schema import parse_recovery_journal
        assert parse_recovery_journal(
            archive.read("event_journal.json"), archive.read("notification_progress.json"),
        ) == journal
        assert json.loads(archive.read("stats_current.json")) == fresh
    await backup.restore_backup_zip(raw)
    assert storage.load_event_journal() == journal
    assert storage.load_stats_current(strict=True) == fresh
    restored = storage.load_subscriber_state(strict_subscribers=True)
    assert restored.backup_schedule == schedule.backup_schedule
    assert restored.notification_memberships == schedule.notification_memberships


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["identical", "older", "full", "unrelated", "legacy"])
@pytest.mark.parametrize("retained", [False, True])
async def test_restore_invalidates_compaction_snapshot_and_preserves_authority(
    backup_env, journal_factory, kind, retained,
):
    from notification_outbox import enqueue

    journal = _compacted_recovery(journal_factory)
    if retained:
        from notification_outbox import retain_outbox
        journal = retain_outbox(journal)
    # Добавляем ещё одну compactable decision; предыдущая summary уже опубликована.
    recipient = journal["outbox"]["records"][-1]["recipients"]["10"]
    recipient.update(status="cancelled", reason="ineligible", terminal_at=1001)
    storage.save_event_journal(journal)
    cur = _journal_current(journal)
    storage.save_stats_current(cur, strict=True)
    generation = storage.restorable_restore_generation()
    if kind == "unrelated":
        raw = _zip_bytes({"user_alerts.json": '{"enabled":false}'})
    elif kind == "legacy":
        raw = _zip_bytes({"stats_current.json": json.dumps(storage._empty_stats_current("2026-Q2"))})
    elif kind == "older":
        older = journal_factory(count=0)
        raw = _recovery_zip(older, _journal_current(older))
    elif kind == "full":
        replacement = journal_factory(count=1)
        replacement["journal_id"] = "c" * 32
        from notification_outbox import migrate_outbox
        replacement = migrate_outbox(replacement, 0)
        replacement = enqueue(replacement, replacement["events"][0], "replacement", {}, 1000)
        raw = _recovery_zip(replacement, _journal_current(replacement))
    else:
        raw = _recovery_zip(journal, cur)
    await backup.restore_backup_zip(raw)
    exact = storage.EVENT_JOURNAL_FILE.read_bytes()
    async with storage.restorable_state_transaction():
        with pytest.raises(storage.EventJournalStateError, match="compaction_changed"):
            storage.compact_event_journal(journal, expected_generation=generation)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == exact
    if kind in {"legacy", "unrelated", "identical"}:
        assert storage.load_event_journal() == journal


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["invalid", "oversized", "publication"])
@pytest.mark.parametrize("retained", [False, True])
async def test_compacted_import_rejection_and_exact_byte_rollback(
    backup_env, journal_factory, monkeypatch, damage, retained,
):
    journal = _compacted_recovery(journal_factory)
    if retained:
        from notification_outbox import retain_outbox
        journal = retain_outbox(journal)
    cur = _journal_current(journal)
    before_journal = b"\xffbroken journal\r\n"
    before_current = b"{ broken quarter\r\n"
    storage.EVENT_JOURNAL_FILE.write_bytes(before_journal)
    storage.STATS_CURRENT_FILE.write_bytes(before_current)
    if damage == "invalid":
        if retained:
            journal["outbox"]["completed_seq"] = True
        else:
            journal["outbox"]["records"][0]["outcomes"]["pending"] = {"count": 1}
    elif damage == "oversized":
        monkeypatch.setattr("backup._IMPORT_MEMBER_MAX_BYTES", len(json.dumps(journal).encode()) - 1)
    else:
        publish = backup._publish_staged_file

        def fail(source, target):
            if target.name == "user_alerts.json":
                raise OSError("publication")
            return publish(source, target)

        monkeypatch.setattr("backup._publish_staged_file", fail)
    raw = _zip_bytes({
        "event_journal.json": json.dumps(journal), "stats_current.json": json.dumps(cur),
        "user_alerts.json": '{"enabled":false}',
    })
    generation = storage.restorable_restore_generation()
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(raw)
    assert storage.EVENT_JOURNAL_FILE.read_bytes() == before_journal
    assert storage.STATS_CURRENT_FILE.read_bytes() == before_current
    assert not storage.USER_ALERTS_FILE.exists()
    assert storage.restorable_restore_generation() == generation


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["unsupported", "null", "subscriber", "missing_token", "huge_chat"])
async def test_restore_rejects_invalid_notification_memberships(backup_env, damage):
    storage.save_subscribers({10: "keep"})
    before = storage.SUBS_FILE.read_bytes()
    payload = json.loads(before)
    if damage == "unsupported":
        payload["notification_memberships"]["version"] = 2
    elif damage == "null":
        payload["notification_memberships"] = None
    elif damage == "subscriber":
        payload["subscribers"]["10"] = True
    elif damage == "huge_chat":
        payload["subscribers"]["1" * 5000] = "invalid"
    else:
        payload["notification_memberships"]["tokens"] = {}
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(_zip_bytes({"subscribers.json": json.dumps(payload), "user_alerts.json": '{"enabled":false}'}))
    assert storage.SUBS_FILE.read_bytes() == before
    assert not storage.USER_ALERTS_FILE.exists()


@pytest.mark.asyncio
async def test_restore_rechecks_capacity_after_legacy_membership_migration(backup_env, monkeypatch):
    before = b'{"subscribers":{"10":"keep"}}\r\n'
    storage.SUBS_FILE.write_bytes(before)
    candidate = '{"subscribers":{"20":"new"}}'
    monkeypatch.setattr("backup._IMPORT_MEMBER_MAX_BYTES", len(candidate.encode()) + 1)
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(_zip_bytes({"subscribers.json": candidate}))
    assert storage.SUBS_FILE.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_kind", ["identical", "unrelated", "older"])
@pytest.mark.parametrize("phase", ["fetch", "admission", "send", "retry"])
async def test_history_restore_invalidates_fetch_send_and_retry(
    backup_env, journal_factory, monkeypatch, restore_kind, phase,
):
    from aiogram.exceptions import TelegramServerError
    from aiogram.methods import SendMessage

    journal = journal_factory(count=0)
    cur = _journal_current(journal)
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    storage.save_subscribers({10: "recipient", 20: "next recipient"})
    if phase == "admission":
        monkeypatch.setattr("handlers.JOURNAL_WARN_BYTES", len(storage.EVENT_JOURNAL_FILE.read_bytes()) + 1)
        monkeypatch.setattr("handlers._last_journal_capacity_notice_at", None)
    attempts = []
    restores = []

    async def restore():
        if restores:
            return
        restores.append(True)
        if restore_kind == "unrelated":
            raw = _zip_bytes({"user_alerts.json": '{"enabled":false}'})
        elif restore_kind == "identical":
            raw = _recovery_zip(storage.load_event_journal(), storage.load_stats_current(strict=True))
        else:
            raw = _recovery_zip(journal, cur)
        await backup.restore_backup_zip(raw)

    entry = {"id": 2, "description": "Просмотрено", "target": {"id": 11, "kind": "tv"}}

    async def fetch(_session, page=1):
        if phase == "fetch":
            await restore()
        return [entry]

    async def send(**kwargs):
        assert not storage._restorable_state_lock().locked()
        if phase == "admission" and kwargs["chat_id"] == handlers.OWNER_ID:
            await restore()
            return
        attempts.append(kwargs)
        if phase == "send":
            await restore()
        if phase == "retry":
            raise TelegramServerError(method=SendMessage(chat_id=10, text="event"), message="temporary")

    async def sleep(_delay):
        if phase == "retry":
            await restore()

    monkeypatch.setattr("handlers.fetch_history", fetch)
    monkeypatch.setattr("handlers.asyncio.sleep", sleep)
    bot = AsyncMock()
    bot.send_message.side_effect = send
    await handlers.check_and_notify(bot, {999}, cur)
    if phase in {"send", "retry"}:
        from notification_delivery import dispatch_notifications
        await dispatch_notifications(bot)
    restored_journal = storage.load_event_journal()
    assert restored_journal["processed_seq"] == (1 if phase in {"send", "retry"} and restore_kind != "older" else 0)
    assert len(attempts) == (0 if phase in {"fetch", "admission"} else 1)
    if phase == "fetch" or restore_kind == "older":
        assert restored_journal["events"] == []
    else:
        assert len(restored_journal["events"]) == 1
        assert storage.load_stats_current(strict=True)["event_projection"]["applied_seq"] == (0 if phase == "admission" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["subscription", "weekly"])
async def test_uncertain_backup_then_rejection_preserves_automatic_schedule(
    backup_env, monkeypatch, kind,
):
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendDocument

    monkeypatch.setattr("telegram_delivery._sleep", AsyncMock())
    old = time.time() - backup.WEEKLY_BACKUP_INTERVAL - 100
    pending = {"subscriptions": 1, "unsubscriptions": 0, "counts_known": True, "token": uuid4().hex}
    _save_subscriber_schedule(last_backup_at=old, weekly_started_at=old, pending=pending if kind == "subscription" else None)
    original = storage.load_subscription_backup_state()
    monkeypatch.setattr("backup._last_backup_sent_at", None)
    bot = AsyncMock()
    bot.send_document.side_effect = [
        TimeoutError(),
        TelegramForbiddenError(method=SendDocument(chat_id=999, document="test"), message="forbidden"),
    ]
    cur = {"period": "2026-Q2", "events": []}
    if kind == "subscription":
        await backup._backup_after_subscription(bot)
    else:
        await backup._weekly_backup_if_due(bot, cur)
    assert storage.load_subscription_backup_state() == original
    assert backup._last_backup_sent_at is None
    assert bot.send_document.await_count == 2
    documents = [call.kwargs["document"] for call in bot.send_document.await_args_list]
    assert documents[0] is not documents[1]
    assert documents[0].data == documents[1].data


def _split_recovery_members(journal_factory):
    from notification_progress_schema import (
        compact_json,
        history_document,
        progress_document,
    )
    journal = _compacted_recovery(journal_factory)
    return journal, {
        "event_journal.json": compact_json(history_document(journal, "d" * 32)),
        "notification_progress.json": compact_json(progress_document(journal, "d" * 32)),
        "stats_current.json": json.dumps(_journal_current(journal)),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("split", [False, True])
async def test_capacity_control_coherent_restore_and_exact_rollback(
    backup_env, outbox_capacity_factory, monkeypatch, split,
):
    from notification_progress_schema import (
        compact_json,
        history_document,
        parse_recovery_journal,
        progress_document,
    )
    journal = outbox_capacity_factory()
    monkeypatch.setattr("backup.SHIKI_USER", journal["profile"])
    monkeypatch.setattr("storage.SHIKI_USER", journal["profile"])
    members = {"stats_current.json": json.dumps(_journal_current(journal))}
    if split:
        members["event_journal.json"] = compact_json(history_document(journal, "d" * 32))
        members["notification_progress.json"] = compact_json(progress_document(journal, "d" * 32))
    else:
        members["event_journal.json"] = compact_json(journal)
    originals = {
        storage.EVENT_JOURNAL_FILE: b"\xffdamaged history\r\n",
        storage.notification_progress_file(): b"\xffdamaged progress\r\n",
        storage.STATS_CURRENT_FILE: b"damaged quarter\r\n",
    }
    for path, raw in originals.items():
        path.write_bytes(raw)
    generation = storage.restorable_restore_generation()

    def fail_publication(*args):
        raise OSError("publish")

    with monkeypatch.context() as patch:
        patch.setattr("backup._publish_staged_file", fail_publication)
        with pytest.raises(ValueError, match="исходное состояние восстановлено"):
            await backup.restore_backup_zip(_zip_bytes(members))
    for path, raw in originals.items():
        assert path.read_bytes() == raw
    assert storage.restorable_restore_generation() == generation
    await backup.restore_backup_zip(_zip_bytes(members))
    assert storage.load_event_journal() == journal
    assert storage.restorable_restore_generation() == generation + 1
    raw, _ = await backup._build_backup_zip()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        restored = parse_recovery_journal(
            archive.read("event_journal.json"),
            archive.read("notification_progress.json") if split else None,
        )
    assert restored == journal


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_progress", "missing_history", "missing_current", "lineage", "bad", "bad_history", "orphan"])
async def test_split_recovery_rejects_missing_or_incompatible_members_before_publication(
    backup_env, journal_factory, damage,
):
    _, members = _split_recovery_members(journal_factory)
    if damage.startswith("missing_"):
        member = {"missing_progress": "notification_progress.json", "missing_history": "event_journal.json", "missing_current": "stats_current.json"}[damage]
        del members[member]
    elif damage == "lineage":
        obj = json.loads(members["notification_progress.json"])
        obj["progress_id"] = "e" * 32
        members["notification_progress.json"] = json.dumps(obj)
    elif damage in {"bad", "bad_history"}:
        member = "event_journal.json" if damage == "bad_history" else "notification_progress.json"
        members[member] = '{"version":1,"version":1}'
    else:
        members["event_journal.json"] = json.dumps(journal_factory())
    storage.USER_ALERTS_FILE.write_bytes(b'{"enabled":true}\r\n')
    members["user_alerts.json"] = '{"enabled":false}'
    before = storage.USER_ALERTS_FILE.read_bytes()
    with pytest.raises(ValueError) as error:
        await backup.restore_backup_zip(_zip_bytes(members))
    if damage in {"bad", "bad_history"}:
        assert str(error.value) == f"Файл {member} в архиве повреждён; восстановление отменено"
    assert storage.USER_ALERTS_FILE.read_bytes() == before
    assert not storage.EVENT_JOURNAL_FILE.exists()
    assert not storage.notification_progress_file().exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_progress", [False, True])
@pytest.mark.parametrize("failure_at", [1, 2, 3])
async def test_split_restore_rolls_back_exact_bytes_and_removes_created_members(
    backup_env, journal_factory, monkeypatch, existing_progress, failure_at,
):
    _, members = _split_recovery_members(journal_factory)
    originals = {
        storage.EVENT_JOURNAL_FILE: b"\xffdamaged history\r\n",
        storage.STATS_CURRENT_FILE: b"damaged quarter\r\n",
    }
    if existing_progress:
        originals[storage.notification_progress_file()] = b"\xffdamaged progress\r\n"
    for path, value in originals.items():
        path.write_bytes(value)
    publish = backup._publish_staged_file
    calls = []

    def fail(source, target):
        calls.append(target)
        if len(calls) == failure_at:
            raise OSError("publication")
        publish(source, target)

    monkeypatch.setattr("backup._publish_staged_file", fail)
    with pytest.raises(ValueError, match="исходное состояние восстановлено"):
        await backup.restore_backup_zip(_zip_bytes(members))
    for path, value in originals.items():
        assert path.read_bytes() == value
    if not existing_progress:
        assert not storage.notification_progress_file().exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("full_export", [False, True])
async def test_unactivated_migration_preparation_is_not_exported(backup_env, journal_factory, full_export):
    journal = _compacted_recovery(journal_factory)
    storage.EVENT_JOURNAL_FILE.write_text(json.dumps(journal), encoding="utf-8")
    storage.save_stats_current(_journal_current(journal), strict=True)
    # Неактивированные данные не занимают ни member, ни общий бюджет capture.
    storage.notification_progress_file().write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    raw, _ = await backup._build_backup_zip(full_export=full_export)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert "notification_progress.json" not in archive.namelist()
        assert json.loads(archive.read("event_journal.json")) == journal
    await backup.restore_backup_zip(raw)
    assert not storage.notification_progress_file().exists()
    assert storage.load_event_journal() == journal


@pytest.mark.asyncio
@pytest.mark.parametrize(("version", "has_progress"), [(True, True), (1.0, True), (4.0, False)])
async def test_backup_preserves_damaged_history_without_interpreting_noninteger_version(
    backup_env, journal_factory, version, has_progress,
):
    history_raw = json.dumps({**journal_factory(), "version": version}).encode("utf-8")
    storage.EVENT_JOURNAL_FILE.write_bytes(history_raw)
    progress_raw = b'{"diagnostic":true}\r\n'
    if has_progress:
        storage.notification_progress_file().write_bytes(progress_raw)
    raw, _ = await backup._build_backup_zip()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("event_journal.json") == history_raw
        assert ("notification_progress.json" in archive.namelist()) is has_progress
        if has_progress:
            assert archive.read("notification_progress.json") == progress_raw


@pytest.mark.asyncio
@pytest.mark.parametrize("source_compact", [False, True])
async def test_split_backup_capture_freezes_progress_before_concurrent_writer(
    backup_env, journal_factory, source_history_factory, monkeypatch, source_compact,
):
    from notification_outbox import (
        begin_attempt,
        complete_attempt,
    )
    from notification_progress_schema import parse_recovery_journal

    if source_compact:
        journal, cur = source_history_factory(count=8, pending_from=6)
        begin_attempt(journal["outbox"]["records"][1]["recipients"]["10"], 1000)
        storage.save_event_journal(journal)
        storage.save_stats_current(cur, strict=True)
        journal = storage.compact_completed_history(
            journal, storage.load_stats_current(strict=True),
            expected_generation=storage.restorable_restore_generation(), force=True,
        )
    else:
        journal = _compacted_recovery(journal_factory)
        storage.save_event_journal(journal)
        storage.save_stats_current(_journal_current(journal), strict=True)
    capture_started, capture_resume = threading.Event(), threading.Event()
    writer_done = asyncio.Event()
    capture = backup._read_backup_members

    def pause(*args):
        if not capture_started.is_set():
            capture_started.set()
            assert capture_resume.wait(5)
        return capture(*args)

    async def writer():
        async with storage.restorable_state_transaction():
            current = storage.load_event_journal()
            complete_attempt(current["outbox"]["records"][1]["recipients"]["10"], "confirmed_success", 1001)
            storage.save_event_journal(current)
        writer_done.set()

    monkeypatch.setattr("backup._read_backup_members", pause)
    task = asyncio.create_task(backup._build_backup_zip())
    while not capture_started.is_set():
        await asyncio.sleep(0)
    writer_task = asyncio.create_task(writer())
    await asyncio.sleep(0)
    assert not writer_done.is_set()
    capture_resume.set()
    raw, _ = await task
    await writer_task
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert parse_recovery_journal(archive.read("event_journal.json"), archive.read("notification_progress.json")) == journal
    assert storage.load_event_journal()["outbox"]["records"][1]["recipients"]["10"]["status"] == "delivered"


@pytest.mark.asyncio
async def test_large_compact_current_roundtrips_complete_v6_backup(
    backup_env, stats_capacity_factory,
):
    from event_journal_schema import validate_recovery_set

    journal, cur = stats_capacity_factory(count=15000)
    old_raw = json.dumps(cur, ensure_ascii=False, indent=2).replace("\n", "\r\n").encode("utf-8")
    assert len(old_raw) > backup._IMPORT_MEMBER_MAX_BYTES
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    current_raw = storage.STATS_CURRENT_FILE.read_bytes()
    assert len(current_raw) < backup._IMPORT_MEMBER_MAX_BYTES
    assert json.loads(storage.EVENT_JOURNAL_FILE.read_bytes())["version"] == 6
    archive, _ = await backup._build_backup_zip()
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        assert zipped.read("stats_current.json") == current_raw
        assert {"event_journal.json", "notification_progress.json", "stats_current.json"} <= set(zipped.namelist())
    await backup.restore_backup_zip(archive)
    assert storage.load_stats_current(strict=True) == cur
    assert storage.load_event_journal() == journal
    validate_recovery_set(storage.load_event_journal(), storage.load_stats_current(strict=True))
    storage.save_stats_current(storage.load_stats_current(strict=True), strict=True)
    assert storage.STATS_CURRENT_FILE.read_bytes() == current_raw


@pytest.mark.asyncio
async def test_physically_oversized_current_rejects_before_compact_restore(
    backup_env,
):
    original = b'{"period":"2026-Q2","events":[]}\r\n'
    storage.STATS_CURRENT_FILE.write_bytes(original)
    raw = original + b" " * (backup._IMPORT_MEMBER_MAX_BYTES + 1 - len(original))
    generation = storage.restorable_restore_generation()
    archive = _zip_bytes({"subscribers.json": '{"subscribers":{"10":"new"}}', "stats_current.json": raw})
    with pytest.raises(ValueError, match="больше 8 МиБ"):
        await backup.restore_backup_zip(archive)
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    assert not storage.SUBS_FILE.exists()
    assert storage.restorable_restore_generation() == generation
    storage.STATS_CURRENT_FILE.write_bytes(raw)
    with pytest.raises(storage.QuarterDeliveryStateError, match="current_size"):
        storage.load_stats_current(strict=True)
    assert storage.STATS_CURRENT_FILE.read_bytes() == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("eol", ["\n", "\r\n"])
async def test_current_import_reserves_legacy_migration_and_future_writes(
    backup_env, monkeypatch, eol,
):
    from copy import deepcopy

    cur = {"period": "2026-Q2", "events": [], "last_report_sent": None, "tracking_since": "2026-04-01T00:00:00+00:00", "pending_quarter_delivery": {
        "old_period": "2026-Q1", "new_period": "2026-Q2", "report_messages": ["frozen", "続き"], "report_sent": False,
    }}
    future = deepcopy(cur)
    future["pending_quarter_delivery"] = storage.migrate_quarter_delivery(cur["pending_quarter_delivery"])
    future["pending_quarter_delivery"].update(next_unit=1, delivery_uncertain=False)
    boundary = len(json.dumps(future, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    archive = _zip_bytes({"stats_current.json": json.dumps(cur, ensure_ascii=False, indent=2).replace("\n", eol)})
    original = b'{"period":"2026-Q2","events":[]}\r\n'
    storage.STATS_CURRENT_FILE.write_bytes(original)
    generation = storage.restorable_restore_generation()
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", boundary - 1)
    with pytest.raises(ValueError, match="current_capacity"):
        await backup.restore_backup_zip(archive)
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    assert storage.restorable_restore_generation() == generation
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", boundary)
    await backup.restore_backup_zip(archive)
    loaded = storage.load_stats_current(strict=True)
    assert loaded == cur
    assert b"\n" not in storage.STATS_CURRENT_FILE.read_bytes()
    storage.save_stats_current(future, strict=True)
    assert storage.load_stats_current(strict=True) == future


@pytest.mark.asyncio
async def test_current_restore_rechecks_reserve_after_legacy_journal_binding(
    backup_env, journal_factory, monkeypatch,
):
    journal = journal_factory(count=0)
    storage.save_event_journal(journal)
    cur = {"period": "2026-Q2", "events": [], "last_report_sent": None, "tracking_since": "2026-04-01T00:00:00+00:00", "pending_quarter_delivery": storage.new_quarter_delivery("2026-Q1", "2026-Q2", ["frozen"])}
    bound = {**cur, "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0}}
    marker = json.loads(json.dumps(bound))
    marker["pending_quarter_delivery"]["delivery_uncertain"] = False
    boundary = len(json.dumps(marker, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert len(json.dumps(bound, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < boundary
    original = {p.name: p.read_bytes() for p in backup_env.iterdir() if p.is_file()}
    archive = _zip_bytes({"stats_current.json": json.dumps(cur, ensure_ascii=False, separators=(",", ":"))})
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", boundary - 1)
    with pytest.raises(ValueError, match="current_capacity"):
        await backup.restore_backup_zip(archive)
    assert {p.name: p.read_bytes() for p in backup_env.iterdir() if p.is_file()} == original
    monkeypatch.setattr("storage.JOURNAL_MAX_BYTES", boundary)
    await backup.restore_backup_zip(archive)
    storage.save_stats_current(storage.load_stats_current(strict=True), strict=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_kind", ["complete", "legacy_quarter", "old_complete"])
async def test_source_backup_coherent_roundtrip_and_older_restore_compatibility(
    backup_env, source_history_factory, restore_kind,
):
    from event_journal_schema import validate_recovery_set
    from notification_progress_schema import parse_recovery_journal
    full, cur = source_history_factory(count=8, pending_from=6)
    storage.save_event_journal(full)
    storage.save_stats_current(cur, strict=True)
    compact = storage.compact_completed_history(
        full, storage.load_stats_current(strict=True),
        expected_generation=storage.restorable_restore_generation(), force=True,
    )
    history_raw = storage.EVENT_JOURNAL_FILE.read_bytes()
    progress_raw = storage.notification_progress_file().read_bytes()
    raw, generation = await backup._build_backup_zip()
    assert generation == storage.restorable_restore_generation()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("event_journal.json") == history_raw
        assert archive.read("notification_progress.json") == progress_raw
        captured = parse_recovery_journal(archive.read("event_journal.json"), archive.read("notification_progress.json"))
        validate_recovery_set(captured, json.loads(archive.read("stats_current.json")))
        assert captured == compact
    if restore_kind == "complete":
        await backup.restore_backup_zip(raw)
        assert storage.load_event_journal() == compact
        assert storage.load_event_journal()["outbox"] == full["outbox"]
    elif restore_kind == "legacy_quarter":
        legacy = {"period": "2026-Q2", "events": [{"id": "legacy", "event": "planned"}]}
        await backup.restore_backup_zip(_zip_bytes({"stats_current.json": json.dumps(legacy)}))
        assert storage.EVENT_JOURNAL_FILE.read_bytes() == history_raw
        assert storage.notification_progress_file().read_bytes() == progress_raw
        current = storage.load_stats_current(strict=True)
        assert current["event_projection"]["baseline_seq"] == 8
        validate_recovery_set(storage.load_event_journal(), current)
        await handlers._drain_history_journal(AsyncMock())
        assert storage.load_stats_current(strict=True)["events"] == legacy["events"]
    else:
        await backup.restore_backup_zip(_recovery_zip(full, cur))
        assert storage.load_event_journal() == full
        assert not storage.notification_progress_file().exists()
    assert storage.restorable_restore_generation() > generation


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["base", "projection", "progress", "missing_current", "missing_progress", "missing_history"])
async def test_source_backup_import_and_capture_share_full_recovery_validation(
    backup_env, source_history_factory, damage,
):
    full, cur = source_history_factory()
    storage.save_event_journal(full)
    storage.save_stats_current(cur, strict=True)
    storage.compact_completed_history(
        full, storage.load_stats_current(strict=True),
        expected_generation=storage.restorable_restore_generation(), force=True,
    )
    members = {
        name: (backup_env / name).read_bytes()
        for name in ["event_journal.json", "notification_progress.json", "stats_current.json"]
    }
    if damage == "base":
        history = json.loads(members["event_journal.json"])
        history["source_base"]["checksum"] = "f" * 64
        members["event_journal.json"] = json.dumps(history).encode()
    elif damage == "projection":
        current = json.loads(members["stats_current.json"])
        current["event_time"]["periods"]["2026-Q1"]["events"][0]["score"] = 9
        members["stats_current.json"] = json.dumps(current).encode()
    elif damage == "progress":
        progress = json.loads(members["notification_progress.json"])
        progress["progress_id"] = "f" * 32
        members["notification_progress.json"] = json.dumps(progress).encode()
    else:
        members.pop({
            "missing_current": "stats_current.json", "missing_progress": "notification_progress.json",
            "missing_history": "event_journal.json",
        }[damage])
    original = {path.name: path.read_bytes() for path in backup_env.iterdir() if path.is_file()}
    generation = storage.restorable_restore_generation()
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(_zip_bytes(members))
    assert {path.name: path.read_bytes() for path in backup_env.iterdir() if path.is_file()} == original
    assert storage.restorable_restore_generation() == generation
    for name in original.keys() - members.keys():
        (backup_env / name).unlink()
    for name, raw in members.items():
        (backup_env / name).write_bytes(raw)
    with pytest.raises(ValueError):
        await backup._build_backup_zip()


@pytest.mark.asyncio
@pytest.mark.parametrize("current_raw", [
    pytest.param(b"null", id="null"),
    pytest.param(b"[]", id="list"),
    pytest.param(b'"text"', id="string"),
    pytest.param(b"42", id="number"),
    pytest.param(b"false", id="bool"),
    pytest.param(b"{broken", id="json"),
    pytest.param(b"\xffbroken", id="encoding"),
    pytest.param(b"[" * 10000 + b"0" + b"]" * 10000, id="depth"),
])
async def test_source_backup_reports_malformed_current_without_changing_recovery_bytes(
    backup_env, source_history_factory, current_raw,
):
    full, cur = source_history_factory()
    storage.save_event_journal(full)
    storage.save_stats_current(cur, strict=True)
    storage.compact_completed_history(
        full, storage.load_stats_current(strict=True),
        expected_generation=storage.restorable_restore_generation(), force=True,
    )
    storage.STATS_CURRENT_FILE.write_bytes(current_raw)
    before = {path.name: path.read_bytes() for path in backup_env.iterdir() if path.is_file()}
    generation = storage.restorable_restore_generation()
    with pytest.raises(ValueError, match="Текущий квартал повреждён"):
        await backup._build_backup_zip()
    assert {path.name: path.read_bytes() for path in backup_env.iterdir() if path.is_file()} == before
    assert storage.restorable_restore_generation() == generation


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("full_export", [False, True])
async def test_source_recovery_preserves_frozen_plans_and_later_correction_ack(
    backup_env, source_history_factory, version, full_export,
):
    from copy import deepcopy

    from event_time_stats import (
        acknowledge_revisions,
        correction_periods,
        project_event,
    )
    from notification_outbox import enqueue
    from source_history import content_hash
    full, cur = source_history_factory(count=8, pending_from=6)
    if version == 1:
        plan = storage.new_quarter_delivery("2026-Q1", "2026-Q2", ["frozen one", "frozen two"])
    else:
        units = [{"transport": "html", "content": text, "disable_preview": False} for text in ["frozen one", "frozen two"]]
        revisions = {"2026-Q1": cur["event_time"]["periods"]["2026-Q1"]["revision"]} if version == 3 else None
        plan = storage.new_quarter_delivery_plan("2026-Q1", "2026-Q2", units, event_time_revisions=revisions)
        if version == 3:
            cur["event_time"]["report_ack"] = {"plan_id": plan["plan_id"], "revisions": revisions}
    plan.update(next_unit=1, delivery_uncertain=True)
    cur["pending_quarter_delivery"] = plan
    storage.save_event_journal(full)
    storage.save_stats_current(cur, strict=True)
    compact = storage.compact_completed_history(
        full, storage.load_stats_current(strict=True),
        expected_generation=storage.restorable_restore_generation(), force=True,
    )
    plan_before = content_hash(plan)
    archive, _ = await backup._build_backup_zip(full_export=full_export)
    await backup.restore_backup_zip(archive)
    restored = storage.load_stats_current(strict=True)
    assert content_hash(restored["pending_quarter_delivery"]) == plan_before
    assert storage.load_event_journal() == compact
    if version == 3:
        # После freeze приходит снятие оценки: старый plan подтверждает только свою ревизию.
        event = {**deepcopy(full["events"][-1]), "seq": 9, "history_id": 100, "event_type": "score_removed", "score": None}
        compact["events"].append(event)
        storage.save_event_journal(compact, admitting=True)
        project_event(restored, compact, 9)
        restored["event_projection"]["applied_seq"] = 9
        storage.save_stats_current(restored, strict=True)
        compact = enqueue(compact, event, None, {}, 1001)
        storage.save_event_journal(compact)
        restored["pending_quarter_delivery"].update(next_unit=2, delivery_uncertain=False)
        restored["last_report_sent"] = "2026-Q2"
        acknowledge_revisions(restored)
        storage.save_stats_current(restored, strict=True)
        bucket = restored["event_time"]["periods"]["2026-Q1"]
        assert bucket["announced_revision"] == 1
        assert bucket["revision"] == 2
        assert correction_periods(restored) == ["2026-Q1"]
        archive, _ = await backup._build_backup_zip(full_export=full_export)
        await backup.restore_backup_zip(archive)
        assert storage.load_stats_current(strict=True) == restored


@pytest.mark.asyncio
@pytest.mark.parametrize("full_export", [False, True])
@pytest.mark.parametrize("ready", [False, True])
async def test_archives_restore_legacy_digest_plan_without_upgrading(backup_env, legacy_digest_factory, monkeypatch, full_export, ready):
    from copy import deepcopy

    from notification_outbox import begin_attempt

    journal = legacy_digest_factory(ready=False)
    storage.save_stats_current({"period": "2026-Q2", "events": [], "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0}}, strict=True)
    storage.save_event_journal(journal)
    monkeypatch.setattr("handlers.render_digest", lambda *a, **k: pytest.fail("legacy plan rerender"))
    if ready:
        await handlers._drain_history_journal(AsyncMock())
        journal = storage.load_event_journal()
        begin_attempt(journal["outbox"]["plans"][0]["units"][0]["recipients"]["10"], 1800000000.0)
        storage.save_event_journal(journal)
    before = deepcopy(storage.load_event_journal())
    archive, _ = await backup._build_backup_zip(full_export=full_export)
    await backup.restore_backup_zip(archive)
    assert storage.load_event_journal() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_name", ["event_journal.json", "notification_progress.json", "stats_current.json"])
async def test_source_restore_failure_rolls_back_exact_bytes_and_cache(
    backup_env, source_history_factory, monkeypatch, fail_name,
):
    full, cur = source_history_factory(count=8, pending_from=6)
    storage.save_event_journal(full)
    storage.save_stats_current(cur, strict=True)
    storage.compact_completed_history(
        full, storage.load_stats_current(strict=True),
        expected_generation=storage.restorable_restore_generation(), force=True,
    )
    archive, _ = await backup._build_backup_zip()
    # Не decode/normalize: исходные повреждённые байты также откатываются точно.
    original = {
        "event_journal.json": b" damaged history\r\n",
        "notification_progress.json": b" damaged progress\r\n",
        "stats_current.json": b" damaged current\r\n",
    }
    for name, raw in original.items():
        (backup_env / name).write_bytes(raw)
    generation = storage.restorable_restore_generation()
    replace = backup._publish_staged_file
    failed = False

    def publish(src, dst):
        nonlocal failed
        if not failed and dst.name == fail_name:
            failed = True
            raise OSError("restore publication")
        return replace(src, dst)

    monkeypatch.setattr("backup._publish_staged_file", publish)
    with pytest.raises(ValueError, match="исходное состояние восстановлено"):
        await backup.restore_backup_zip(archive)
    assert failed
    assert {name: (backup_env / name).read_bytes() for name in original} == original
    assert storage.restorable_restore_generation() == generation


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("full_export", [False, True])
@pytest.mark.parametrize("ready", [False, True])
async def test_digest_archives_preserve_prepared_ready_and_quarterly_plans(backup_env, digest_factory, version, full_export, ready):
    from copy import deepcopy

    from event_time_stats import ensure_event_time

    journal = digest_factory(ready=False, long_title=True)
    cur = {"period": "2026-Q1", "events": [], "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0}}
    ensure_event_time(cur)
    cur["period"] = "2026-Q2"
    storage.save_stats_current(cur, strict=True)
    storage.save_event_journal(journal)
    if ready:
        await handlers._drain_history_journal(AsyncMock())
    cur = storage.load_stats_current(strict=True)
    revisions = {period: bucket["revision"] for period, bucket in cur["event_time"]["periods"].items()} if version == 3 else None
    if version == 1:
        plan = storage.new_quarter_delivery("2026-Q1", "2026-Q2", ["one", "two"])
    else:
        units = [{"transport": "html", "content": text, "disable_preview": False} for text in ["one", "two"]]
        plan = storage.new_quarter_delivery_plan("2026-Q1", "2026-Q2", units, event_time_revisions=revisions)
        if version == 3:
            cur["event_time"]["report_ack"] = {"plan_id": plan["plan_id"], "revisions": revisions}
    plan.update(next_unit=1, delivery_uncertain=True)
    cur["pending_quarter_delivery"] = plan
    storage.save_stats_current(cur, strict=True)
    storage.STATS_ALL_FILE.write_bytes(b'{"diagnostic":"exact bytes"}\r\n')
    before = deepcopy(storage.load_event_journal())
    archive, _ = await backup._build_backup_zip(full_export=full_export)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        assert ("stats_all.json" in zipped.namelist()) is full_export
        if full_export:
            assert zipped.read("stats_all.json") == storage.STATS_ALL_FILE.read_bytes()
    await backup.restore_backup_zip(archive)
    assert storage.load_event_journal() == before
    assert storage.load_stats_current(strict=True)["pending_quarter_delivery"] == plan
    assert storage.load_stats_current(strict=True)["event_time"].get("report_ack") == cur["event_time"].get("report_ack")
    if version == 3 and ready:
        from event_time_stats import (
            acknowledge_revisions,
            correction_periods,
        )
        restored = storage.load_event_journal()
        event = {**deepcopy(restored["events"][-1]), "seq": 11, "history_id": 100, "event_type": "score_removed", "score": None}
        restored["events"].append(event)
        storage.save_event_journal(restored, admitting=True)
        await handlers._drain_history_journal(AsyncMock())
        fresh = storage.load_stats_current(strict=True)
        assert fresh["pending_quarter_delivery"] == plan
        fresh["pending_quarter_delivery"].update(next_unit=2, delivery_uncertain=False)
        fresh["last_report_sent"] = "2026-Q2"
        acknowledge_revisions(fresh)
        storage.save_stats_current(fresh, strict=True)
        assert fresh["event_time"]["periods"]["2026-Q1"]["announced_revision"] == revisions["2026-Q1"]
        assert fresh["event_time"]["periods"]["2026-Q1"]["revision"] > revisions["2026-Q1"]
        assert correction_periods(fresh) == ["2026-Q1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_name", ["event_journal.json", "notification_progress.json", "stats_current.json"])
async def test_digest_restore_rolls_back_exact_damaged_bytes(backup_env, digest_factory, monkeypatch, fail_name):
    journal = digest_factory(ready=False)
    storage.save_stats_current({"period": "2026-Q2", "events": [], "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0}}, strict=True)
    storage.save_event_journal(journal)
    archive, _ = await backup._build_backup_zip()
    files = [storage.EVENT_JOURNAL_FILE, storage.notification_progress_file(), storage.STATS_CURRENT_FILE]
    before = {path: b'\xff damaged ' + path.name.encode() + b'\r\n' for path in files}
    for path, data in before.items():
        path.write_bytes(data)
    publish = backup._publish_staged_file

    def fail(source, target):
        if target.name == fail_name:
            raise OSError("publication failure")
        return publish(source, target)

    monkeypatch.setattr("backup._publish_staged_file", fail)
    generation = storage.restorable_restore_generation()
    with pytest.raises(ValueError):
        await backup.restore_backup_zip(archive)
    assert {path: path.read_bytes() for path in files} == before
    assert storage.restorable_restore_generation() == generation


@pytest.mark.asyncio
@pytest.mark.parametrize("full_export", [False, True])
async def test_digest_capture_rejects_malformed_plan_without_normalizing_bytes(backup_env, digest_factory, full_export):
    journal = digest_factory(ready=False)
    storage.save_stats_current({"period": "2026-Q2", "events": [], "event_projection": {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0}}, strict=True)
    storage.save_event_journal(journal)
    progress = storage.notification_progress_file()
    payload = json.loads(progress.read_bytes())
    payload["outbox"]["plans"][0]["units"][0]["events"].pop()
    damaged = json.dumps(payload).encode() + b'\r\n'
    progress.write_bytes(damaged)
    with pytest.raises(ValueError):
        await backup._build_backup_zip(full_export=full_export)
    assert progress.read_bytes() == damaged


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("kind", ["subscription", "weekly", "shutdown"])
async def test_automatic_backups_capture_unfinished_digest_and_keep_its_authority(backup_env, digest_factory, monkeypatch, ready, kind):
    from notification_progress_schema import parse_recovery_journal

    journal = digest_factory(ready=False)
    cur = storage._empty_stats_current("2026-Q2")
    cur["event_projection"] = {"journal_id": journal["journal_id"], "baseline_seq": 0, "applied_seq": 0}
    storage.save_stats_current(cur, strict=True)
    storage.save_event_journal(journal)
    old = time.time() - backup.WEEKLY_BACKUP_INTERVAL - 100
    _save_subscriber_schedule(last_backup_at=old, weekly_started_at=old)
    if ready:
        await handlers._drain_history_journal(AsyncMock())
    if kind == "subscription":
        await storage.mutate_subscription(7, "Neo", subscribed=True)
    before = storage.notification_progress_file().read_bytes()
    frozen = storage.load_event_journal()
    schedule = storage.load_subscription_backup_state()
    monkeypatch.setattr("backup._last_backup_sent_at", None)
    bot = AsyncMock()
    cur = storage.load_stats_current(strict=True)
    if kind == "subscription":
        assert await backup._backup_after_subscription(bot)
    elif kind == "weekly":
        assert await backup._weekly_backup_if_due(bot, cur) is cur
    else:
        await backup._shutdown_backup(bot)
    bot.send_document.assert_awaited_once()
    with zipfile.ZipFile(io.BytesIO(bot.send_document.await_args.kwargs["document"].data)) as archive:
        assert parse_recovery_journal(archive.read("event_journal.json"), archive.read("notification_progress.json")) == frozen
    assert storage.notification_progress_file().read_bytes() == before
    after = storage.load_subscription_backup_state()
    if kind == "shutdown":
        assert after == schedule
    else:
        assert after["last_backup_at"] > old and after["pending"] is None
