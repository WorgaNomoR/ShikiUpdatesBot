import asyncio
import pytest

@pytest.mark.asyncio
async def test_first_run_initializes_history_and_favourites(monkeypatch):
    import main

    monkeypatch.setattr(main, "load_seen_ids", lambda: set())
    monkeypatch.setattr(main, "load_seen_favourites", lambda: set())
    monkeypatch.setattr(main, "load_subscribers", lambda: {})

    saved_ids = {}
    saved_favs = {}

    monkeypatch.setattr(
        main,
        "save_seen_ids",
        lambda ids: saved_ids.setdefault("value", ids),
    )

    monkeypatch.setattr(
        main,
        "save_seen_favourites",
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

    monkeypatch.setattr(main, "fetch_history", fake_history)
    monkeypatch.setattr(main, "fetch_favourites", fake_favourites)

    async def fake_check(bot, seen):
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "check_and_notify", fake_check)

    class DummyBot:
        pass

    with pytest.raises(asyncio.CancelledError):
        await main.polling_loop(DummyBot())

    assert saved_ids["value"] == {1, 2}
    assert "animes_10" in saved_favs["value"]


@pytest.mark.asyncio
async def test_missing_seen_favourites_does_not_send_notifications(monkeypatch):
    import main

    # История уже инициализирована
    monkeypatch.setattr(
        main,
        "load_seen_ids",
        lambda: {1, 2, 3},
    )

    # Файл избранного отсутствует
    monkeypatch.setattr(
        main,
        "load_seen_favourites",
        lambda: set(),
    )

    monkeypatch.setattr(
        main,
        "load_subscribers",
        lambda: {},
    )

    saved = {}

    def fake_save(seen):
        saved["value"] = seen

    monkeypatch.setattr(
        main,
        "save_seen_favourites",
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
        main,
        "fetch_favourites",
        fake_fetch,
    )

    called = False

    async def fake_send(bot, text):
        nonlocal called
        called = True

    monkeypatch.setattr(
        main,
        "send_to_all_chats",
        fake_send,
    )

    async def fake_check(bot, seen):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        main,
        "check_and_notify",
        fake_check,
    )

    class DummyBot:
        pass

    with pytest.raises(asyncio.CancelledError):
        await main.polling_loop(DummyBot())

    assert called is False
    assert "animes_10" in saved["value"]
   

@pytest.mark.asyncio
async def test_favourites_initialization_failure(monkeypatch):
    import main

    monkeypatch.setattr(main, "load_seen_ids", lambda: {1})
    monkeypatch.setattr(main, "load_seen_favourites", lambda: set())
    monkeypatch.setattr(main, "load_subscribers", lambda: {})

    save_called = False

    def fake_save(_):
        nonlocal save_called
        save_called = True

    monkeypatch.setattr(
        main,
        "save_seen_favourites",
        fake_save,
    )

    async def fake_fetch(session):
        return None

    monkeypatch.setattr(
        main,
        "fetch_favourites",
        fake_fetch,
    )

    async def fake_check(bot, seen):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        main,
        "check_and_notify",
        fake_check,
    )

    class DummyBot:
        pass

    with pytest.raises(asyncio.CancelledError):
        await main.polling_loop(DummyBot())

    assert save_called is False


@pytest.mark.asyncio
async def test_polling_survives_unexpected_exception(monkeypatch):
    import main

    monkeypatch.setattr(main, "load_seen_ids", lambda: {1})
    monkeypatch.setattr(main, "load_seen_favourites", lambda: {"animes_1"})
    monkeypatch.setattr(main, "load_subscribers", lambda: {})
    monkeypatch.setattr(main, "ERROR_NOTIFY_INTERVAL", 0)

    logged = []

    monkeypatch.setattr(
        main.log,
        "exception",
        lambda *args, **kwargs: logged.append(args),
    )

    sent = []

    class DummyBot:
        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    calls = 0

    async def fake_check(bot, seen):
        nonlocal calls
        calls += 1

        if calls == 1:
            raise RuntimeError("boom")

        raise asyncio.CancelledError

    monkeypatch.setattr(main, "check_and_notify", fake_check)

    async def fake_check_favs(bot, seen):
        return seen

    monkeypatch.setattr(
        main,
        "check_and_notify_favourites",
        fake_check_favs,
    )

    async def fake_sleep(_):
        pass

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main.polling_loop(DummyBot())

    assert calls == 2
    assert logged
    assert sent

    chat_id, text = sent[0]

    assert chat_id == main.OWNER_ID
    assert "RuntimeError" in text
    assert "boom" in text


@pytest.mark.asyncio
async def test_polling_propagates_cancelled_error(monkeypatch):
    import main

    monkeypatch.setattr(main, "load_seen_ids", lambda: {1})
    monkeypatch.setattr(main, "load_seen_favourites", lambda: {"animes_1"})
    monkeypatch.setattr(main, "load_subscribers", lambda: {})

    async def fake_check(bot, seen):
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "check_and_notify", fake_check)

    async def fake_check_favs(bot, seen):
        return seen

    monkeypatch.setattr(main, "check_and_notify_favourites", fake_check_favs)

    class DummyBot:
        async def send_message(self, *args, **kwargs):
            pass

    with pytest.raises(asyncio.CancelledError):
        await main.polling_loop(DummyBot())