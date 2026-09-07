# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import asyncio
import io
import json
import logging
import zipfile
from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import (
    AsyncMock,
    MagicMock,
)
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramServerError
from aiogram.methods import SendMessage

import backup
import config
import handlers
import shiki_api
import storage
import telegram_delivery
from report_model import (
    Report,
    plain_report,
)


class _RendererReached(RuntimeError):
    pass


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
async def test_polling_services_due_subscription_before_weekly(monkeypatch):
    monkeypatch.setattr("handlers.load_seen_ids", lambda: {1})
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: {"animes_10"})
    monkeypatch.setattr("handlers.load_subscribers", lambda: {})
    monkeypatch.setattr(
        "handlers.load_stats_current",
        lambda: {"period": "2026-Q2", "events": []},
    )
    monkeypatch.setattr("handlers.load_stats_all", storage._empty_stats_all)
    monkeypatch.setattr(
        "handlers.fetch_favourites",
        AsyncMock(return_value={"animes": [], "mangas": []}),
    )
    monkeypatch.setattr(
        "handlers.sync_stats_all",
        AsyncMock(return_value=(storage._empty_stats_all(), True)),
    )
    order = []

    async def rotate(_bot, cur, _stats_all, resync=False):
        order.append("quarter")
        return cur

    async def subscription(_bot):
        order.append("subscription")
        return True

    async def weekly(_bot, cur):
        order.append("weekly")
        if order.count("weekly") == 2:
            raise asyncio.CancelledError
        return cur

    async def check(_bot, seen, cur):
        return seen, cur

    async def check_favourites(_bot, seen, favourites=None):
        return seen, False

    monkeypatch.setattr("handlers.rotate_quarter_if_needed", rotate)
    monkeypatch.setattr("handlers._backup_after_subscription", subscription)
    monkeypatch.setattr("handlers._weekly_backup_if_due", weekly)
    monkeypatch.setattr("handlers.check_and_notify", check)
    monkeypatch.setattr("handlers.check_and_notify_favourites", check_favourites)

    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(handlers.asyncio, "sleep", no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(object())

    assert order == [
        "quarter",
        "subscription",
        "weekly",
        "quarter",
        "subscription",
        "weekly",
    ]


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
    sync_marks = []
    monkeypatch.setattr(
        "handlers.mark_full_sync_success",
        lambda: sync_marks.append(True),
    )

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
    assert len(sync_marks) == 2


@pytest.mark.asyncio
async def test_failed_full_sync_does_not_advance_display_timestamp(monkeypatch):
    monkeypatch.setattr("handlers.load_seen_ids", lambda: {1})
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: {"animes_10"})
    monkeypatch.setattr("handlers.load_subscribers", lambda: {})
    monkeypatch.setattr(
        "handlers.load_stats_current",
        lambda: {"period": "2026-Q2", "events": []},
    )
    monkeypatch.setattr("handlers.load_stats_all", storage._empty_stats_all)
    monkeypatch.setattr(
        "handlers.fetch_favourites",
        AsyncMock(return_value={"animes": [], "mangas": []}),
    )
    monkeypatch.setattr(
        "handlers.sync_stats_all",
        AsyncMock(return_value=(storage._empty_stats_all(), False)),
    )
    monkeypatch.setattr(
        "handlers.rotate_quarter_if_needed",
        AsyncMock(side_effect=lambda bot, cur, stats_all, resync=False: cur),
    )
    monkeypatch.setattr(
        "handlers.check_and_notify",
        AsyncMock(side_effect=asyncio.CancelledError),
    )
    mark = MagicMock()
    monkeypatch.setattr("handlers.mark_full_sync_success", mark)

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(object())

    mark.assert_not_called()


@pytest.mark.asyncio
async def test_startup_private_list_notifies_owner_without_saving_public_favourites(
    monkeypatch,
):
    """Доступное favourites не маскирует закрытый list export на старте."""
    monkeypatch.setattr("handlers.load_seen_ids", lambda: {1})
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: set())
    monkeypatch.setattr("handlers.load_subscribers", lambda: {777: "subscriber"})
    monkeypatch.setattr(
        "handlers.load_stats_current",
        lambda: {"period": "2026-Q2", "events": []},
    )
    preserved_stats = storage._empty_stats_all()
    monkeypatch.setattr("handlers.load_stats_all", lambda: preserved_stats)

    saved_favourites = []
    monkeypatch.setattr(
        "handlers.save_seen_favourites",
        lambda seen: saved_favourites.append(set(seen)),
    )
    monkeypatch.setattr(
        "handlers.save_stats_all",
        lambda data: pytest.fail("privacy failure сохранил stats_all"),
    )

    favourites = {
        "animes": [{"id": 10}],
        "mangas": [],
        "characters": [],
        "people": [],
    }

    async def fake_favourites(session):
        return favourites

    async def fake_sync(session=None, fav=None):
        assert fav is favourites
        raise shiki_api.ProfilePrivacyError("fetch_list_export(anime)")

    async def fake_check(bot, seen, cur):
        raise asyncio.CancelledError

    monkeypatch.setattr("handlers.fetch_favourites", fake_favourites)
    monkeypatch.setattr("handlers.sync_stats_all", fake_sync)
    monkeypatch.setattr("handlers.check_and_notify", fake_check)
    monkeypatch.setattr(
        "handlers.rotate_quarter_if_needed",
        AsyncMock(side_effect=AssertionError("privacy failure запустил ротацию")),
    )
    monkeypatch.setattr(
        "handlers.send_to_all_chats",
        AsyncMock(side_effect=AssertionError("privacy diagnostic ушёл подписчикам")),
    )

    sent = []

    class DummyBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(DummyBot())

    assert saved_favourites == []
    assert len(sent) == 1
    assert sent[0][0] == config.OWNER_ID
    assert "Могут видеть мой список" in sent[0][1]
    assert f"/{config.SHIKI_USER}/edit/profile" in sent[0][1]
    assert sent[0][2] == {"parse_mode": handlers.ParseMode.HTML}


