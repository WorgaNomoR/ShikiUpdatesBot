# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import asyncio
from unittest.mock import AsyncMock

import pytest

import backup
import config
import handlers
import storage


# ─────────────────────────────────────────────────────────────
#  Хелпер: мокаем всё, что polling_loop вызывает по части статистики.
#  sync_stats_all и rotate_quarter_if_needed делают сетевые/файловые
#  вызовы — без моков тесты уходят в реальную сеть и виснут.
#  load_stats_current читает файл — отдаём пустой стейт квартала.
# ─────────────────────────────────────────────────────────────
def _patch_stats(monkeypatch, main):
    monkeypatch.setattr("handlers.load_stats_current", lambda: {"period": "2026-Q2", "events": []})

    async def fake_sync(session=None, fav=None):
        # sync_stats_all теперь возвращает кортеж (stats, ok).
        return storage._empty_stats_all(), True

    monkeypatch.setattr("handlers.sync_stats_all", fake_sync)

    async def fake_rotate(bot, cur, stats_all):
        return cur

    monkeypatch.setattr("handlers.rotate_quarter_if_needed", fake_rotate)


@pytest.mark.asyncio
async def test_first_run_initializes_history_and_favourites(monkeypatch):
    import main

    monkeypatch.setattr("handlers.load_seen_ids", lambda: set())
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: set())
    monkeypatch.setattr("handlers.load_subscribers", lambda: {})
    _patch_stats(monkeypatch, main)

    saved_ids = {}
    saved_favs = {}

    monkeypatch.setattr(
        "handlers.save_seen_ids",
        lambda ids: saved_ids.setdefault("value", ids),
    )

    monkeypatch.setattr(
        "handlers.save_seen_favourites",
        lambda favs: saved_favs.setdefault("value", favs),
    )

    async def fake_history(session):
        return [{"id": 1}, {"id": 2}]

    async def fake_favourites(session):
        return {
            "animes": [{"id": 10}],
            "mangas": [],
            "characters": [],
            "people": [],
        }

    monkeypatch.setattr("handlers.fetch_history", fake_history)
    monkeypatch.setattr("handlers.fetch_favourites", fake_favourites)

    async def fake_check(bot, seen, cur):
        raise asyncio.CancelledError

    monkeypatch.setattr("handlers.check_and_notify", fake_check)

    class DummyBot:
        pass

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(DummyBot())

    assert saved_ids["value"] == {1, 2}
    assert "animes_10" in saved_favs["value"]


@pytest.mark.asyncio
async def test_missing_seen_favourites_does_not_send_notifications(monkeypatch):
    import main

    # История уже инициализирована
    monkeypatch.setattr(
        "handlers.load_seen_ids",
        lambda: {1, 2, 3},
    )

    # Файл избранного отсутствует
    monkeypatch.setattr(
        "handlers.load_seen_favourites",
        lambda: set(),
    )

    monkeypatch.setattr(
        "handlers.load_subscribers",
        lambda: {},
    )
    _patch_stats(monkeypatch, main)

    saved = {}

    def fake_save(seen):
        saved["value"] = seen

    monkeypatch.setattr(
        "handlers.save_seen_favourites",
        fake_save,
    )

    async def fake_fetch(session):
        return {
            "animes": [
                {
                    "id": 10,
                    "name": "Ergo Proxy",
                }
            ]
        }

    monkeypatch.setattr(
        "handlers.fetch_favourites",
        fake_fetch,
    )

    called = False

    async def fake_send(bot, text):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "handlers.send_to_all_chats",
        fake_send,
    )

    async def fake_check(bot, seen, cur):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "handlers.check_and_notify",
        fake_check,
    )

    class DummyBot:
        pass

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(DummyBot())

    assert called is False
    assert "animes_10" in saved["value"]


