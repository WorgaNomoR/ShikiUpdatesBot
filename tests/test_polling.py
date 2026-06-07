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

    class StopLoop(Exception):
        pass

    async def fake_check(bot, seen):
        raise StopLoop

    monkeypatch.setattr(main, "check_and_notify", fake_check)

    class DummyBot:
        pass

    with pytest.raises(StopLoop):
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

    assert save_called is False