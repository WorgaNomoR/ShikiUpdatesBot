import asyncio
import json

import pytest

from main import (
    build_favourite_message,
    check_and_notify_favourites,
    load_seen_favourites,
    save_seen_favourites,
)


# ============================================================
# Storage
# ============================================================

def test_load_seen_favourites_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "main.SEEN_FAVS_FILE",
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
        "main.SEEN_FAVS_FILE",
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
        "main.SEEN_FAVS_FILE",
        str(file),
    )

    assert load_seen_favourites() == set()


def test_seen_favourites_roundtrip(monkeypatch, tmp_path):
    file = tmp_path / "favs.json"

    monkeypatch.setattr(
        "main.SEEN_FAVS_FILE",
        str(file),
    )

    original = {
        "animes_1",
        "mangas_2",
    }

    save_seen_favourites(original)

    assert load_seen_favourites() == original


# ============================================================
# Message building
# ============================================================

def test_build_favourite_message_prefers_russian():
    item = {
        "russian": "Эрго Прокси",
        "name": "Ergo Proxy",
    }

    msg = build_favourite_message("animes", item)

    assert "Эрго Прокси" in msg


def test_build_favourite_message_english_fallback():
    item = {
        "name": "Ergo Proxy",
    }

    msg = build_favourite_message("animes", item)

    assert "Ergo Proxy" in msg


def test_build_favourite_message_html_escape():
    item = {
        "name": "<Ergo & Proxy>",
    }

    msg = build_favourite_message("animes", item)

    assert "&lt;Ergo &amp; Proxy&gt;" in msg


def test_build_favourite_message_link():
    item = {
        "name": "Ergo Proxy",
        "url": "/animes/790-ergo-proxy",
    }

    msg = build_favourite_message("animes", item)

    assert "shikimori.io/animes/790-ergo-proxy" in msg


# ============================================================
# Notification logic
# ============================================================

@pytest.mark.asyncio
async def test_favourites_empty_response(monkeypatch):
    async def fake_fetch(session):
        return {}

    monkeypatch.setattr(
        "main.fetch_favourites",
        fake_fetch,
    )

    class DummyBot:
        pass

    result = await check_and_notify_favourites(
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
        "main.fetch_favourites",
        fake_fetch,
    )

    called = False

    async def fake_send(bot, text):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "main.send_to_all_chats",
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
        "main.fetch_favourites",
        fake_fetch,
    )

    monkeypatch.setattr(
        "main.build_favourite_message",
        lambda category, item: "MESSAGE",
    )

    sent = []

    async def fake_send(bot, text):
        sent.append(text)

    monkeypatch.setattr(
        "main.send_to_all_chats",
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

    result = await check_and_notify_favourites(
        DummyBot(),
        set(),
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
        "main.fetch_favourites",
        fake_fetch,
    )

    monkeypatch.setattr(
        "main.build_favourite_message",
        lambda category, item: f"{category}_{item['id']}",
    )

    sent = []

    async def fake_send(bot, text):
        sent.append(text)

    monkeypatch.setattr(
        "main.send_to_all_chats",
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

    result = await check_and_notify_favourites(
        DummyBot(),
        set(),
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
        "main.fetch_favourites",
        fake_fetch,
    )

    called = False

    async def fake_send(bot, text):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "main.send_to_all_chats",
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

    result = await check_and_notify_favourites(
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
        "main.fetch_favourites",
        fake_fetch,
    )

    called = False

    async def fake_send(bot, text):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "main.send_to_all_chats",
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

    result = await check_and_notify_favourites(
        DummyBot(),
        set(),
    )

    assert result == set()
    assert called is False


@pytest.mark.asyncio
async def test_favourites_init_skipped_when_api_unavailable(monkeypatch):
    import main

    monkeypatch.setattr(
        main,
        "load_seen_ids",
        lambda: {1},
    )

    monkeypatch.setattr(
        main,
        "load_seen_favourites",
        lambda: set(),
    )

    async def fake_fetch(session):
        return None

    monkeypatch.setattr(
        main,
        "fetch_favourites",
        fake_fetch,
    )

    saved = False

    def fake_save(data):
        nonlocal saved
        saved = True

    monkeypatch.setattr(
        main,
        "save_seen_favourites",
        fake_save,
    )

    class StopLoop(Exception):
        pass

    async def fake_check(bot, seen):
        raise StopLoop

    monkeypatch.setattr(
        main,
        "check_and_notify",
        fake_check,
    )

    class DummyBot:
        pass

    with pytest.raises(StopLoop):
        await main.polling_loop(DummyBot())

    assert saved is False