@pytest.mark.asyncio
async def test_favourites_initialization_failure(monkeypatch):
    import main

    monkeypatch.setattr("handlers.load_seen_ids", lambda: {1})
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: set())
    monkeypatch.setattr("handlers.load_subscribers", lambda: {})
    _patch_stats(monkeypatch, main)

    save_called = False

    def fake_save(_):
        nonlocal save_called
        save_called = True

    monkeypatch.setattr(
        "handlers.save_seen_favourites",
        fake_save,
    )

    async def fake_fetch(session):
        return None

    monkeypatch.setattr(
        "handlers.fetch_favourites",
        fake_fetch,
    )

    async def fake_check(bot, seen, cur):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "handlers.check_and_notify",
        fake_check,
    )

    class DummyBot:
        pass

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(DummyBot())

    assert save_called is False


@pytest.mark.asyncio
async def test_polling_survives_unexpected_exception(monkeypatch):
    import main

    monkeypatch.setattr("handlers.load_seen_ids", lambda: {1})
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: {"animes_1"})
    monkeypatch.setattr("handlers.load_subscribers", lambda: {})
    monkeypatch.setattr("handlers.ERROR_NOTIFY_INTERVAL", 0)
    _patch_stats(monkeypatch, main)

    logged = []

    monkeypatch.setattr(
        config.log,
        "exception",
        lambda *args, **kwargs: logged.append(args),
    )

    sent = []

    class DummyBot:
        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    calls = 0

    async def fake_check(bot, seen, cur):
        nonlocal calls
        calls += 1

        if calls == 1:
            raise RuntimeError("boom")

        raise asyncio.CancelledError

    monkeypatch.setattr("handlers.check_and_notify", fake_check)

    async def fake_check_favs(bot, seen):
        return seen

    monkeypatch.setattr(
        "handlers.check_and_notify_favourites",
        fake_check_favs,
    )

    async def fake_sleep(_):
        pass

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(DummyBot())

    assert calls == 2
    assert logged
    assert sent

    chat_id, text = sent[0]

    assert chat_id == config.OWNER_ID
    assert "RuntimeError" in text
    assert "boom" in text


@pytest.mark.asyncio
async def test_polling_propagates_cancelled_error(monkeypatch):
    import main

    monkeypatch.setattr("handlers.load_seen_ids", lambda: {1})
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: {"animes_1"})
    monkeypatch.setattr("handlers.load_subscribers", lambda: {})
    _patch_stats(monkeypatch, main)

    async def fake_check(bot, seen, cur):
        raise asyncio.CancelledError

    monkeypatch.setattr("handlers.check_and_notify", fake_check)

    async def fake_check_favs(bot, seen):
        return seen

    monkeypatch.setattr("handlers.check_and_notify_favourites", fake_check_favs)

    class DummyBot:
        async def send_message(self, *args, **kwargs):
            pass

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(DummyBot())


# ── периодический ресинк stats_all ──

def test_should_full_sync_predicate():
    """None ⇒ ретрай каждый цикл; недавно ⇒ ждём; протухло ⇒ пора."""
    iv = 6 * 3600
    assert handlers._should_full_sync(None, 1000.0, iv) is True
    assert handlers._should_full_sync(1000.0, 1000.0 + 10, iv) is False
    assert handlers._should_full_sync(1000.0, 1000.0 + iv, iv) is True
    assert handlers._should_full_sync(1000.0, 1000.0 + iv + 1, iv) is True


