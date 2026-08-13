# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Тесты ветки backup: /backup (экспорт/импорт zip) + авто-бэкап состояния.

Дисциплина: каждый тест падает на непропатченном коде и проходит на
пропатченном. Полные aiogram-объекты — через unittest.mock; узкая поверхность —
ручными стабами. Файлы DATA_DIR редиректятся в tmp_path фикстурой backup_env.
"""
import io
import json
import lzma
import time
import zipfile
import zlib
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

import backup
import handlers
import main
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
    (backup_env / "stats_current.json").write_text('{"period": "2026-Q2"}', encoding="utf-8")
    (backup_env / "subscribers.json.tmp").write_text("garbage", encoding="utf-8")
    restore_stage = backup_env / ".restore-interrupted.tmp" / "new"
    storage._atomic_write(restore_stage / "subscribers.json", "staged")
    (backup_env / "quarters" / "2026-Q1.json").write_text('{"period": "2026-Q1"}', encoding="utf-8")

    raw = backup._build_backup_zip()
    names = set(zipfile.ZipFile(io.BytesIO(raw)).namelist())

    assert "subscribers.json" in names
    assert "stats_current.json" in names
    assert "quarters/2026-Q1.json" in names          # вложенность сохранена
    assert "subscribers.json.tmp" not in names       # *.tmp исключён
    assert not any(name.startswith(".restore-") for name in names)


# ─────────────────────────────────────────────────────────────
#  Белый список импорта / zip-slip
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "subscribers.json",
    "stats_current.json",
    "update_state.json",
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


# ─────────────────────────────────────────────────────────────
#  Восстановление
# ─────────────────────────────────────────────────────────────

def test_restore_round_trip(backup_env):
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"123": "Alice"}}',
        "stats_current.json": '{"period": "2026-Q2", "events": []}',
        "update_state.json": (
            '{"last_checked_at": null, "latest_version": "v1.2.0", '
            '"release_url": "https://release", "last_notified_version": "v1.2.0"}'
        ),
        "quarters/2026-Q1.json": '{"period": "2026-Q1"}',
        "seen_ids.json": '{"seen_ids": [1, 2, 3]}',   # должен быть отброшен
    })
    result = backup.restore_backup_zip(raw)

    assert set(result["restored"]) == {
        "subscribers.json",
        "stats_current.json",
        "update_state.json",
        "quarters/2026-Q1.json",
    }
    assert "seen_ids.json" in result["skipped"]
    # файлы реально записаны
    assert storage.load_subscribers() == {123: "Alice"}
    assert storage.load_update_state()["last_notified_version"] == "v1.2.0"
    assert (backup_env / "quarters" / "2026-Q1.json").exists()
    assert not (backup_env / "seen_ids.json").exists()


def test_restore_skips_corrupt_json(backup_env):
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"1": "Bob"}}',
        "stats_current.json": "{ это не json",   # битый — пропускаем
    })
    result = backup.restore_backup_zip(raw)
    assert "subscribers.json" in result["restored"]
    assert "stats_current.json" in result["skipped"]
    assert not (backup_env / "stats_current.json").exists()


def test_restore_bad_zip_raises(backup_env):
    with pytest.raises(ValueError):
        backup.restore_backup_zip(b"this is not a zip")


def test_restore_skips_corrupt_crc_member(backup_env):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("subscribers.json", '{"subscribers": {"1": "Bob"}}')
        zf.writestr("stats_current.json", '{"period": "2026-Q2", "events": []}')
    raw = _corrupt_stored_member(buf.getvalue(), "subscribers.json")

    result = backup.restore_backup_zip(raw)

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
def test_restore_skips_unreadable_zip_member(backup_env, monkeypatch, read_error):
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"1": "Bob"}}',
        "stats_current.json": '{"period": "2026-Q2", "events": []}',
    })
    real_read = zipfile.ZipFile.read

    def fail_selected_member(zf, name, *args, **kwargs):
        if name == "subscribers.json":
            raise read_error
        return real_read(zf, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", fail_selected_member)

    result = backup.restore_backup_zip(raw)

    assert "subscribers.json" in result["skipped"]
    assert result["restored"] == ["stats_current.json"]
    assert not (backup_env / "subscribers.json").exists()
    assert json.loads(
        (backup_env / "stats_current.json").read_text(encoding="utf-8")
    ) == {"period": "2026-Q2", "events": []}


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_restore_rejects_inexact_update_state_schema(backup_env, change):
    state = {
        "last_checked_at": None,
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
        backup.restore_backup_zip(raw)
    assert not (backup_env / "update_state.json").exists()


@pytest.mark.parametrize(
    "key",
    [
        "last_checked_at",
        "latest_version",
        "release_url",
        "last_notified_version",
    ],
)
def test_restore_rejects_non_string_update_state_value(backup_env, key):
    state = {
        "last_checked_at": None,
        "latest_version": "v1.2.0",
        "release_url": "https://release",
        "last_notified_version": "v1.2.0",
    }
    state[key] = 42
    raw = _zip_bytes({"update_state.json": json.dumps(state)})

    with pytest.raises(ValueError, match="нет валидных файлов"):
        backup.restore_backup_zip(raw)
    assert not (backup_env / "update_state.json").exists()


def test_restore_rolls_back_first_file_when_second_publish_fails(
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
        backup.restore_backup_zip(raw)

    assert storage.load_subscribers() == {1: "Old"}
    assert json.loads((backup_env / "stats_current.json").read_text(encoding="utf-8")) == {
        "period": "old",
        "events": [],
    }


def test_restore_removes_new_file_when_second_publish_fails(
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
        backup.restore_backup_zip(raw)

    assert not (backup_env / "subscribers.json").exists()
    assert not (backup_env / "stats_current.json").exists()


def test_restore_no_valid_members_raises(backup_env):
    raw = _zip_bytes({"seen_ids.json": "{}", "junk.txt": "x"})
    with pytest.raises(ValueError):
        backup.restore_backup_zip(raw)


def test_restore_partial_corrupt_does_not_write_before_validation(backup_env):
    # битый stats_current не должен оставить полузаписанный файл
    raw = _zip_bytes({"stats_current.json": "{bad"})
    with pytest.raises(ValueError):
        backup.restore_backup_zip(raw)
    assert not (backup_env / "stats_current.json").exists()


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
async def test_backup_after_subscription_subscribe(backup_env, monkeypatch):
    (backup_env / "subscribers.json").write_text(
        '{"subscribers": {"7": "Neo"}}', encoding="utf-8")
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)
    bot = AsyncMock()

    await backup._backup_after_subscription(bot, 7, "Neo", subscribed=True)

    sent.assert_awaited_once()
    caption = sent.call_args.args[1]
    assert "➕" in caption
    assert 'tg://user?id=7' in caption
    assert backup.BACKUP_TAG in caption


@pytest.mark.asyncio
async def test_backup_after_subscription_unsubscribe(backup_env, monkeypatch):
    (backup_env / "subscribers.json").write_text('{"subscribers": {}}', encoding="utf-8")
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)

    await backup._backup_after_subscription(AsyncMock(), 5, "Trinity", subscribed=False)

    caption = sent.call_args.args[1]
    assert "➖" in caption
    assert 'tg://user?id=5' in caption


# ─────────────────────────────────────────────────────────────
#  Еженедельный авто-бэкап
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_weekly_backup_first_time_marks_without_sending(backup_env, monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)
    cur = {"period": "2026-Q2", "events": []}   # нет last_backup_at

    out = await backup._weekly_backup_if_due(AsyncMock(), cur)

    sent.assert_not_awaited()
    assert isinstance(out["last_backup_at"], float)


@pytest.mark.asyncio
async def test_weekly_backup_not_due_does_nothing(backup_env, monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)
    ts = time.time()
    cur = {"period": "2026-Q2", "events": [], "last_backup_at": ts}

    out = await backup._weekly_backup_if_due(AsyncMock(), cur)

    sent.assert_not_awaited()
    assert out["last_backup_at"] == ts


@pytest.mark.asyncio
async def test_weekly_backup_due_sends_and_updates(backup_env, monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)
    old = time.time() - backup.WEEKLY_BACKUP_INTERVAL - 100
    cur = {"period": "2026-Q2", "events": [], "last_backup_at": old}

    out = await backup._weekly_backup_if_due(AsyncMock(), cur)

    sent.assert_awaited_once()
    assert out["last_backup_at"] > old


@pytest.mark.asyncio
async def test_weekly_backup_due_send_fails_keeps_old_timestamp(backup_env, monkeypatch):
    monkeypatch.setattr("backup.send_backup", AsyncMock(return_value=False))
    old = time.time() - backup.WEEKLY_BACKUP_INTERVAL - 100
    cur = {"period": "2026-Q2", "events": [], "last_backup_at": old}

    out = await backup._weekly_backup_if_due(AsyncMock(), cur)

    assert out["last_backup_at"] == old   # не сдвигаем метку, если не ушло


# ─────────────────────────────────────────────────────────────
#  Структура stats_current
# ─────────────────────────────────────────────────────────────

def test_empty_stats_current_has_last_backup_at():
    fresh = storage._empty_stats_current("2026-Q2")
    assert "last_backup_at" in fresh
    assert fresh["last_backup_at"] is None


def test_load_stats_current_backfills_last_backup_at(backup_env):
    # файл старого формата без last_backup_at
    storage.STATS_CURRENT_FILE.write_text(json.dumps({
        "period": "2026-Q2",
        "period_start": "2026-04-01T00:00:00",
        "tracking_since": "2026-04-01T00:00:00",
        "last_report_sent": None,
        "events": [],
    }), encoding="utf-8")
    data = storage.load_stats_current()
    assert data["last_backup_at"] is None

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
async def test_shutdown_backup_sends_when_no_recent(backup_env, monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)
    monkeypatch.setattr("backup._last_backup_sent_at", None)
    await backup._shutdown_backup(AsyncMock())
    sent.assert_awaited_once()
    caption = sent.call_args.args[1]
    assert backup.BACKUP_TAG in caption
    assert "SIGTERM" in caption


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
    monkeypatch.setattr("backup.SHUTDOWN_BACKUP_TIMEOUT", 0.01)

    async def _slow(_bot, _caption):
        await main.asyncio.sleep(0.2)
        return True

    monkeypatch.setattr("backup.send_backup", _slow)
    await backup._shutdown_backup(AsyncMock())   # не должно бросить


@pytest.mark.asyncio
async def test_shutdown_backup_timeout_cancels_retry_sequence(backup_env, monkeypatch):
    monkeypatch.setattr("backup._last_backup_sent_at", None)
    monkeypatch.setattr("backup.SHUTDOWN_BACKUP_TIMEOUT", 0.01)
    bot = AsyncMock()
    bot.send_document.side_effect = aiohttp.ClientOSError(
        104,
        "Connection reset by peer",
    )

    await backup._shutdown_backup(bot)

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
def test_restore_rejects_malformed_subscribers(backup_env, payload):
    raw = _zip_bytes({"subscribers.json": payload})
    with pytest.raises(ValueError):          # единственный файл невалиден → нечего восстанавливать
        backup.restore_backup_zip(raw)
    assert not (backup_env / "subscribers.json").exists()


def test_restore_skips_bad_shape_keeps_good(backup_env):
    raw = _zip_bytes({
        "subscribers.json": '{"subscribers": {"5": "Ok"}}',
        "stats_current.json": '{"period": "2026-Q2"}',   # нет events-списка → пропуск
    })
    result = backup.restore_backup_zip(raw)
    assert "subscribers.json" in result["restored"]
    assert "stats_current.json" in result["skipped"]
    assert not (backup_env / "stats_current.json").exists()


def test_restore_rejects_quarter_without_period(backup_env):
    raw = _zip_bytes({"quarters/x.json": '{"events": []}'})
    with pytest.raises(ValueError):
        backup.restore_backup_zip(raw)


def test_valid_import_payload_accepts_canonical_shapes():
    assert backup._valid_import_payload("subscribers.json", {"subscribers": {"1": "A"}})
    assert backup._valid_import_payload("stats_current.json", {"period": "2026-Q2", "events": []})
    assert backup._valid_import_payload("quarters/2026-Q1.json", {"period": "2026-Q1"})


# ─────────────────────────────────────────────────────────────
#  Замечание ревью: вложенный каталог quarters/ создаётся на свежем томе.
# ─────────────────────────────────────────────────────────────


def test_restore_creates_missing_quarters_dir(backup_env):
    # эмулируем свежий том: каталога quarters/ ещё нет (кейс из «HIGH RISK» Codacy)
    import shutil
    shutil.rmtree(backup_env / "quarters")
    assert not (backup_env / "quarters").exists()
    raw = _zip_bytes({"quarters/2026-Q1.json": '{"period": "2026-Q1"}'})
    result = backup.restore_backup_zip(raw)
    assert "quarters/2026-Q1.json" in result["restored"]
    # _atomic_write сам создаёт parent — краша на свежем томе нет
    assert (backup_env / "quarters" / "2026-Q1.json").exists()
