# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Тесты фичи «избранное»: флоу check_and_notify_favourites + сбор
(_collect_favourites/build_favourites_messages) + seen-хранилище.
build_favourite_message (leaf messages) вынесен в test_messages.py."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import handlers
import shiki_api
import stats as smod
import storage
from handlers import check_and_notify_favourites
from storage import (
    load_seen_favourites,
    save_seen_favourites,
)

# Срез реального ответа /favourites: все 8 категорий, url=null везде,
# у TeddyLoid russian="" (должен фолбэкнуться на name).
FAV_SAMPLE = {
    "animes": [
        {"id": 226, "name": "Elfen Lied", "russian": "Эльфийская песнь", "url": None},
    ],
    "mangas": [
        {"id": 21525, "name": "Akatsuki no Yona", "russian": "Йона на заре", "url": None},
    ],
    "ranobe": [
        {"id": 74697, "name": "Re:Zero", "russian": "Re:Zero. Жизнь с нуля", "url": None},
    ],
    "characters": [],
    "people": [
        {"id": 30805, "name": "TeddyLoid", "russian": "", "url": None},
    ],
    "mangakas": [
        {"id": 32649, "name": "Tappei Nagatsuki", "russian": "Таппэй Нагацуки", "url": None},
    ],
    "seyu": [
        {"id": 34785, "name": "Rie Takahashi", "russian": "Риэ Такахаси", "url": None},
    ],
    "producers": [
        {"id": 38963, "name": "Masahiro Shinohara", "russian": "Масахиро Синохара", "url": None},
    ],
}


@pytest.fixture
def silence_favourites_io(monkeypatch):
    """Глушит общую рутину тестов check_and_notify_favourites: запись seen
    избранного и asyncio.sleep (рассылку каждый тест мокает сам — её инспектируют)."""
    monkeypatch.setattr("handlers.save_seen_favourites", lambda *a, **k: None)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())


def _stats_with_titles():
    """stats_all с парой тайтлов для проверки джойна ссылок/оценок."""
    stats = storage._empty_stats_all()
    stats["anime"]["titles"] = {
        "226": {"title": "Эльфийская песнь", "url": "/animes/226-elfen-lied",
                "score": 9, "kind": "tv", "status": "completed"},
    }
    stats["manga"]["titles"] = {
        "21525": {"title": "Йона на заре", "url": "/mangas/21525-akatsuki-no-yona",
                  "score": 8, "kind": "manga", "status": "completed"},
    }
    return stats


# ============================================================
# Storage
# ============================================================