@pytest.mark.asyncio
async def test_boot_fetches_favourites_once_and_threads_session(monkeypatch):
    """boot-throttle: на старте избранное тянется ОДИН раз и отдаётся в sync (fav=),
    а sync получает ту же общую сессию (не None)."""
    import main  # noqa: F401

    monkeypatch.setattr("handlers.load_seen_ids", lambda: {1})
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: set())
    monkeypatch.setattr("handlers.load_subscribers", lambda: {})
    monkeypatch.setattr("handlers.load_stats_current", lambda: {"period": "2026-Q2", "events": []})
    monkeypatch.setattr("handlers.save_seen_favourites", lambda favs: None)

    fav_calls = []

    async def fake_favourites(session):
        fav_calls.append(session)
        return {"animes": [{"id": 10}], "mangas": [], "characters": [], "people": []}

    monkeypatch.setattr("handlers.fetch_favourites", fake_favourites)

    captured = {}

    async def fake_sync(session=None, fav=None):
        captured["session"] = session
        captured["fav"] = fav
        return storage._empty_stats_all(), True

    monkeypatch.setattr("handlers.sync_stats_all", fake_sync)

    async def fake_rotate(bot, cur, stats_all):
        return cur

    monkeypatch.setattr("handlers.rotate_quarter_if_needed", fake_rotate)

    async def fake_check(bot, seen, cur):
        raise asyncio.CancelledError

    monkeypatch.setattr("handlers.check_and_notify", fake_check)

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(object())

    # избранное запрошено РОВНО один раз (а не дважды: init + внутри sync)
    assert len(fav_calls) == 1
    # та же сессия проброшена в sync (общая, не None), избранное передано через fav=
    assert captured["session"] is not None
    assert captured["session"] is fav_calls[0]
    assert captured["fav"] is not None and "animes" in captured["fav"]


@pytest.mark.asyncio
async def test_cycle_fetches_favourites_once_and_threads_to_sync(monkeypatch):
    """Дедуп в цикловом пути: за один проход избранное тянется ОДИН раз и
    делится между уведомлениями (favourites=) и ресинком stats_all (fav=),
    вместо двух фетчей (check + внутри sync)."""
    import main  # noqa: F401

    monkeypatch.setattr("handlers.load_seen_ids", lambda: {1})
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: {"animes_10"})
    monkeypatch.setattr("handlers.load_subscribers", lambda: {})
    monkeypatch.setattr("handlers.load_stats_current", lambda: {"period": "2026-Q2", "events": []})
    monkeypatch.setattr("handlers.load_stats_all", lambda: storage._empty_stats_all())
    monkeypatch.setattr("handlers.save_seen_favourites", lambda favs: None)
    monkeypatch.setattr("handlers.heartbeat", lambda: None)
    # Форсим ресинк stats_all в цикле, чтобы проверить проброс fav=.
    monkeypatch.setattr("handlers._should_full_sync", lambda *a, **k: True)

    fav_payload = {"animes": [{"id": 10}], "mangas": [], "characters": [], "people": []}
    fav_calls = []

    async def fake_favourites(session):
        fav_calls.append(session)
        return fav_payload

    monkeypatch.setattr("handlers.fetch_favourites", fake_favourites)

    cnf_favs = []

    async def fake_cnf(bot, seen, favourites=None):
        cnf_favs.append(favourites)
        return seen, False

    monkeypatch.setattr("handlers.check_and_notify_favourites", fake_cnf)

    sync_favs = []

    async def fake_sync(session=None, fav=None):
        sync_favs.append(fav)
        return storage._empty_stats_all(), True

    monkeypatch.setattr("handlers.sync_stats_all", fake_sync)

    async def fake_rotate(bot, cur, stats_all, resync=False):
        return cur

    monkeypatch.setattr("handlers.rotate_quarter_if_needed", fake_rotate)

    async def fake_weekly(bot, cur):
        return cur

    monkeypatch.setattr("handlers._weekly_backup_if_due", fake_weekly)

    calls = 0

    async def fake_check(bot, seen, cur):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise asyncio.CancelledError
        return seen, cur

    monkeypatch.setattr("handlers.check_and_notify", fake_check)

    async def fake_sleep(_):
        pass

    monkeypatch.setattr(handlers.asyncio, "sleep", fake_sleep)

    class DummyBot:
        pass

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(DummyBot())

    # boot(1) + один проход цикла(1) = 2; второй проход падает в check до фетча.
    assert len(fav_calls) == 2
    # Уведомлениям цикл отдал уже скачанное избранное (тот же объект).
    assert cnf_favs == [fav_payload]
    # Ресинку в цикле проброшен fav= (иначе sync_stats_all фетчил бы 2-й раз).
    assert sync_favs[-1] is fav_payload


