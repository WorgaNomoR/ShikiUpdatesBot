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
import time
import zipfile
from unittest.mock import AsyncMock, MagicMock

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


# ─────────────────────────────────────────────────────────────
#  Сборка архива
# ─────────────────────────────────────────────────────────────

def test_build_backup_zip_excludes_tmp_and_keeps_structure(backup_env):
    (backup_env / "subscribers.json").write_text('{"subscribers": {}}', encoding="utf-8")
    (backup_env / "stats_current.json").write_text('{"period": "2026-Q2"}', encoding="utf-8")
    (backup_env / "subscribers.json.tmp").write_text("garbage", encoding="utf-8")
    (backup_env / "quarters" / "2026-Q1.json").write_text('{"period": "2026-Q1"}', encoding="utf-8")

    raw = backup._build_backup_zip()
    names = set(zipfile.ZipFile(io.BytesIO(raw)).namelist())

    assert "subscribers.json" in names
    assert "stats_current.json" in names
    assert "quarters/2026-Q1.json" in names          # вложенность сохранена
    assert "subscribers.json.tmp" not in names       # *.tmp исключён


# ─────────────────────────────────────────────────────────────
#  Белый список импорта / zip-slip
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "subscribers.json",
    "stats_current.json",
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
        "quarters/2026-Q1.json": '{"period": "2026-Q1"}',
        "seen_ids.json": '{"seen_ids": [1, 2, 3]}',   # должен быть отброшен
    })
    result = backup.restore_backup_zip(raw)

    assert set(result["restored"]) == {
        "subscribers.json", "stats_current.json", "quarters/2026-Q1.json",
    }
    assert "seen_ids.json" in result["skipped"]
    # файлы реально записаны
    assert storage.load_subscribers() == {123: "Alice"}
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


# ─────────────────────────────────────────────────────────────
#  Ссылка на профиль
# ─────────────────────────────────────────────────────────────

def test_subscriber_link_wraps_name_in_tg_profile_link():
    link = backup._subscriber_link(42, "Алиса")
    assert 'href="tg://user?id=42"' in link
    assert ">Алиса<" in link


def test_subscriber_link_escapes_html_in_name():
    link = backup._subscriber_link(1, "<b>x</b>")
    assert "&lt;b&gt;" in link   # имя экранировано


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


@pytest.mark.asyncio
async def test_cmd_start_triggers_auto_backup(backup_env, monkeypatch):
    (backup_env / "subscribers.json").write_text('{"subscribers": {}}', encoding="utf-8")
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("backup.send_backup", sent)

    msg = MagicMock()
    msg.chat.id = 555
    msg.from_user.full_name = "Morpheus"
    msg.from_user.id = 555
    msg.answer = AsyncMock()
    msg.bot = AsyncMock()

    await handlers.cmd_start(msg)

    assert storage.load_subscribers() == {555: "Morpheus"}   # подписка сохранена
    sent.assert_awaited_once()                            # бэкап ушёл


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
#  Замечания ревью: ротация-бэкап покрыт + вложенный каталог создаётся
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quarter_rotation_triggers_backup(backup_env, monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("handlers.send_backup", sent)
    monkeypatch.setattr("handlers.sync_stats_all", AsyncMock(return_value=({}, True)))
    monkeypatch.setattr("handlers.build_quarterly_report_messages", lambda *a, **k: [])
    monkeypatch.setattr("handlers._save_quarter_snapshot", lambda *a, **k: None)
    monkeypatch.setattr("handlers._update_by_quarter", lambda *a, **k: None)
    monkeypatch.setattr("handlers._load_prev_quarter_summary", lambda *a, **k: None)
    monkeypatch.setattr("handlers.save_stats_all", lambda *a, **k: None)

    old_cur = {"period": "2025-Q1", "events": []}   # заведомо прошлый квартал → ротация
    await handlers.rotate_quarter_if_needed(AsyncMock(), old_cur, {})

    sent.assert_awaited_once()
    assert backup.BACKUP_TAG in sent.call_args.args[1]


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


@pytest.mark.asyncio
async def test_rotation_skips_resync_at_boot(backup_env, monkeypatch):
    """resync=False (стартовый вызов): НЕ дёргаем sync_stats_all — polling_loop
    уже дал свежий stats_all; второй синк своей сессией ловил 429 в день ротации."""
    sync = AsyncMock(return_value=({}, True))
    monkeypatch.setattr("handlers.sync_stats_all", sync)
    monkeypatch.setattr("handlers.send_backup", AsyncMock(return_value=True))
    monkeypatch.setattr("handlers.build_quarterly_report_messages", lambda *a, **k: [])
    monkeypatch.setattr("handlers._save_quarter_snapshot", lambda *a, **k: None)
    monkeypatch.setattr("handlers._update_by_quarter", lambda *a, **k: None)
    monkeypatch.setattr("handlers._load_prev_quarter_summary", lambda *a, **k: None)
    monkeypatch.setattr("handlers.save_stats_all", lambda *a, **k: None)

    old_cur = {"period": "2025-Q1", "events": []}
    await handlers.rotate_quarter_if_needed(AsyncMock(), old_cur, {}, resync=False)
    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotation_resyncs_in_loop(backup_env, monkeypatch):
    """resync=True (дефолт, цикловой вызов): дёргаем sync_stats_all для свежих метаданных."""
    sync = AsyncMock(return_value=({}, True))
    monkeypatch.setattr("handlers.sync_stats_all", sync)
    monkeypatch.setattr("handlers.send_backup", AsyncMock(return_value=True))
    monkeypatch.setattr("handlers.build_quarterly_report_messages", lambda *a, **k: [])
    monkeypatch.setattr("handlers._save_quarter_snapshot", lambda *a, **k: None)
    monkeypatch.setattr("handlers._update_by_quarter", lambda *a, **k: None)
    monkeypatch.setattr("handlers._load_prev_quarter_summary", lambda *a, **k: None)
    monkeypatch.setattr("handlers.save_stats_all", lambda *a, **k: None)

    old_cur = {"period": "2025-Q1", "events": []}
    await handlers.rotate_quarter_if_needed(AsyncMock(), old_cur, {})
    sync.assert_awaited_once()