@pytest.mark.asyncio
async def test_polling_private_profile_is_debounced_and_recovers(monkeypatch):
    monkeypatch.setattr("handlers.load_seen_ids", lambda: {1})
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: {"animes_10"})
    monkeypatch.setattr("handlers.load_subscribers", lambda: {777: "subscriber"})
    monkeypatch.setattr(
        "handlers.load_stats_current",
        lambda: {"period": "2026-Q2", "events": []},
    )
    monkeypatch.setattr("handlers.load_stats_all", storage._empty_stats_all)
    monkeypatch.setattr("handlers.ERROR_NOTIFY_INTERVAL", 3600)
    monkeypatch.setattr("handlers._should_full_sync", lambda *args: False)

    favourites = {
        "animes": [{"id": 10}],
        "mangas": [],
        "characters": [],
        "people": [],
    }

    async def fake_favourites(session):
        return favourites

    async def fake_sync(session=None, fav=None):
        return storage._empty_stats_all(), True

    async def fake_rotate(bot, cur, stats_all, resync=True):
        return cur

    async def fake_weekly(bot, cur):
        return cur

    check_calls = 0

    async def fake_check(bot, seen, cur):
        nonlocal check_calls
        check_calls += 1
        if check_calls <= 2:
            raise shiki_api.ProfilePrivacyError("fetch_history(page=1)")
        if check_calls == 3:
            return seen, cur
        raise asyncio.CancelledError

    recovered_favourites_checks = []

    async def fake_check_favourites(bot, seen, favourites=None):
        recovered_favourites_checks.append(favourites)
        return seen, False

    async def fake_sleep(delay):
        return None

    heartbeat_calls = []
    monkeypatch.setattr("handlers.fetch_favourites", fake_favourites)
    monkeypatch.setattr("handlers.sync_stats_all", fake_sync)
    monkeypatch.setattr("handlers.rotate_quarter_if_needed", fake_rotate)
    monkeypatch.setattr("handlers._weekly_backup_if_due", fake_weekly)
    monkeypatch.setattr("handlers.check_and_notify", fake_check)
    monkeypatch.setattr(
        "handlers.check_and_notify_favourites",
        fake_check_favourites,
    )
    monkeypatch.setattr("handlers.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "handlers.heartbeat",
        lambda: heartbeat_calls.append(True),
    )
    broadcast = AsyncMock()
    monkeypatch.setattr("handlers.send_to_all_chats", broadcast)

    sent = []

    class DummyBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(DummyBot())

    assert check_calls == 4
    assert len(sent) == 1
    assert sent[0][0] == config.OWNER_ID
    assert sent[0][2] == {"parse_mode": handlers.ParseMode.HTML}
    assert recovered_favourites_checks == [favourites]
    assert len(heartbeat_calls) == 3
    broadcast.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════
#  Ротация квартала (rotate_quarter_if_needed) — polling-флоу.
#  Перенесено из test_backup.py (#35): цель — handlers.rotate_quarter_if_needed,
#  а не backup.py. Матрица вход→выход ротации живёт здесь; test_backup.py
#  мокал rotate на уровне цикла.
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_quarter_renderer_runs_without_restorable_state_lock(monkeypatch):
    lock_depth = 0

    @asynccontextmanager
    async def transaction():
        nonlocal lock_depth
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1

    def render(report):
        assert lock_depth == 0
        raise _RendererReached

    old_cur = {"period": "2026-Q2", "events": []}
    monkeypatch.setattr(handlers, "restorable_state_transaction", transaction)
    monkeypatch.setattr(handlers, "load_stats_current", lambda **kwargs: old_cur)
    monkeypatch.setattr(handlers, "current_quarter", lambda: "2026-Q3")
    monkeypatch.setattr(handlers, "_load_prev_quarter_summary", lambda *args: None)
    monkeypatch.setattr(handlers, "rendered_html", render)

    with pytest.raises(_RendererReached):
        await handlers.rotate_quarter_if_needed(
            AsyncMock(),
            old_cur,
            {},
            resync=False,
        )


