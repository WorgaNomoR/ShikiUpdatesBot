import asyncio

import pytest

from handlers import check_and_notify


# Пустой стейт квартала — check_and_notify теперь принимает cur третьим аргументом.
def _empty_cur():
    return {"period": "2026-Q2", "events": []}


@pytest.mark.asyncio
async def test_empty_history(monkeypatch):
    async def fake_fetch_history(session):
        return []

    monkeypatch.setattr(
        "handlers.fetch_history",
        fake_fetch_history,
    )

    saved = []

    monkeypatch.setattr(
        "handlers.save_seen_ids",
        lambda ids: saved.append(ids.copy()),
    )

    # check_and_notify в конце зовёт save_stats_current — глушим запись на диск
    monkeypatch.setattr(
        "handlers.save_stats_current",
        lambda cur: None,
    )

    class DummyBot:
        pass

    seen_ids = {1, 2, 3}

    result, cur = await check_and_notify(
        DummyBot(),
        seen_ids.copy(),
        _empty_cur(),
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
        "handlers.fetch_history",
        fake_fetch_history,
    )

    called = False

    async def fake_send(bot, text):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "handlers.send_to_all_chats",
        fake_send,
    )

    monkeypatch.setattr(
        "handlers.save_stats_current",
        lambda cur: None,
    )

    class DummyBot:
        pass

    await check_and_notify(
        DummyBot(),
        {100},
        _empty_cur(),
    )

    assert called is False


@pytest.mark.asyncio
async def test_new_relevant_entry(monkeypatch):
    async def fake_fetch_history(session):
        return [
            {"id": 123},
        ]

    monkeypatch.setattr(
        "handlers.fetch_history",
        fake_fetch_history,
    )

    monkeypatch.setattr(
        "handlers.get_media_info",
        lambda entry: ("anime", "tv"),
    )

    monkeypatch.setattr(
        "handlers.is_relevant",
        lambda media_type, kind: True,
    )

    monkeypatch.setattr(
        "handlers.build_message",
        lambda entry: "MESSAGE",
    )

    sent = []

    async def fake_send(bot, text):
        sent.append(text)

    monkeypatch.setattr(
        "handlers.send_to_all_chats",
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
        "handlers.save_seen_ids",
        lambda ids: saved.append(ids.copy()),
    )

    monkeypatch.setattr(
        "handlers.save_stats_current",
        lambda cur: None,
    )

    class DummyBot:
        pass

    result, cur = await check_and_notify(
        DummyBot(),
        {999},
        _empty_cur(),
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
        "handlers.fetch_history",
        fake_fetch_history,
    )

    monkeypatch.setattr(
        "handlers.get_media_info",
        lambda entry: ("anime", "special"),
    )

    monkeypatch.setattr(
        "handlers.is_relevant",
        lambda media_type, kind: False,
    )

    called = False

    async def fake_send(bot, text):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "handlers.send_to_all_chats",
        fake_send,
    )

    monkeypatch.setattr(
        "handlers.save_stats_current",
        lambda cur: None,
    )

    class DummyBot:
        pass

    result, cur = await check_and_notify(
        DummyBot(),
        set(),
        _empty_cur(),
    )

    assert 999 in result
    assert called is False
