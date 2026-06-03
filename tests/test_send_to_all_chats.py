import asyncio

import pytest

from main import send_to_all_chats


class DummyBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_no_subscribers(monkeypatch):
    monkeypatch.setattr(
        "main.load_subscribers",
        lambda: {},
    )

    saved = []

    monkeypatch.setattr(
        "main.save_subscribers",
        lambda subs: saved.append(subs),
    )

    async def fake_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    bot = DummyBot()

    await send_to_all_chats(bot, "hello")

    assert bot.sent == []
    assert saved == []


@pytest.mark.asyncio
async def test_single_subscriber(monkeypatch):
    monkeypatch.setattr(
        "main.load_subscribers",
        lambda: {123: "Alice"},
    )

    async def fake_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    bot = DummyBot()

    await send_to_all_chats(bot, "hello")

    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 123


@pytest.mark.asyncio
async def test_multiple_subscribers(monkeypatch):
    monkeypatch.setattr(
        "main.load_subscribers",
        lambda: {
            111: "Alice",
            222: "Bob",
            333: "Carol",
        },
    )

    async def fake_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    bot = DummyBot()

    await send_to_all_chats(bot, "hello")

    assert len(bot.sent) == 3


@pytest.mark.asyncio
async def test_blocked_user_removed(monkeypatch):
    monkeypatch.setattr(
        "main.load_subscribers",
        lambda: {
            111: "Alice",
            222: "Bob",
        },
    )

    saved = []

    monkeypatch.setattr(
        "main.save_subscribers",
        lambda subs: saved.append(subs.copy()),
    )

    async def fake_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class BotWithBlockedUser:
        async def send_message(self, chat_id, text, parse_mode=None):
            if chat_id == 111:
                raise Exception("bot was blocked")
            return

    await send_to_all_chats(
        BotWithBlockedUser(),
        "hello",
    )

    assert len(saved) == 1
    assert 111 not in saved[0]
    assert 222 in saved[0]


@pytest.mark.asyncio
async def test_chat_not_found_removed(monkeypatch):
    monkeypatch.setattr(
        "main.load_subscribers",
        lambda: {
            111: "Alice",
            222: "Bob",
        },
    )

    saved = []

    monkeypatch.setattr(
        "main.save_subscribers",
        lambda subs: saved.append(subs.copy()),
    )

    async def fake_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class BotChatNotFound:
        async def send_message(self, chat_id, text, parse_mode=None):
            if chat_id == 111:
                raise Exception("chat not found")

    await send_to_all_chats(
        BotChatNotFound(),
        "hello",
    )

    assert len(saved) == 1
    assert 111 not in saved[0]


@pytest.mark.asyncio
async def test_generic_error_does_not_remove_user(monkeypatch):
    monkeypatch.setattr(
        "main.load_subscribers",
        lambda: {
            111: "Alice",
        },
    )

    saved = []

    monkeypatch.setattr(
        "main.save_subscribers",
        lambda subs: saved.append(subs),
    )

    async def fake_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class BotGenericError:
        async def send_message(self, chat_id, text, parse_mode=None):
            raise Exception("network error")

    await send_to_all_chats(
        BotGenericError(),
        "hello",
    )

    assert saved == []