@pytest.mark.asyncio
async def test_quarter_rotation_defers_after_repeated_state_changes(monkeypatch):
    state = {"cur": {"period": "2026-Q2", "events": [], "generation": 0}}
    render_calls = 0

    def render(report):
        nonlocal render_calls
        render_calls += 1
        if render_calls > 3:
            pytest.fail("Ротация не остановилась после трёх попыток")
        state["cur"] = {
            "period": "2026-Q2",
            "events": [],
            "generation": render_calls,
        }
        return ["REPORT"]

    snapshot = MagicMock()
    save_current = MagicMock()
    deliver_pending = AsyncMock()
    monkeypatch.setattr(handlers, "load_stats_current", lambda **kwargs: state["cur"])
    monkeypatch.setattr(handlers, "current_quarter", lambda: "2026-Q3")
    monkeypatch.setattr(handlers, "_load_prev_quarter_summary", lambda *args: None)
    monkeypatch.setattr(
        handlers,
        "build_quarterly_report_messages",
        lambda *args: plain_report("REPORT"),
    )
    monkeypatch.setattr(handlers, "rendered_html", render)
    monkeypatch.setattr(handlers, "_save_quarter_snapshot", snapshot)
    monkeypatch.setattr(handlers, "save_stats_current", save_current)
    monkeypatch.setattr(handlers, "_deliver_pending_quarter", deliver_pending)

    result = await handlers.rotate_quarter_if_needed(
        AsyncMock(),
        state["cur"],
        {},
        resync=False,
    )

    assert result is state["cur"]
    assert result["generation"] == 3
    assert render_calls == 3
    snapshot.assert_not_called()
    save_current.assert_not_called()
    deliver_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_quarter_rotation_triggers_backup(backup_env, monkeypatch):
    """Расхоловленный (#35): чистые хелперы _update_by_quarter и
    build_quarterly_report_messages гоняем ВЖИВУЮ на реальном quarter-state;
    мокаем только I/O-границы (send_backup, sync_stats_all сеть,
    _save_quarter_snapshot / save_stats_all файлы). Сводку предыдущего квартала
    читаем из настоящего временного снапшота, чтобы проверить выбранный период.
    Так тест ловит реальную агрегацию by_quarter и содержимое отчёта, а не
    только факт «rotate дёрнул send_backup»."""
    # Реальный стейт прошлого квартала: 2 завершённых аниме, 1 манга, 1 дроп.
    old_cur = {
        "period": "2026-Q2",
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
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    (backup_env / "quarters" / "2026-Q1.json").write_text(
        '{"period":"2026-Q1","anime_completed":1,"manga_completed":0}',
        encoding="utf-8",
    )
    saved = {}
    monkeypatch.setattr("handlers.save_stats_all", lambda sa: saved.update(sa=sa))
    # Время — не I/O ротации: гасим паузу между сообщениями отчёта.

    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(handlers.asyncio, "sleep", _no_sleep)

    bot = AsyncMock()
    subscription_pending = {
        "subscriptions": 2,
        "unsubscriptions": 1,
        "counts_known": True,
        "token": uuid4().hex,
    }
    storage.save_subscriber_state(
        storage.SubscriberState(
            {7: "Neo"},
            {
                "version": 1,
                "last_backup_at": None,
                "weekly_started_at": 100.0,
                "pending": subscription_pending,
            },
        )
    )
    storage.save_stats_current(old_cur)
    await handlers.rotate_quarter_if_needed(bot, old_cur, {})   # resync=True (дефолт)

    # 1. Бэкап-снапшот ротации ушёл владельцу с тегом.
    sent.assert_awaited_once()
    assert backup.BACKUP_TAG in sent.call_args.args[1]
    backup_schedule = storage.load_subscription_backup_state()
    assert isinstance(backup_schedule["last_backup_at"], float)
    assert backup_schedule["pending"] == subscription_pending

    # 2. _update_by_quarter реально агрегировал квартал в stats_all.
    a_bq = saved["sa"]["anime"]["aggregates"]["by_quarter"]["2026-Q2"]
    assert a_bq == {"completed": 2, "avg_score": 9.0, "episodes_watched": 36}
    m_bq = saved["sa"]["manga"]["aggregates"]["by_quarter"]["2026-Q2"]
    assert m_bq == {"completed": 1, "avg_score": 9.0, "chapters_read": 100}

    # 3. build_quarterly_report_messages реально собрал отчёт (5 тем),
    #    с заголовком, реальными тайтлами и блоком сравнения (prev-summary дан).
    report = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert len(report) == 5
    assert "КВАРТАЛЬНЫЙ ОТЧЁТ" in report[0]
    assert "Аниме-Один" in report[0]
    assert "Сравнение" in report[4]
    assert "январь — март 2026" in report[4]


@pytest.mark.asyncio
async def test_quarter_rotation_without_preceding_snapshot_omits_comparison(
        backup_env, monkeypatch):
    """Первый отслеженный квартал остаётся отчётным, но без сравнения."""
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    monkeypatch.setattr("handlers.send_backup", AsyncMock(return_value=True))
    monkeypatch.setattr("handlers._save_quarter_snapshot", lambda *a, **k: None)
    monkeypatch.setattr("handlers._update_by_quarter", lambda *a, **k: None)
    monkeypatch.setattr("handlers.save_stats_all", lambda *a, **k: None)

    # Снапшот закрываемого Q2 — приманка для прежней ошибочной загрузки.
    # Настоящий предшествующий Q1 отсутствует, поэтому сравнения быть не должно.
    (backup_env / "quarters" / "2026-Q2.json").write_text(
        '{"period":"2026-Q2","anime_completed":1,"manga_completed":1}',
        encoding="utf-8",
    )

    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(handlers.asyncio, "sleep", _no_sleep)

    cur = {"period": "2026-Q2", "events": []}
    bot = AsyncMock()
    storage.save_stats_current(cur)
    await handlers.rotate_quarter_if_needed(bot, cur, storage._empty_stats_all(), resync=False)

    report = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert report
    assert all("Сравнение" not in message for message in report)


@pytest.mark.asyncio
async def test_rotation_skips_resync_at_boot(backup_env, monkeypatch):
    """resync=False (стартовый вызов): НЕ дёргаем sync_stats_all — polling_loop
    уже дал свежий stats_all; второй синк своей сессией ловил 429 в день ротации."""
    sync = AsyncMock(return_value=({}, True))
    monkeypatch.setattr("handlers.sync_stats_all", sync)
    monkeypatch.setattr("handlers.send_backup", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "handlers.build_quarterly_report_messages",
        lambda *args, **kwargs: Report(()),
    )
    monkeypatch.setattr("handlers._save_quarter_snapshot", lambda *a, **k: None)
    monkeypatch.setattr("handlers._update_by_quarter", lambda *a, **k: None)
    monkeypatch.setattr("handlers._load_prev_quarter_summary", lambda *a, **k: None)
    monkeypatch.setattr("handlers.save_stats_all", lambda *a, **k: None)

    old_cur = {"period": "2025-Q1", "events": []}
    storage.save_stats_current(old_cur)
    await handlers.rotate_quarter_if_needed(AsyncMock(), old_cur, {}, resync=False)
    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotation_resyncs_in_loop(backup_env, monkeypatch):
    """resync=True (дефолт, цикловой вызов): дёргаем sync_stats_all для свежих метаданных."""
    sync = AsyncMock(return_value=({}, True))
    monkeypatch.setattr("handlers.sync_stats_all", sync)
    monkeypatch.setattr("handlers.send_backup", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "handlers.build_quarterly_report_messages",
        lambda *args, **kwargs: Report(()),
    )
    monkeypatch.setattr("handlers._save_quarter_snapshot", lambda *a, **k: None)
    monkeypatch.setattr("handlers._update_by_quarter", lambda *a, **k: None)
    monkeypatch.setattr("handlers._load_prev_quarter_summary", lambda *a, **k: None)
    monkeypatch.setattr("handlers.save_stats_all", lambda *a, **k: None)

    old_cur = {"period": "2025-Q1", "events": []}
    storage.save_stats_current(old_cur)
    await handlers.rotate_quarter_if_needed(AsyncMock(), old_cur, {})
    sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_rotation_retries_report_before_marking_delivery(
    backup_env,
    monkeypatch,
):
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    monkeypatch.setattr(
        "handlers.build_quarterly_report_messages",
        lambda *args: plain_report("REPORT"),
    )
    monkeypatch.setattr("handlers._save_quarter_snapshot", lambda *args: None)
    monkeypatch.setattr("handlers._update_by_quarter", lambda *args: None)
    monkeypatch.setattr("handlers._load_prev_quarter_summary", lambda *args: None)
    monkeypatch.setattr("handlers.save_stats_all", lambda *args: None)
    backup_send = AsyncMock(return_value=True)
    monkeypatch.setattr("handlers.send_backup", backup_send)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())

    bot = AsyncMock()
    bot.send_message.side_effect = [RuntimeError("owner unavailable"), None]
    old_cur = {"period": "2026-Q2", "events": []}
    storage.save_stats_current(old_cur)

    first = await handlers.rotate_quarter_if_needed(
        bot,
        old_cur,
        {},
        resync=False,
    )

    assert first["period"] == "2026-Q3"
    assert first["last_report_sent"] is None
    assert storage.load_subscription_backup_state()["last_backup_at"] is None
    assert first["pending_quarter_delivery"]["next_unit"] == 0
    backup_send.assert_not_awaited()

    second = await handlers.rotate_quarter_if_needed(
        bot,
        first,
        {},
        resync=False,
    )

    assert bot.send_message.await_count == 2
    backup_send.assert_awaited_once()
    assert second["last_report_sent"] == "2026-Q3"
    assert isinstance(
        storage.load_subscription_backup_state()["last_backup_at"],
        float,
    )
    assert second["pending_quarter_delivery"] is None


