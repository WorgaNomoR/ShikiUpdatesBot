import asyncio

import pytest

from main import check_and_notify


@pytest.mark.asyncio
async def test_empty_history(monkeypatch):
    async def fake_fetch_history(session):
        return []

    monkeypatch.setattr(
        "main.fetch_history",
        fake_fetch_history,
    )

    saved = []

    monkeypatch.setattr(
        "main.save_seen_ids",
        lambda ids: saved.append(ids.copy()),
    )

    class DummyBot:
        pass

    seen_ids = {1, 2, 3}

    result = await check_and_notify(
        DummyBot(),
        seen_ids.copy(),
    )

    assert result == {1, 2, 3}
    assert saved == []


@pytest.mark.asyncio
async def test_no_new_entries(monkeypatch):
    async def fake_fetch_history(session):
        return [
            {"id": 100},
        ]

    monkeypatch.setattr(
        "main.fetch_history",
        fake_fetch_history,
    )

    called = False

    async def fake_send(bot, text):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "main.send_to_all_chats",
        fake_send,
    )

    class DummyBot:
        pass

    await check_and_notify(
        DummyBot(),
        {100},
    )

    assert called is False


@pytest.mark.asyncio
async def test_new_relevant_entry(monkeypatch):
    async def fake_fetch_history(session):
        return [
            {"id": 123},
        ]

    monkeypatch.setattr(
        "main.fetch_history",
        fake_fetch_history,
    )

    monkeypatch.setattr(
        "main.get_media_info",
        lambda entry: ("anime", "tv"),
    )

    monkeypatch.setattr(
        "main.is_relevant",
        lambda media_type, kind: True,
    )

    monkeypatch.setattr(
        "main.build_message",
        lambda entry: "MESSAGE",
    )

    sent = []

    async def fake_send(bot, text):
        sent.append(text)

    monkeypatch.setattr(
        "main.send_to_all_chats",
        fake_send,
    )

    async def fake_sleep(*_):
        pass

    monkeypatch.setattr(
        asyncio,
        "sleep",
        fake_sleep,
    )

    saved = []

    monkeypatch.setattr(
        "main.save_seen_ids",
        lambda ids: saved.append(ids.copy()),
    )

    class DummyBot:
        pass

    result = await check_and_notify(
        DummyBot(),
        set(),
    )

    assert 123 in result
    assert sent == ["MESSAGE"]
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_new_irrelevant_entry(monkeypatch):
    async def fake_fetch_history(session):
        return [
            {"id": 999},
        ]

    monkeypatch.setattr(
        "main.fetch_history",
        fake_fetch_history,
    )

    monkeypatch.setattr(
        "main.get_media_info",
        lambda entry: ("anime", "special"),
    )

    monkeypatch.setattr(
        "main.is_relevant",
        lambda media_type, kind: False,
    )

    called = False

    async def fake_send(bot, text):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "main.send_to_all_chats",
        fake_send,
    )

    class DummyBot:
        pass

    result = await check_and_notify(
        DummyBot(),
        set(),
    )

    assert 999 in result
    assert called is False
