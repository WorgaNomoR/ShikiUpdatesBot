# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import asyncio

import pytest

import config
import handlers
import stats as smod
import storage
import utils


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


# ═══════════════════════════════════════════════════════════════════
#  Тесты слоя ЦИКЛОВЫХ проверок и хелперов (фиксы этой сессии).
#  Существующие loop-тесты выше мокают check_and_notify и проверяют
#  init-блок polling_loop; здесь — сами check_and_notify*, запись
#  событий и предикат ресинка, т.е. слой, которого те тесты не касались.
# ═══════════════════════════════════════════════════════════════════

def _relevant_entry(eid):
    """Запись истории, которая прошла бы is_relevant (kind=tv) и без guard'а
    обязательно ушла бы в чат."""
    return {
        "id": eid,
        "target": {"type": "Anime", "kind": "tv",
                   "name": "Title %d" % eid, "russian": "Тайтл %d" % eid,
                   "url": "/animes/%d" % eid},
        "description": "просмотрено 12 эпизодов",
    }


def _completed_event(tid, score, media="anime"):
    return {"id": str(tid), "media": media, "event": "completed",
            "score": score, "recorded_at": "2026-04-01T00:00:00+00:00"}


# ── guard: пустой baseline ⇒ тихая инициализация без отправки ──

@pytest.mark.asyncio
async def test_check_history_empty_baseline_does_not_spam(monkeypatch):
    """check_and_notify с пустым seen_ids: релевантные записи НЕ уходят в чат,
    baseline молча принимается и сохраняется."""
    sent, saved = [], {}

    async def fake_fetch_history(session):
        return [_relevant_entry(10), _relevant_entry(11), _relevant_entry(12)]

    async def fake_send(bot, text):
        sent.append(text)

    monkeypatch.setattr("handlers.fetch_history", fake_fetch_history)
    monkeypatch.setattr("handlers.send_to_all_chats", fake_send)
    monkeypatch.setattr("handlers.save_seen_ids", lambda s: saved.update(ids=set(s)))
    monkeypatch.setattr("handlers.build_message", lambda e: "msg")
    monkeypatch.setattr("handlers.record_current_event", lambda cur, *a, **k: cur)
    monkeypatch.setattr("handlers.save_stats_current", lambda c: None)

    cur = storage._empty_stats_current(utils.current_quarter())
    seen, _ = await handlers.check_and_notify(bot=None, seen_ids=set(), cur=cur)

    assert sent == []
    assert seen == {10, 11, 12}
    assert saved.get("ids") == {10, 11, 12}


@pytest.mark.asyncio
async def test_check_history_failed_fetch_keeps_state(monkeypatch):
    """fetch_history → None (429): 0 отправок, seen_ids не тронут, save не звался."""
    sent, save_calls = [], []

    async def fake_fetch_history(session):
        return None

    async def fake_send(bot, text):
        sent.append(text)

    monkeypatch.setattr("handlers.fetch_history", fake_fetch_history)
    monkeypatch.setattr("handlers.send_to_all_chats", fake_send)
    monkeypatch.setattr("handlers.save_seen_ids", lambda s: save_calls.append(s))

    baseline = {1, 2, 3}
    cur = storage._empty_stats_current(utils.current_quarter())
    seen, _ = await handlers.check_and_notify(bot=None, seen_ids=set(baseline), cur=cur)

    assert sent == []
    assert seen == baseline
    assert save_calls == []


@pytest.mark.asyncio
async def test_check_favourites_empty_baseline_does_not_spam(monkeypatch):
    """check_and_notify_favourites с пустым seen: 0 отправок, baseline сохранён.
    (Слой ЦИКЛА — в отличие от loop-теста выше, что проверяет init-блок.)"""
    sent, saved = [], {}

    async def fake_fetch_favourites(session):
        return {"animes": [{"id": 100}, {"id": 101}], "mangas": [{"id": 200}]}

    async def fake_send(bot, text):
        sent.append(text)

    monkeypatch.setattr("handlers.fetch_favourites", fake_fetch_favourites)
    monkeypatch.setattr("handlers.send_to_all_chats", fake_send)
    monkeypatch.setattr("handlers.save_seen_favourites", lambda s: saved.update(keys=set(s)))

    seen, _ = await handlers.check_and_notify_favourites(bot=None, seen=set())

    expected = {"animes_100", "animes_101", "mangas_200"}
    assert sent == []
    assert seen == expected
    assert saved.get("keys") == expected


@pytest.mark.asyncio
async def test_check_favourites_failed_fetch_keeps_state(monkeypatch):
    """fetch_favourites → None: 0 отправок, seen не тронут, save не звался."""
    sent, save_calls = [], []

    async def fake_fetch_favourites(session):
        return None

    async def fake_send(bot, text):
        sent.append(text)

    monkeypatch.setattr("handlers.fetch_favourites", fake_fetch_favourites)
    monkeypatch.setattr("handlers.send_to_all_chats", fake_send)
    monkeypatch.setattr("handlers.save_seen_favourites", lambda s: save_calls.append(s))

    baseline = {"animes_1"}
    seen, _ = await handlers.check_and_notify_favourites(bot=None, seen=set(baseline))

    assert sent == []
    assert seen == baseline
    assert save_calls == []


# ── коррекция оценки в том же квартале ──

def test_score_change_updates_existing_completed_event():
    """score_changed по тайтлу с completed-событием квартала ⇒ обновляет его score.
    Кейс «Атака титанов: случайно 3 → исправил»."""
    cur = storage._empty_stats_current(utils.current_quarter())
    cur["events"].append(_completed_event(123, 3))

    out = smod.record_current_event(cur, {"target": {"id": 123}}, "score_changed", "anime", 9)

    completed = [e for e in out["events"] if e["id"] == "123" and e["event"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["score"] == 9
    assert all(e["event"] != "score_changed" for e in out["events"])


def test_score_change_without_completed_is_noop():
    """score_changed по тайтлу вне событий квартала ⇒ ничего не добавляем/не меняем."""
    cur = storage._empty_stats_current(utils.current_quarter())
    out = smod.record_current_event(cur, {"target": {"id": 999}}, "score_changed", "anime", 9)
    assert out["events"] == []


# ── периодический ресинк stats_all ──

def test_should_full_sync_predicate():
    """None ⇒ ретрай каждый цикл; недавно ⇒ ждём; протухло ⇒ пора."""
    iv = 6 * 3600
    assert handlers._should_full_sync(None, 1000.0, iv) is True
    assert handlers._should_full_sync(1000.0, 1000.0 + 10, iv) is False
    assert handlers._should_full_sync(1000.0, 1000.0 + iv, iv) is True
    assert handlers._should_full_sync(1000.0, 1000.0 + iv + 1, iv) is True


@pytest.mark.asyncio
async def test_sync_stats_all_total_failure_preserves_and_flags_false(monkeypatch):
    """Оба экспорта упали (429) ⇒ возвращаем ПРЕЖНИЙ stats_all нетронутым и ok=False,
    save не вызывается. Гарантия «429 не ломает stats_all»."""
    import stats
    preserved = {"_sentinel": "keep-me"}
    saved = []

    async def fake_export(session, media):
        return None

    monkeypatch.setattr(stats, "fetch_list_export", fake_export)
    monkeypatch.setattr("stats.load_stats_all", lambda use_cache=True: preserved)
    monkeypatch.setattr("stats.save_stats_all", lambda d: saved.append(d))

    stats, ok = await smod.sync_stats_all()

    assert ok is False
    assert stats is preserved
    assert saved == []


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