def _frozen_quarter(messages=None, next_unit=0):
    cur = storage._empty_stats_current("2026-Q3")
    cur["pending_quarter_delivery"] = storage.new_quarter_delivery(
        "2026-Q2", "2026-Q3",
        ["unit-0", "unit-1", "unit-2", "unit-3"] if messages is None else messages,
    )
    cur["pending_quarter_delivery"]["next_unit"] = next_unit
    return cur


def _quarter_archive(cur):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("stats_current.json", json.dumps(cur))
    return stream.getvalue()


@pytest.fixture
def quarter_delivery_env(backup_env, monkeypatch):
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    monkeypatch.setattr("handlers._last_quarter_notice_at", None)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())
    backup_send = AsyncMock(return_value=False)
    monkeypatch.setattr("handlers.send_backup", backup_send)
    return backup_send


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_index", [1, 2])
async def test_quarter_failure_reload_resumes_exact_next_unit(quarter_delivery_env, failed_index):
    cur = _frozen_quarter()
    storage.save_stats_current(cur, strict=True)
    bot = AsyncMock()
    bot.send_message.side_effect = [None] * failed_index + [RuntimeError("failed")]
    await handlers.rotate_quarter_if_needed(bot, cur, {}, resync=False)
    reloaded = storage.load_stats_current(strict=True)
    assert reloaded["pending_quarter_delivery"]["next_unit"] == failed_index
    assert reloaded["pending_quarter_delivery"]["plan_id"] == cur["pending_quarter_delivery"]["plan_id"]
    assert reloaded["last_report_sent"] is None
    quarter_delivery_env.assert_not_awaited()
    bot.send_message.reset_mock(side_effect=True)
    await handlers.rotate_quarter_if_needed(bot, reloaded, {"changed": "data"}, resync=False)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == (
        ["unit-1", "unit-2", "unit-3"] if failed_index == 1 else ["unit-2", "unit-3"]
    )
    final = storage.load_stats_current(strict=True)
    assert final["pending_quarter_delivery"]["next_unit"] == 4
    assert final["last_report_sent"] == "2026-Q3"
    quarter_delivery_env.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_uses_persisted_plan_without_build_or_sync(quarter_delivery_env, monkeypatch):
    cur = _frozen_quarter(next_unit=2)
    storage.save_stats_current(cur, strict=True)
    build = MagicMock(side_effect=AssertionError("rebuild"))
    sync = AsyncMock(side_effect=AssertionError("sync"))
    monkeypatch.setattr("handlers.build_quarterly_report_messages", build)
    monkeypatch.setattr("handlers.sync_stats_all", sync)
    # Даже при следующей календарной ротации старый pending сначала завершается.
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q4")
    bot = AsyncMock()
    await handlers.rotate_quarter_if_needed(bot, {"stale": "caller"}, {})
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == ["unit-2", "unit-3"]
    build.assert_not_called()
    sync.assert_not_awaited()
    assert storage.load_stats_current()["period"] == "2026-Q3"