def test_load_seen_favourites_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "storage.SEEN_FAVS_FILE",
        str(tmp_path / "missing.json"),
    )

    assert load_seen_favourites() == set()


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

    assert load_seen_favourites() == {
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

    assert load_seen_favourites() == set()


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

    save_seen_favourites(original)

    assert load_seen_favourites() == original


# ============================================================
# Notification logic
# ============================================================

@pytest.mark.asyncio
async def test_favourites_empty_response(monkeypatch):
    async def fake_fetch(session):
        return {}

    monkeypatch.setattr(
        "handlers.fetch_favourites",
        fake_fetch,
    )

    class DummyBot:
        pass

    result, _ = await check_and_notify_favourites(
        DummyBot(),
        set(),
    )

    assert result == set()


@pytest.mark.asyncio
async def test_favourites_no_changes(monkeypatch):
    async def fake_fetch(session):
        return {
            "animes": [
                {"id": 1}
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

    async def fake_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(
        asyncio,
        "sleep",
        fake_sleep,
    )

    class DummyBot:
        pass

    await check_and_notify_favourites(
        DummyBot(),
        {"animes_1"},
    )

    assert called is False


@pytest.mark.asyncio
async def test_new_favourite(monkeypatch):
    async def fake_fetch(session):
        return {
            "animes": [
                {
                    "id": 1,
                    "name": "Ergo Proxy",
                }
            ]
        }

    monkeypatch.setattr(
        "handlers.fetch_favourites",
        fake_fetch,
    )

    monkeypatch.setattr(
        "handlers.build_favourite_message",
        lambda category, item: "MESSAGE",
    )

    sent = []

    async def fake_send(bot, text):
        sent.append(text)

    monkeypatch.setattr(
        "handlers.send_to_all_chats",
        fake_send,
    )

    async def fake_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(
        asyncio,
        "sleep",
        fake_sleep,
    )

    # Изоляция от ФС: ветка found_new иначе читает/пишет stats_all и seen.
    monkeypatch.setattr("handlers.load_stats_all",
                        lambda *a, **k: {"anime": {"titles": {}}, "manga": {"titles": {}}})
    monkeypatch.setattr("handlers.save_stats_all", lambda *a, **k: None)
    monkeypatch.setattr("handlers.save_seen_favourites", lambda *a, **k: None)

    class DummyBot:
        pass

    result, _ = await check_and_notify_favourites(
        DummyBot(),
        {"animes_999"},
    )

    assert "animes_1" in result
    assert sent == ["MESSAGE"]


@pytest.mark.asyncio
async def test_multiple_new_favourites(monkeypatch):
    async def fake_fetch(session):
        return {
            "animes": [{"id": 1}],
            "mangas": [{"id": 2}],
            "characters": [{"id": 3}],
        }

    monkeypatch.setattr(
        "handlers.fetch_favourites",
        fake_fetch,
    )

    monkeypatch.setattr(
        "handlers.build_favourite_message",
        lambda category, item: f"{category}_{item['id']}",
    )

    sent = []

    async def fake_send(bot, text):
        sent.append(text)

    monkeypatch.setattr(
        "handlers.send_to_all_chats",
        fake_send,
    )

    async def fake_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(
        asyncio,
        "sleep",
        fake_sleep,
    )

    # Изоляция от ФС: ветка found_new иначе читает/пишет stats_all и seen.
    monkeypatch.setattr("handlers.load_stats_all",
                        lambda *a, **k: {"anime": {"titles": {}}, "manga": {"titles": {}}})
    monkeypatch.setattr("handlers.save_stats_all", lambda *a, **k: None)
    monkeypatch.setattr("handlers.save_seen_favourites", lambda *a, **k: None)

    class DummyBot:
        pass

    result, _ = await check_and_notify_favourites(
        DummyBot(),
        {"animes_999"},
    )

    assert "animes_1" in result
    assert "mangas_2" in result
    assert "characters_3" in result
    assert len(sent) == 3


@pytest.mark.asyncio
async def test_favourite_without_id_is_ignored(monkeypatch):
    async def fake_fetch(session):
        return {
            "animes": [
                {
                    "name": "Broken object"
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

    async def fake_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(
        asyncio,
        "sleep",
        fake_sleep,
    )

    class DummyBot:
        pass

    result, _ = await check_and_notify_favourites(
        DummyBot(),
        set(),
    )

    assert result == set()
    assert called is False


@pytest.mark.asyncio
async def test_untracked_category_is_ignored(monkeypatch):
    async def fake_fetch(session):
        return {
            "studios": [
                {
                    "id": 1,
                    "name": "Studio Trigger",
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

    async def fake_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(
        asyncio,
        "sleep",
        fake_sleep,
    )

    class DummyBot:
        pass

    result, _ = await check_and_notify_favourites(
        DummyBot(),
        set(),
    )

    assert result == set()
    assert called is False


@pytest.mark.asyncio
async def test_favourites_init_skipped_when_api_unavailable(monkeypatch):

    monkeypatch.setattr("handlers.load_seen_ids", lambda: {1})
    monkeypatch.setattr("handlers.load_seen_favourites", lambda: set())
    monkeypatch.setattr("handlers.load_stats_current", lambda: {"period": "2026-Q2", "events": []})

    async def fake_fetch(session):
        return None
    monkeypatch.setattr("handlers.fetch_favourites", fake_fetch)

    saved = False
    def fake_save(data):
        nonlocal saved
        saved = True
    monkeypatch.setattr("handlers.save_seen_favourites", fake_save)

    # sync_stats_all и rotate_quarter_if_needed делают сетевые вызовы — мокаем
    async def fake_sync():
        return storage._empty_stats_all() if hasattr("handlers._empty_stats_all") else {}
    monkeypatch.setattr("handlers.sync_stats_all", fake_sync)

    async def fake_rotate(bot, cur, stats_all):
        return cur
    monkeypatch.setattr("handlers.rotate_quarter_if_needed", fake_rotate)

    # check_and_notify теперь принимает (bot, seen_ids, cur) и возвращает (seen_ids, cur)
    async def fake_check(bot, seen, cur):
        raise asyncio.CancelledError
    monkeypatch.setattr("handlers.check_and_notify", fake_check)

    class DummyBot:
        pass

    with pytest.raises(asyncio.CancelledError):
        await handlers.polling_loop(DummyBot())

    assert saved is False


# ═══════════════════════════════════════════════════════════════
#  Ветка favourites-fix: категории (ранобэ + слияние индустрии),
#  джойн ссылок, пересборка stats["favourites"] при found_new.
# ═══════════════════════════════════════════════════════════════

def test_collect_favourites_merges_industry_and_adds_ranobe():
    stats = storage._empty_stats_all()
    out = asyncio.run(smod._collect_favourites(None, stats, fav=FAV_SAMPLE))
    fav = out["favourites"]

    # Ранобэ — отдельный блок
    assert len(fav["ranobe"]) == 1
    assert fav["ranobe"][0]["id"] == "74697"

    # people + mangakas + seyu + producers слиты в один блок (4 человека)
    assert len(fav["people"]) == 4
    ids = {p["id"] for p in fav["people"]}
    assert ids == {"30805", "32649", "34785", "38963"}

    # Персонажи отдельно и пусты в этом срезе
    assert fav["characters"] == []


def test_collect_favourites_empty_russian_falls_back_to_name():
    stats = storage._empty_stats_all()
    out = asyncio.run(smod._collect_favourites(None, stats, fav=FAV_SAMPLE))
    teddy = next(p for p in out["favourites"]["people"] if p["id"] == "30805")
    # russian был "" — заголовок не должен быть пустым, берём name
    assert teddy["title"] == "TeddyLoid"


def test_collect_favourites_url_join_from_titles():
    stats = _stats_with_titles()
    out = asyncio.run(smod._collect_favourites(None, stats, fav=FAV_SAMPLE))
    anime = out["favourites"]["anime"][0]
    assert anime["url"] == "/animes/226-elfen-lied"   # ссылка подтянута из titles
    assert anime["score"] == 9                          # и оценка


def test_build_favourites_messages_has_ranobe_and_industry_blocks():
    stats = storage._empty_stats_all()
    stats["favourites"]["ranobe"] = [{"id": "1", "title": "Ранобэ-тайтл", "url": ""}]
    stats["favourites"]["people"] = [{"id": "2", "title": "Человек", "url": ""}]
    msg = smod.build_favourites_messages(stats)[0]
    assert "Ранобэ" in msg
    assert "Люди индустрии" in msg


@pytest.mark.asyncio
async def test_check_and_notify_favourites_joins_url_in_notification(monkeypatch, silence_favourites_io):
    """Баг 1: уведомление о новом аниме должно содержать ссылку из titles{}."""

    fav = {k: [] for k in shiki_api._FAV_CATEGORIES}
    fav["animes"] = [{"id": 226, "name": "Elfen Lied",
                      "russian": "Эльфийская песнь", "url": None}]

    sent = []
    monkeypatch.setattr("handlers.fetch_favourites", AsyncMock(return_value=fav))
    monkeypatch.setattr("handlers.send_to_all_chats",
                        AsyncMock(side_effect=lambda bot, text: sent.append(text)))
    monkeypatch.setattr("handlers.load_stats_all", lambda *a, **k: _stats_with_titles())
    monkeypatch.setattr("handlers.save_stats_all", lambda *a, **k: None)

    # baseline: всё уже виденное, КРОМЕ нового аниме 226
    seen = {f"{c}_{i['id']}" for c in shiki_api._FAV_CATEGORIES
            for i in fav.get(c, []) if i["id"] != 226}
    seen.add("animes_999")  # чтобы baseline не был пустым

    new_seen, found_new = await handlers.check_and_notify_favourites(None, seen)

    assert found_new is True
    assert len(sent) == 1
    assert 'href="https://shikimori.io/animes/226-elfen-lied"' in sent[0]


@pytest.mark.asyncio
async def test_check_and_notify_favourites_refreshes_stats(monkeypatch, silence_favourites_io):
    """Unit 3: при found_new stats["favourites"] пересобирается из скачанного
    списка — без повторного fetch_favourites."""

    fav = {k: [] for k in shiki_api._FAV_CATEGORIES}
    fav["mangas"] = [{"id": 21525, "name": "Akatsuki no Yona",
                      "russian": "Йона на заре", "url": None}]

    saved = {}
    monkeypatch.setattr("handlers.fetch_favourites", AsyncMock(return_value=fav))
    monkeypatch.setattr("handlers.send_to_all_chats", AsyncMock())
    monkeypatch.setattr("handlers.load_stats_all", lambda *a, **k: _stats_with_titles())
    monkeypatch.setattr("handlers.save_stats_all",
                        lambda data: saved.update(data))

    seen = {"mangas_1"}  # непустой baseline, нового тайтла там нет

    _, found_new = await handlers.check_and_notify_favourites(None, seen)

    assert found_new is True
    # stats сохранён и manga-блок избранного содержит новый тайтл с джойном ссылки
    assert saved, "save_stats_all не вызван"
    manga_favs = saved["favourites"]["manga"]
    assert any(e["id"] == "21525" and e["url"] == "/mangas/21525-akatsuki-no-yona"
               for e in manga_favs)
    # fetch_favourites вызван РОВНО один раз (нет второго запроса при пересборке)
    assert handlers.fetch_favourites.await_count == 1


@pytest.mark.asyncio
async def test_check_and_notify_favourites_no_new_returns_false(monkeypatch, silence_favourites_io):

    fav = {k: [] for k in shiki_api._FAV_CATEGORIES}
    fav["animes"] = [{"id": 226, "name": "Elfen Lied",
                      "russian": "Эльфийская песнь", "url": None}]

    monkeypatch.setattr("handlers.fetch_favourites", AsyncMock(return_value=fav))
    monkeypatch.setattr("handlers.send_to_all_chats", AsyncMock())
    monkeypatch.setattr("handlers.load_stats_all", lambda *a, **k: _stats_with_titles())
    monkeypatch.setattr("handlers.save_stats_all", lambda *a, **k: None)

    seen = {"animes_226", "animes_999"}  # 226 уже виден
    _, found_new = await handlers.check_and_notify_favourites(None, seen)
    assert found_new is False


@pytest.mark.asyncio
async def test_check_and_notify_favourites_dedups_industry_people(monkeypatch, silence_favourites_io):
    """Один человек в нескольких ролях (seyu + producers) → ОДНО уведомление,
    но обе ролевые seen-записи зафиксированы."""

    fav = {k: [] for k in shiki_api._FAV_CATEGORIES}
    person = {"id": 34785, "name": "Rie Takahashi",
              "russian": "Риэ Такахаси", "url": None}
    fav["seyu"] = [person]
    fav["producers"] = [person]  # тот же человек во второй роли

    sent = []
    monkeypatch.setattr("handlers.fetch_favourites", AsyncMock(return_value=fav))
    monkeypatch.setattr("handlers.send_to_all_chats",
                        AsyncMock(side_effect=lambda bot, text: sent.append(text)))
    monkeypatch.setattr("handlers.load_stats_all",
                        lambda *a, **k: {"anime": {"titles": {}}, "manga": {"titles": {}}})
    monkeypatch.setattr("handlers.save_stats_all", lambda *a, **k: None)

    seen = {"animes_999"}  # непустой baseline без этого человека
    new_seen, found_new = await handlers.check_and_notify_favourites(None, seen)

    assert found_new is True
    assert len(sent) == 1                      # одно сообщение, не два
    assert "seyu_34785" in new_seen            # обе роли отмечены виденными,
    assert "producers_34785" in new_seen       # иначе зацикливание на «новом»