# ═══════════════════════════════════════════════════════════════════
#  Ротация квартала (rotate_quarter_if_needed) — polling-флоу.
#  Перенесено из test_backup.py (#35): цель — handlers.rotate_quarter_if_needed,
#  а не backup.py. Матрица вход→выход ротации живёт здесь; test_backup.py
#  мокал rotate на уровне цикла.
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_quarter_rotation_triggers_backup(backup_env, monkeypatch):
    """Расхоловленный (#35): чистые хелперы _update_by_quarter и
    build_quarterly_report_messages гоняем ВЖИВУЮ на реальном quarter-state;
    мокаем только I/O-границы (send_backup, sync_stats_all сеть,
    _save_quarter_snapshot / _load_prev_quarter_summary / save_stats_all файлы).
    Так тест ловит реальную агрегацию by_quarter и содержимое отчёта, а не
    только факт «rotate дёрнул send_backup»."""
    # Реальный стейт прошлого квартала: 2 завершённых аниме, 1 манга, 1 дроп.
    old_cur = {
        "period": "2025-Q1",
        "events": [
            {"id": "1", "media": "anime", "event": "completed", "score": 10},
            {"id": "2", "media": "anime", "event": "completed", "score": 8},
            {"id": "3", "media": "manga", "event": "completed", "score": 9},
            {"id": "4", "media": "anime", "event": "dropped"},
        ],
    }
    stats_all = storage._empty_stats_all()
    stats_all["anime"]["titles"] = {
        "1": {"title": "Аниме-Один", "url": "/animes/1", "score": 10,
              "year": 2020, "episodes_watched": 12},
        "2": {"title": "Аниме-Два", "url": "/animes/2", "score": 8,
              "year": 2021, "episodes_watched": 24},
        "4": {"title": "Аниме-Дроп", "url": "/animes/4", "score": 0},
    }
    stats_all["manga"]["titles"] = {
        "3": {"title": "Манга-Три", "url": "/mangas/3", "score": 9,
              "year": 2019, "chapters_read": 100},
    }

    # I/O-границы — мок. sync_stats_all отдаёт наш реальный stats_all
    # (сеть замокана, но данные настоящие → чистые хелперы работают на них).
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("handlers.send_backup", sent)
    monkeypatch.setattr("handlers.sync_stats_all", AsyncMock(return_value=(stats_all, True)))
    monkeypatch.setattr("handlers._save_quarter_snapshot", lambda *a, **k: None)
    monkeypatch.setattr("handlers._load_prev_quarter_summary",
                        lambda *a, **k: {"period": "2024-Q4",
                                         "anime_completed": 1, "manga_completed": 0})
    saved = {}
    monkeypatch.setattr("handlers.save_stats_all", lambda sa: saved.update(sa=sa))
    # Время — не I/O ротации: гасим паузу между сообщениями отчёта.

    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(handlers.asyncio, "sleep", _no_sleep)

    bot = AsyncMock()
    await handlers.rotate_quarter_if_needed(bot, old_cur, {})   # resync=True (дефолт)

    # 1. Бэкап-снапшот ротации ушёл владельцу с тегом.
    sent.assert_awaited_once()
    assert backup.BACKUP_TAG in sent.call_args.args[1]

    # 2. _update_by_quarter реально агрегировал квартал в stats_all.
    a_bq = saved["sa"]["anime"]["aggregates"]["by_quarter"]["2025-Q1"]
    assert a_bq == {"completed": 2, "avg_score": 9.0, "episodes_watched": 36}
    m_bq = saved["sa"]["manga"]["aggregates"]["by_quarter"]["2025-Q1"]
    assert m_bq == {"completed": 1, "avg_score": 9.0, "chapters_read": 100}

    # 3. build_quarterly_report_messages реально собрал отчёт (3 темы),
    #    с заголовком, реальными тайтлами и блоком сравнения (prev-summary дан).
    report = [c.args[1] for c in bot.send_message.await_args_list]
    assert len(report) == 3
    assert "КВАРТАЛЬНЫЙ ОТЧЁТ" in report[0]
    assert "Аниме-Один" in report[0]
    assert "Сравнение" in report[2]


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