@pytest.mark.asyncio
async def test_telegram_success_interrupted_before_ack_repeats_only_unacknowledged(quarter_delivery_env, monkeypatch):
    cur = _frozen_quarter(next_unit=1)
    storage.save_stats_current(cur, strict=True)
    real_save = handlers.save_stats_current

    def interrupt(data, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("handlers.save_stats_current", interrupt)
    bot = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await handlers._deliver_pending_quarter(bot, cur)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == ["unit-1"]
    assert storage.load_stats_current()["pending_quarter_delivery"]["next_unit"] == 1
    quarter_delivery_env.assert_not_awaited()
    monkeypatch.setattr("handlers.save_stats_current", real_save)
    bot.send_message.reset_mock()
    await handlers._deliver_pending_quarter(bot, storage.load_stats_current())
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == ["unit-1", "unit-2", "unit-3"]


@pytest.mark.asyncio
async def test_ack_write_failure_retains_disk_progress_and_full_completion(quarter_delivery_env, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="shikiupdatesbot")
    cur = _frozen_quarter(next_unit=2)
    storage.save_stats_current(cur, strict=True)
    original_write = storage._atomic_write

    def fail_ack(path, data):
        if path == storage.STATS_CURRENT_FILE:
            raise OSError("PRIVATE REPORT CONTENT")
        return original_write(path, data)

    monkeypatch.setattr(storage, "_atomic_write", fail_ack)
    bot = AsyncMock()
    await handlers._deliver_pending_quarter(bot, cur)
    persisted = storage.load_stats_current()
    assert persisted["pending_quarter_delivery"]["next_unit"] == 2
    assert persisted["last_report_sent"] is None
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == ["unit-2", handlers._QUARTER_STATE_NOTICE]
    assert "PRIVATE REPORT CONTENT" not in caplog.text
    quarter_delivery_env.assert_not_awaited()
    monkeypatch.setattr(storage, "_atomic_write", original_write)
    bot.send_message.reset_mock()
    await handlers._deliver_pending_quarter(bot, persisted)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == ["unit-2", "unit-3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["identical", "older", "newer", "conflicting"])
async def test_restore_during_report_never_acknowledges_restored_state(quarter_delivery_env, replacement):
    cur = _frozen_quarter(next_unit=1)
    storage.save_stats_current(cur, strict=True)
    restored = deepcopy(cur)
    if replacement == "older":
        restored["pending_quarter_delivery"]["next_unit"] = 0
    elif replacement == "newer":
        restored["pending_quarter_delivery"]["next_unit"] = 2
    elif replacement == "conflicting":
        restored = _frozen_quarter(["another", "report"])

    async def send(**kwargs):
        assert not storage._restorable_state_lock().locked()
        await backup.restore_backup_zip(_quarter_archive(restored))

    bot = AsyncMock()
    bot.send_message.side_effect = send
    await handlers._deliver_pending_quarter(bot, cur)
    bot.send_message.assert_awaited_once()
    assert storage.load_stats_current() == restored
    quarter_delivery_env.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["progress", "plan", "period", "removed", "malformed"])
async def test_concurrent_replacement_is_not_acknowledged(quarter_delivery_env, replacement):
    cur = _frozen_quarter(next_unit=1)
    storage.save_stats_current(cur, strict=True)
    changed = deepcopy(cur)
    if replacement == "progress":
        changed["pending_quarter_delivery"]["next_unit"] = 2
    elif replacement == "plan":
        changed = _frozen_quarter()
    elif replacement == "period":
        changed["period"] = "2026-Q4"
    elif replacement == "removed":
        changed["pending_quarter_delivery"] = None
    else:
        changed["pending_quarter_delivery"]["plan_hash"] = "damaged"

    async def send(**kwargs):
        async with storage.restorable_state_transaction():
            storage.save_stats_current(changed, strict=True)

    bot = AsyncMock()
    bot.send_message.side_effect = send
    await handlers._deliver_pending_quarter(bot, cur)
    assert storage.load_stats_current() == changed
    assert bot.send_message.await_args_list[0].kwargs["text"] == "unit-1"
    assert all(call.kwargs["text"] != "unit-2" for call in bot.send_message.await_args_list)
    quarter_delivery_env.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [False, True])
