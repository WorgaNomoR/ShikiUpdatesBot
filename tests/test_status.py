import pytest

from main import format_rate_entry


# ============================================================
# format_rate_entry()
# ============================================================

def test_format_rate_entry_russian_title_priority():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Ergo Proxy",
            "russian": "Эрго Прокси",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "Эрго Прокси" in result
    assert "Ergo Proxy" not in result


def test_format_rate_entry_fallback_to_english():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Ergo Proxy",
            "russian": "",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "Ergo Proxy" in result


def test_format_rate_entry_html_escape():
    item = {
        "_status": "watching",
        "anime": {
            "name": "<Ergo & Proxy>",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "&lt;Ergo &amp; Proxy&gt;" in result


def test_format_rate_entry_watching_icon():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert result.startswith("▶️")


def test_format_rate_entry_rewatching_icon():
    item = {
        "_status": "rewatching",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert result.startswith("🔁")


def test_format_rate_entry_unknown_icon():
    item = {
        "_status": "something",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert result.startswith("•")


def test_format_rate_entry_with_link():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Anime",
            "url": "/animes/1-anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert 'href="' in result
    assert "/animes/1-anime" in result


def test_format_rate_entry_without_link():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "href=" not in result


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
    import main

    async def fake_fetch(media, statuses):
        return []

    monkeypatch.setattr(main, "fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await main.cmd_status(msg)

    text = msg.calls[0][0]

    assert "ничего не смотрит" in text.lower()


@pytest.mark.asyncio
async def test_status_api_failure(monkeypatch):
    import main

    async def fake_fetch(media, statuses):
        return None

    monkeypatch.setattr(main, "fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await main.cmd_status(msg)

    text = msg.calls[0][0]

    assert "не удалось получить данные" in text.lower()


@pytest.mark.asyncio
async def test_status_anime_only(monkeypatch):
    import main

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

    monkeypatch.setattr(main, "fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await main.cmd_status(msg)

    text = msg.calls[0][0]

    assert "Сейчас смотрит" in text
    assert "Ergo Proxy" in text
    assert "Сейчас читает" not in text


@pytest.mark.asyncio
async def test_status_manga_only(monkeypatch):
    import main

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

    monkeypatch.setattr(main, "fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await main.cmd_status(msg)

    text = msg.calls[0][0]

    assert "Сейчас читает" in text
    assert "Berserk" in text
    assert "Сейчас смотрит" not in text


@pytest.mark.asyncio
async def test_status_anime_and_manga(monkeypatch):
    import main

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

    monkeypatch.setattr(main, "fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await main.cmd_status(msg)

    text = msg.calls[0][0]

    assert "Сейчас смотрит" in text
    assert "Сейчас читает" in text
    assert "Ergo Proxy" in text
    assert "Berserk" in text


@pytest.mark.asyncio
async def test_status_filters_disallowed_anime_kind(monkeypatch):
    import main

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

    monkeypatch.setattr(main, "fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await main.cmd_status(msg)

    text = msg.calls[0][0]

    assert "ничего не смотрит" in text.lower()


@pytest.mark.asyncio
async def test_status_anime_failed_manga_ok(monkeypatch):
    import main

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

    monkeypatch.setattr(main, "fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await main.cmd_status(msg)

    text = msg.calls[0][0]

    assert "не удалось получить данные" in text.lower()


@pytest.mark.asyncio
async def test_status_manga_failed_anime_ok(monkeypatch):
    import main

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

    monkeypatch.setattr(main, "fetch_current_rates", fake_fetch)

    msg = DummyMessage()

    await main.cmd_status(msg)

    text = msg.calls[0][0]

    assert "не удалось получить данные" in text.lower()
