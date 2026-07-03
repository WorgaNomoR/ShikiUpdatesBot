"""Тесты handlers.cmd_status — команда /status (текущие просмотры/чтение)."""

import pytest

import handlers

# ============================================================
# cmd_status()
# ============================================================

class DummyMessage:
    def __init__(self):
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))


@pytest.mark.asyncio
async def test_status_nothing(monkeypatch):

    async def fake_fetch(media, statuses):
        return []

    monkeypatch.setattr("handlers.fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await handlers.cmd_status(msg)

    text = msg.calls[0][0]

    assert "ничего не смотрит" in text.lower()


@pytest.mark.asyncio
async def test_status_api_failure(monkeypatch):

    async def fake_fetch(media, statuses):
        return None

    monkeypatch.setattr("handlers.fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await handlers.cmd_status(msg)

    text = msg.calls[0][0]

    assert "не удалось получить данные" in text.lower()


@pytest.mark.asyncio
async def test_status_anime_only(monkeypatch):

    async def fake_fetch(media, statuses):
        if media == "anime":
            return [
                {
                    "_status": "watching",
                    "anime": {
                        "name": "Ergo Proxy",
                        "kind": "tv",
                    },
                }
            ]
        return []

    monkeypatch.setattr("handlers.fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await handlers.cmd_status(msg)

    text = msg.calls[0][0]

    assert "Сейчас смотрит" in text
    assert "Ergo Proxy" in text
    assert "Сейчас читает" not in text


@pytest.mark.asyncio
async def test_status_manga_only(monkeypatch):

    async def fake_fetch(media, statuses):
        if media == "manga":
            return [
                {
                    "_status": "watching",
                    "manga": {
                        "name": "Berserk",
                    },
                }
            ]
        return []

    monkeypatch.setattr("handlers.fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await handlers.cmd_status(msg)

    text = msg.calls[0][0]

    assert "Сейчас читает" in text
    assert "Berserk" in text
    assert "Сейчас смотрит" not in text


@pytest.mark.asyncio
async def test_status_anime_and_manga(monkeypatch):

    async def fake_fetch(media, statuses):
        if media == "anime":
            return [
                {
                    "_status": "watching",
                    "anime": {
                        "name": "Ergo Proxy",
                        "kind": "tv",
                    },
                }
            ]

        return [
            {
                "_status": "watching",
                "manga": {
                    "name": "Berserk",
                },
            }
        ]

    monkeypatch.setattr("handlers.fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await handlers.cmd_status(msg)

    text = msg.calls[0][0]

    assert "Сейчас смотрит" in text
    assert "Сейчас читает" in text
    assert "Ergo Proxy" in text
    assert "Berserk" in text


@pytest.mark.asyncio
async def test_status_filters_disallowed_anime_kind(monkeypatch):

    async def fake_fetch(media, statuses):
        if media == "anime":
            return [
                {
                    "_status": "watching",
                    "anime": {
                        "name": "Music Clip",
                        "kind": "music",
                    },
                }
            ]
        return []

    monkeypatch.setattr("handlers.fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await handlers.cmd_status(msg)

    text = msg.calls[0][0]

    assert "ничего не смотрит" in text.lower()


@pytest.mark.asyncio
async def test_status_anime_failed_manga_ok(monkeypatch):

    async def fake_fetch(media, statuses):
        if media == "anime":
            return None

        return [
            {
                "_status": "watching",
                "manga": {
                    "name": "Berserk",
                },
            }
        ]

    monkeypatch.setattr("handlers.fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await handlers.cmd_status(msg)

    text = msg.calls[0][0]

    assert "не удалось получить данные" in text.lower()


@pytest.mark.asyncio
async def test_status_manga_failed_anime_ok(monkeypatch):

    async def fake_fetch(media, statuses):
        if media == "manga":
            return None

        return [
            {
                "_status": "watching",
                "anime": {
                    "name": "Ergo Proxy",
                    "kind": "tv",
                },
            }
        ]

    monkeypatch.setattr("handlers.fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await handlers.cmd_status(msg)

    text = msg.calls[0][0]

    assert "не удалось получить данные" in text.lower()