async def test_legacy_migration_published_before_delivery(quarter_delivery_env, sent):
    cur = storage._empty_stats_current("2026-Q3")
    cur["pending_quarter_delivery"] = {
        "old_period": "2026-Q2", "new_period": "2026-Q3",
        "report_messages": ["legacy-0", "legacy-1"], "report_sent": sent,
    }
    storage.save_stats_current(cur, strict=True)

    async def send(**kwargs):
        pending = storage.load_stats_current()["pending_quarter_delivery"]
        assert pending["version"] == 1
        assert "report_sent" not in pending
        assert pending["report_messages"] == ["legacy-0", "legacy-1"]

    bot = AsyncMock()
    bot.send_message.side_effect = send
    await handlers._deliver_pending_quarter(bot, cur)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == ([] if sent else ["legacy-0", "legacy-1"])
    pending = storage.load_stats_current()["pending_quarter_delivery"]
    assert pending["next_unit"] == 2
    assert storage.load_stats_current()["last_report_sent"] == "2026-Q3"
    quarter_delivery_env.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("messages,next_unit", [([], 0), (["already", "sent"], 2)])
async def test_complete_plan_retries_only_backup_and_clears_after_success(quarter_delivery_env, messages, next_unit):
    cur = _frozen_quarter(messages, next_unit)
    storage.save_stats_current(cur, strict=True)
    bot = AsyncMock()
    quarter_delivery_env.side_effect = [False, True]
    await handlers._deliver_pending_quarter(bot, cur)
    pending = storage.load_stats_current()
    assert pending["last_report_sent"] == "2026-Q3"
    assert pending["pending_quarter_delivery"]["next_unit"] == next_unit
    await handlers._deliver_pending_quarter(bot, pending)
    assert storage.load_stats_current()["pending_quarter_delivery"] is None
    bot.send_message.assert_not_awaited()
    assert quarter_delivery_env.await_count == 2


@pytest.mark.asyncio
async def test_malformed_recovery_preserves_data_and_debounces_safe_notice(quarter_delivery_env, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="shikiupdatesbot")
    cur = _frozen_quarter(["PRIVATE REPORT CONTENT"])
    cur["pending_quarter_delivery"]["version"] = 99
    storage.save_stats_current(cur, strict=True)
    original = storage.STATS_CURRENT_FILE.read_bytes()
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q4")
    bot = AsyncMock()
    await handlers.rotate_quarter_if_needed(bot, cur, {}, resync=False)
    await handlers.rotate_quarter_if_needed(bot, cur, {}, resync=False)
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    bot.send_message.assert_awaited_once_with(chat_id=999, text=handlers._QUARTER_STATE_NOTICE)
    assert "unsupported_version" in caplog.text
    assert "PRIVATE REPORT CONTENT" not in caplog.text
    quarter_delivery_env.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_awaits_retry_and_backup_are_outside_state_lock(quarter_delivery_env, monkeypatch):
    cur = _frozen_quarter(["first", "last"])
    storage.save_stats_current(cur, strict=True)
    sends = 0
    retry_count = 0

    async def send(**kwargs):
        nonlocal sends
        assert not storage._restorable_state_lock().locked()
        current = storage.load_stats_current()
        assert current["last_report_sent"] is None
        sends += 1
        if sends == 1:
            raise TelegramServerError(method=SendMessage(chat_id=999, text="first"), message="transient")
        if sends == 2:
            # Независимое событие не должно теряться после acknowledgement.
            current["events"].append({"id": "added-during-send"})
            async with storage.restorable_state_transaction():
                storage.save_stats_current(current, strict=True)

    async def retry_sleep(delay):
        nonlocal retry_count
        assert not storage._restorable_state_lock().locked()
        retry_count += 1

    async def send_backup(*args):
        assert not storage._restorable_state_lock().locked()
        current = storage.load_stats_current()
        assert current["pending_quarter_delivery"]["next_unit"] == 2
        assert current["last_report_sent"] == "2026-Q3"
        return True

    monkeypatch.setattr(telegram_delivery, "_sleep", retry_sleep)
    quarter_delivery_env.side_effect = send_backup
    bot = AsyncMock()
    bot.send_message.side_effect = send
    await handlers._deliver_pending_quarter(bot, cur)
    assert sends == 3
    assert retry_count == 1
    assert storage.load_stats_current()["events"] == [{"id": "added-during-send"}]


@pytest.mark.asyncio
async def test_identical_restore_during_backup_does_not_clear_pending(quarter_delivery_env):
    cur = _frozen_quarter(["done"], next_unit=1)
    cur["last_report_sent"] = "2026-Q3"
    storage.save_stats_current(cur, strict=True)

    async def restore_while_sending(*args):
        await backup.restore_backup_zip(_quarter_archive(cur))
        return True

    quarter_delivery_env.side_effect = restore_while_sending
    await handlers._deliver_pending_quarter(AsyncMock(), cur)
    assert storage.load_stats_current() == cur
    assert storage.load_subscription_backup_state()["last_backup_at"] is None


