# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import asyncio

import pytest

import handlers
from handlers import send_to_all_chats


class DummyBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """send_to_all_chats троттлит рассылку asyncio.sleep — глушим, чтобы тесты
    не ждали реальные паузы."""
    async def _fast(*args, **kwargs):
        pass
    monkeypatch.setattr(asyncio, "sleep", _fast)


@pytest.mark.asyncio
async def test_no_subscribers(monkeypatch):
    monkeypatch.setattr(
        "handlers.load_subscribers",
        lambda: {},
    )

    saved = []

    monkeypatch.setattr(
        "handlers.save_subscribers",
        lambda subs: saved.append(subs),
    )


    bot = DummyBot()

    await send_to_all_chats(bot, "hello")

    assert bot.sent == []
    assert saved == []


@pytest.mark.asyncio
async def test_single_subscriber(monkeypatch):
    monkeypatch.setattr(
        "handlers.load_subscribers",
        lambda: {123: "Alice"},
    )


    bot = DummyBot()

    await send_to_all_chats(bot, "hello")

    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 123


@pytest.mark.asyncio
async def test_multiple_subscribers(monkeypatch):
    monkeypatch.setattr(
        "handlers.load_subscribers",
        lambda: {
            111: "Alice",
            222: "Bob",
            333: "Carol",
        },
    )


    bot = DummyBot()

    await send_to_all_chats(bot, "hello")

    assert len(bot.sent) == 3
    assert {c for c, _ in bot.sent} == {111, 222, 333}


@pytest.mark.asyncio
async def test_blocked_user_removed(monkeypatch):
    monkeypatch.setattr(
        "handlers.load_subscribers",
        lambda: {
            111: "Alice",
            222: "Bob",
        },
    )

    saved = []

    monkeypatch.setattr(
        "handlers.save_subscribers",
        lambda subs: saved.append(subs.copy()),
    )


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
        "handlers.load_subscribers",
        lambda: {
            111: "Alice",
            222: "Bob",
        },
    )

    saved = []

    monkeypatch.setattr(
        "handlers.save_subscribers",
        lambda subs: saved.append(subs.copy()),
    )


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
        "handlers.load_subscribers",
        lambda: {
            111: "Alice",
        },
    )

    saved = []

    monkeypatch.setattr(
        "handlers.save_subscribers",
        lambda subs: saved.append(subs),
    )


    class BotGenericError:
        async def send_message(self, chat_id, text, parse_mode=None):
            raise Exception("network error")

    await send_to_all_chats(
        BotGenericError(),
        "hello",
    )

    assert saved == []


# ── _is_blocked_error: единый детектор «получатель недоступен» ──────

@pytest.mark.parametrize("msg", [
    "Forbidden: bot was blocked by the user",
    "Forbidden: user is deactivated",
    "Bad Request: chat not found",
])
def test_is_blocked_error_true(msg):
    assert handlers._is_blocked_error(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    "Too Many Requests: retry after 5",
    "Internal Server Error",
    "",
])
def test_is_blocked_error_false(msg):
    assert handlers._is_blocked_error(Exception(msg)) is False