@pytest.mark.asyncio
async def test_changed_plan_followup_read_failure_keeps_owner_diagnostic(quarter_delivery_env, monkeypatch):
    cur = _frozen_quarter(["done"], next_unit=1)
    cur["last_report_sent"] = "2026-Q3"
    storage.save_stats_current(cur)

    async def replace_during_backup(*args):
        async with storage.restorable_state_transaction():
            storage.mark_quarter_state_restored()
        monkeypatch.setattr("handlers.load_stats_current", MagicMock(side_effect=[
            deepcopy(cur), storage.QuarterDeliveryStateError("current_read"),
        ]))
        return True

    quarter_delivery_env.side_effect = replace_during_backup
    bot = AsyncMock()
    result = await handlers._deliver_pending_quarter(bot, cur)
    assert result == cur
    assert storage.load_stats_current() == cur
    assert storage.load_subscription_backup_state()["last_backup_at"] is None
    bot.send_message.assert_awaited_once_with(chat_id=999, text=handlers._QUARTER_STATE_NOTICE)


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["old", "2026-Q2 "])
async def test_invalid_rotation_period_cannot_publish_snapshot_or_aggregates(quarter_delivery_env, monkeypatch, period):
    cur = {"period": period, "events": []}
    storage.save_stats_current(cur)
    original = storage.STATS_CURRENT_FILE.read_bytes()
    snapshot = MagicMock()
    save_all = MagicMock()
    monkeypatch.setattr("handlers._save_quarter_snapshot", snapshot)
    monkeypatch.setattr("handlers.save_stats_all", save_all)
    bot = AsyncMock()
    await handlers.rotate_quarter_if_needed(bot, cur, storage._empty_stats_all(), resync=False)
    snapshot.assert_not_called()
    save_all.assert_not_called()
    assert storage.STATS_CURRENT_FILE.read_bytes() == original
    quarter_delivery_env.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(chat_id=999, text=handlers._QUARTER_STATE_NOTICE)


@pytest.mark.asyncio
async def test_concurrent_quarter_attempts_do_not_repeat_acknowledged_report(quarter_delivery_env):
    cur = _frozen_quarter(["first", "last"])
    storage.save_stats_current(cur, strict=True)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(**kwargs):
        if kwargs["text"] == "first":
            entered.set()
            await release.wait()

    bot = AsyncMock()
    bot.send_message.side_effect = send
    first = asyncio.create_task(handlers._deliver_pending_quarter(bot, cur))
    await entered.wait()
    second = asyncio.create_task(handlers._deliver_pending_quarter(bot, cur))
    release.set()
    await asyncio.gather(first, second)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == ["first", "last"]
    assert storage.load_stats_current()["pending_quarter_delivery"]["next_unit"] == 2


@pytest.mark.asyncio
async def test_restore_during_inter_unit_sleep_stops_next_send(quarter_delivery_env, monkeypatch):
    cur = _frozen_quarter(["first", "last"])
    storage.save_stats_current(cur, strict=True)

    async def gap(delay):
        assert not storage._restorable_state_lock().locked()
        assert storage.load_stats_current()["pending_quarter_delivery"]["next_unit"] == 1
        await backup.restore_backup_zip(_quarter_archive(cur))

    monkeypatch.setattr(handlers.asyncio, "sleep", gap)
    bot = AsyncMock()
    await handlers._deliver_pending_quarter(bot, cur)
    bot.send_message.assert_awaited_once()
    assert storage.load_stats_current() == cur
    quarter_delivery_env.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_ack_write_failure_does_not_publish_last_report_sent(quarter_delivery_env, monkeypatch):
    cur = _frozen_quarter(["first", "last"], next_unit=1)
    storage.save_stats_current(cur, strict=True)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "_atomic_write", fail)
    await handlers._deliver_pending_quarter(AsyncMock(), cur)
    assert storage.load_stats_current()["pending_quarter_delivery"]["next_unit"] == 1
    assert storage.load_stats_current()["last_report_sent"] is None
    quarter_delivery_env.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_migration_never_sends_report_or_backup(quarter_delivery_env, monkeypatch):
    cur = storage._empty_stats_current("2026-Q3")
    cur["pending_quarter_delivery"] = {
        "old_period": "2026-Q2", "new_period": "2026-Q3",
        "report_messages": ["legacy"], "report_sent": False,
    }
    storage.save_stats_current(cur, strict=True)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "_atomic_write", fail)
    bot = AsyncMock()
    await handlers._deliver_pending_quarter(bot, cur)
    assert storage.load_stats_current() == cur
    bot.send_message.assert_awaited_once_with(chat_id=999, text=handlers._QUARTER_STATE_NOTICE)
    quarter_delivery_env.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotation_publishes_all_rendered_continuations_before_first_send(quarter_delivery_env, monkeypatch):
    cur = storage._empty_stats_current("2026-Q2")
    storage.save_stats_current(cur, strict=True)
    monkeypatch.setattr("handlers.build_quarterly_report_messages", lambda *args: plain_report("x" * 4096 + "tail"))
    monkeypatch.setattr("handlers._save_quarter_snapshot", lambda *args: None)
    monkeypatch.setattr("handlers._update_by_quarter", lambda *args: None)
    monkeypatch.setattr("handlers._load_prev_quarter_summary", lambda *args: None)
    monkeypatch.setattr("handlers.save_stats_all", lambda *args: None)
    plan_ids = []

    async def send(**kwargs):
        current = storage.load_stats_current(strict=True)
        assert current["period"] == "2026-Q3"
        pending = current["pending_quarter_delivery"]
        assert pending["version"] == 1
        assert pending["report_messages"] == ["x" * 4096, "tail"]
        plan_ids.append(pending["plan_id"])
        if kwargs["text"] == "tail":
            assert pending["next_unit"] == 1
            raise RuntimeError("stop")
        assert pending["next_unit"] == 0

    bot = AsyncMock()
    bot.send_message.side_effect = send
    await handlers.rotate_quarter_if_needed(bot, cur, {}, resync=False)
    assert len(plan_ids) == 2
    assert plan_ids[0] == plan_ids[1]
    quarter_delivery_env.assert_not_awaited()
    bot.send_message.reset_mock(side_effect=True)
    await handlers.rotate_quarter_if_needed(bot, storage.load_stats_current(), {}, resync=False)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == "tail"


@pytest.mark.asyncio
async def test_rotation_plan_write_failure_never_sends_report(quarter_delivery_env, monkeypatch):
    cur = storage._empty_stats_current("2026-Q2")
    storage.save_stats_current(cur, strict=True)
    monkeypatch.setattr("handlers.build_quarterly_report_messages", lambda *args: plain_report("REPORT"))
    monkeypatch.setattr("handlers._save_quarter_snapshot", lambda *args: None)
    monkeypatch.setattr("handlers._update_by_quarter", lambda *args: None)
    monkeypatch.setattr("handlers._load_prev_quarter_summary", lambda *args: None)
    monkeypatch.setattr("handlers.save_stats_all", lambda *args: None)

    def fail(*args):
        raise OSError("disk")

    monkeypatch.setattr(storage, "_atomic_write", fail)
    bot = AsyncMock()
    await handlers.rotate_quarter_if_needed(bot, cur, {}, resync=False)
    assert storage.load_stats_current() == cur
    bot.send_message.assert_awaited_once_with(chat_id=999, text=handlers._QUARTER_STATE_NOTICE)
    quarter_delivery_env.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotation_retries_backup_without_repeating_report(
    backup_env,
    monkeypatch,
):
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q3")
    monkeypatch.setattr(
        "handlers.build_quarterly_report_messages",
        lambda *args: plain_report("REPORT"),
    )
    monkeypatch.setattr("handlers._save_quarter_snapshot", lambda *args: None)
    monkeypatch.setattr("handlers._update_by_quarter", lambda *args: None)
    monkeypatch.setattr("handlers._load_prev_quarter_summary", lambda *args: None)
    monkeypatch.setattr("handlers.save_stats_all", lambda *args: None)
    backup_send = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr("handlers.send_backup", backup_send)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())

    bot = AsyncMock()
    old_cur = {"period": "2026-Q2", "events": []}
    storage.save_stats_current(old_cur)

    first = await handlers.rotate_quarter_if_needed(
        bot,
        old_cur,
        {},
        resync=False,
    )

    assert first["last_report_sent"] == "2026-Q3"
    assert storage.load_subscription_backup_state()["last_backup_at"] is None
    assert first["pending_quarter_delivery"]["next_unit"] == 1

    second = await handlers.rotate_quarter_if_needed(
        bot,
        first,
        {},
        resync=False,
    )

    bot.send_message.assert_awaited_once()
    assert backup_send.await_count == 2
    assert second["last_report_sent"] == "2026-Q3"
    assert isinstance(
        storage.load_subscription_backup_state()["last_backup_at"],
        float,
    )
    assert second["pending_quarter_delivery"] is None


@pytest.mark.asyncio
async def test_split_quarter_plan_resumes_frozen_ranobe_after_reload(quarter_delivery_env, monkeypatch):
    old = {"period": "2026-Q2", "events": [
        {"id": str(tid), "media": "manga", "event": "completed", "score": 8}
        for tid in (1, 2, 3)
    ]}
    stats = storage._empty_stats_all()
    stats["manga"]["titles"] = {
        "1": {"kind": "manga", "title": "Manga", "score": 8},
        "2": {"kind": "novel", "title": "Novel", "score": 8},
        "3": {"kind": "future", "title": "Unknown", "score": 8},
    }
    storage.save_stats_current(old, strict=True)
    sync = AsyncMock(side_effect=AssertionError("Лишний sync"))
    monkeypatch.setattr("handlers.sync_stats_all", sync)
    monkeypatch.setattr("handlers.save_stats_all", MagicMock())
    bot = AsyncMock()
    bot.send_message.side_effect = [None, None, RuntimeError("interrupted")]

    await handlers.rotate_quarter_if_needed(bot, old, stats, resync=False)

    reloaded = storage.load_stats_current(strict=True)
    pending = reloaded["pending_quarter_delivery"]
    frozen = pending["report_messages"]
    assert pending["next_unit"] == 2
    assert "МАНГА" in frozen[1] and "РАНОБЭ" in frozen[2] and "НЕ ОПРЕДЕЛЕНО" in frozen[3]
    quarter_delivery_env.assert_not_awaited()
    # Новые метаданные после рестарта не переклассифицируют уже замороженный отчёт.
    stats["manga"]["titles"]["2"]["kind"] = "manga"
    monkeypatch.setattr("handlers.build_quarterly_report_messages", MagicMock(side_effect=AssertionError("rebuild")))
    bot.send_message.reset_mock(side_effect=True)
    await handlers.rotate_quarter_if_needed(bot, reloaded, stats, resync=False)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == frozen[2:]
    assert storage.load_stats_current(strict=True)["last_report_sent"] == "2026-Q3"
    sync.assert_not_awaited()
    quarter_delivery_env.assert_awaited_once()
