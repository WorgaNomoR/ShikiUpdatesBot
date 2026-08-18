# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import asyncio
import logging

import pytest

from handlers import check_and_notify


def _empty_cur():
    return {"period": "2026-Q2", "events": []}


def _relevant_entry(eid):
    # запись, которая прошла бы is_relevant (anime/tv) и без baseline-ветки
    # ушла бы в чат — тем и докажем, что baseline возвращает ДО отправки
    return {"id": eid, "target": {"type": "Anime", "kind": "tv"},
            "description": "просмотрено"}


class DummyBot:
    pass


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """check_and_notify троттлит рассылку asyncio.sleep — глушим паузы."""
    async def _fast(*args, **kwargs):
        pass
    monkeypatch.setattr(asyncio, "sleep", _fast)


def _patch_history(monkeypatch, entries):
    calls = []

    async def _fetch(session, page=1):
        calls.append(page)
        return entries
    monkeypatch.setattr("handlers.fetch_history", _fetch)
    return calls


def _patch_history_pages(monkeypatch, pages):
    calls = []

    async def _fetch(session, page=1):
        calls.append(page)
        return pages[page]

    monkeypatch.setattr("handlers.fetch_history", _fetch)
    return calls


def _patch_notifying_entries(monkeypatch):
    monkeypatch.setattr("handlers.get_media_info", lambda entry: ("anime", "tv"))
    monkeypatch.setattr("handlers.is_relevant", lambda media_type, kind: True)
    monkeypatch.setattr("handlers.classify_event", lambda description: "started")
    monkeypatch.setattr(
        "handlers.record_current_event",
        lambda cur, entry, event_type, media_type, score: cur,
    )
    monkeypatch.setattr("handlers.build_message", lambda entry: str(entry["id"]))


def _capture_sends(monkeypatch):
    sent = []
    async def _send(bot, text):
        sent.append(text)
    monkeypatch.setattr("handlers.send_to_all_chats", _send)
    return sent


def _capture_saves(monkeypatch):
    saved = []
    monkeypatch.setattr("handlers.save_seen_ids", lambda ids: saved.append(set(ids)))
    monkeypatch.setattr("handlers.save_stats_current", lambda cur: None)
    return saved


@pytest.mark.asyncio
async def test_failed_fetch_skips_cycle(monkeypatch):
    # упавший фетч (None, напр. 429) -> цикл пропущен: не шлём и не сохраняем
    _patch_history(monkeypatch, None)
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)

    result, cur = await check_and_notify(DummyBot(), {5}, _empty_cur())

    assert result == {5}     # seen_ids не тронут
    assert saved == []       # ничего не сохранили
    assert sent == []        # ничего не слали


@pytest.mark.asyncio
async def test_baseline_init_from_empty_seen_no_send(monkeypatch):
    # пустой seen_ids -> baseline, НИЧЕГО не шлём — ДАЖЕ релевантные записи
    # (иначе первый запуск спамит всю историю). Релевантность критична: на
    # нерелевантных тест прошёл бы и с удалённой baseline-веткой (их отсеет
    # is_relevant) — т.е. не охранял бы её.
    entries = [_relevant_entry(eid) for eid in range(51, 0, -1)]
    calls = _patch_history(monkeypatch, entries)
    monkeypatch.setattr("handlers.build_message", lambda e: "SHOULD_NOT_SEND")
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)

    result, cur = await check_and_notify(DummyBot(), set(), _empty_cur())

    assert result == set(range(1, 52))
    assert saved == [set(range(1, 52))]
    assert sent == []
    assert calls == [1]


@pytest.mark.asyncio
async def test_known_boundary_on_full_first_page_uses_one_request(monkeypatch):
    page = [{"id": eid} for eid in range(200, 149, -1)]
    calls = _patch_history_pages(monkeypatch, {1: page})
    _patch_notifying_entries(monkeypatch)
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)

    result, _cur = await check_and_notify(DummyBot(), {150}, _empty_cur())

    assert calls == [1]
    assert sent == [str(eid) for eid in range(151, 201)]
    assert result == {150, *range(151, 201)}
    assert saved == [result]


@pytest.mark.asyncio
async def test_catchup_fetches_until_boundary_deduplicates_and_orders(monkeypatch):
    pages = {
        1: [{"id": eid} for eid in range(250, 199, -1)],
        2: [{"id": eid} for eid in range(200, 149, -1)],
    }
    calls = _patch_history_pages(monkeypatch, pages)
    _patch_notifying_entries(monkeypatch)
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)

    result, _cur = await check_and_notify(DummyBot(), {150}, _empty_cur())

    assert calls == [1, 2]
    assert sent == [str(eid) for eid in range(151, 251)]
    assert sent.count("200") == 1
    assert result == {150, *range(151, 251)}
    assert saved == [result]


@pytest.mark.asyncio
async def test_catchup_short_page_without_boundary_is_exhausted(monkeypatch):
    pages = {
        1: [{"id": eid} for eid in range(350, 299, -1)],
        2: [{"id": 300}, {"id": 299}, {"id": 298}],
    }
    calls = _patch_history_pages(monkeypatch, pages)
    _patch_notifying_entries(monkeypatch)
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)

    result, _cur = await check_and_notify(DummyBot(), {1}, _empty_cur())

    assert calls == [1, 2]
    assert sent == [str(eid) for eid in range(298, 351)]
    assert result == {1, *range(298, 351)}
    assert saved == [result]


@pytest.mark.asyncio
async def test_catchup_failure_on_second_page_keeps_seen_unpublished(monkeypatch):
    pages = {
        1: [{"id": eid} for eid in range(450, 399, -1)],
        2: None,
    }
    calls = _patch_history_pages(monkeypatch, pages)
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)
    original_seen = {1}
    original_cur = _empty_cur()

    result, returned_cur = await check_and_notify(
        DummyBot(), original_seen, original_cur,
    )

    assert calls == [1, 2]
    assert result is original_seen
    assert returned_cur is original_cur
    assert saved == []
    assert sent == []


@pytest.mark.asyncio
async def test_catchup_cap_without_boundary_is_incomplete(monkeypatch, caplog):
    pages = {
        page: [
            {"id": eid}
            for eid in range(1000 - (page - 1) * 50, 949 - (page - 1) * 50, -1)
        ]
        for page in range(1, 6)
    }
    calls = _patch_history_pages(monkeypatch, pages)
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)
    original_seen = {1}

    with caplog.at_level(logging.WARNING):
        result, _cur = await check_and_notify(DummyBot(), original_seen, _empty_cur())

    assert calls == [1, 2, 3, 4, 5]
    assert result is original_seen
    assert saved == []
    assert sent == []
    assert any("за 5 страниц не найдена известная граница" in msg for msg in caplog.messages)


@pytest.mark.asyncio
async def test_empty_history_keeps_seen(monkeypatch):
    _patch_history(monkeypatch, [])
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)

    result, cur = await check_and_notify(DummyBot(), {1, 2, 3}, _empty_cur())

    assert result == {1, 2, 3}
    assert saved == []
    assert sent == []


@pytest.mark.asyncio
async def test_no_new_entries_no_send(monkeypatch):
    _patch_history(monkeypatch, [{"id": 100}])
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)

    await check_and_notify(DummyBot(), {100}, _empty_cur())

    assert sent == []
    assert saved == []


@pytest.mark.asyncio
async def test_new_relevant_entry_sends_and_saves(monkeypatch):
    _patch_history(monkeypatch, [{"id": 123}])
    monkeypatch.setattr("handlers.get_media_info", lambda entry: ("anime", "tv"))
    monkeypatch.setattr("handlers.is_relevant", lambda media_type, kind: True)
    monkeypatch.setattr("handlers.build_message", lambda entry: "MESSAGE")
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)

    result, cur = await check_and_notify(DummyBot(), {999}, _empty_cur())

    assert 123 in result
    assert sent == ["MESSAGE"]
    assert saved == [{999, 123}]


@pytest.mark.asyncio
async def test_new_irrelevant_entry_records_but_no_send(monkeypatch):
    # ВАЖНО: непустой seen_ids — иначе код уходит в baseline-ветку ДО фильтра,
    # и тест на нерелевантность становится холостым (проходит при сломанном фильтре).
    _patch_history(monkeypatch, [{"id": 999}])
    monkeypatch.setattr("handlers.get_media_info", lambda entry: ("anime", "special"))
    monkeypatch.setattr("handlers.is_relevant", lambda media_type, kind: False)
    monkeypatch.setattr("handlers.build_message", lambda entry: "SHOULD_NOT_SEND")
    _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)

    result, cur = await check_and_notify(DummyBot(), {111}, _empty_cur())

    assert 999 in result     # ID запомнен даже для нерелевантного
    assert sent == []        # но сообщение не отправлено (фильтр)


@pytest.mark.asyncio
async def test_unknown_event_sends_and_marks_seen_without_quarter_event(
    monkeypatch,
    caplog,
):
    entry = {
        "id": 123,
        "description": "<b>Неизвестнo & новое</b>",
        "target": {"id": 77, "type": "Anime", "kind": "tv"},
    }
    _patch_history(monkeypatch, [entry])
    monkeypatch.setattr("handlers.get_media_info", lambda item: ("anime", "tv"))
    monkeypatch.setattr("handlers.is_relevant", lambda media_type, kind: True)
    monkeypatch.setattr("handlers.build_message", lambda item: "NEUTRAL")
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)
    cur = _empty_cur()
    expected_cur = _empty_cur()

    with caplog.at_level(logging.WARNING):
        result, returned_cur = await check_and_notify(DummyBot(), {999}, cur)

    assert result == {999, 123}
    assert saved == [{999, 123}]
    assert sent == ["NEUTRAL"]
    assert returned_cur == expected_cur
    assert any(
        "Неизвестно & новое" in message
        for message in caplog.messages
    )


@pytest.mark.asyncio
async def test_ignored_events_are_seen_without_send_stats_or_warning(
    monkeypatch,
    caplog,
):
    entries = [
        {
            "id": 123,
            "description": "Просмотрено 15 эпизодов",
            "target": {"id": 77, "type": "Anime", "kind": "tv"},
        },
        {
            "id": 124,
            "description": "Удалено из списка",
            "target": {"id": 78, "type": "Anime", "kind": "tv"},
        },
    ]
    _patch_history(monkeypatch, entries)
    monkeypatch.setattr("handlers.get_media_info", lambda item: ("anime", "tv"))
    monkeypatch.setattr("handlers.is_relevant", lambda media_type, kind: True)
    monkeypatch.setattr(
        "handlers.build_message",
        lambda item: pytest.fail("для ignored начали строить сообщение"),
    )
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)
    cur = _empty_cur()
    expected_cur = _empty_cur()

    with caplog.at_level(logging.WARNING):
        result, returned_cur = await check_and_notify(DummyBot(), {999}, cur)

    assert result == {999, 123, 124}
    assert saved == [{999, 123, 124}]
    assert sent == []
    assert returned_cur == expected_cur
    assert not caplog.messages


@pytest.mark.asyncio
async def test_score_removed_is_seen_and_clears_current_score_without_send(
    monkeypatch,
    caplog,
):
    entry = {
        "id": 125,
        "description": "Отменена оценка",
        "target": {"id": 79, "type": "Anime", "kind": "tv"},
    }
    _patch_history(monkeypatch, [entry])
    monkeypatch.setattr("handlers.get_media_info", lambda item: ("anime", "tv"))
    monkeypatch.setattr("handlers.is_relevant", lambda media_type, kind: True)
    monkeypatch.setattr(
        "handlers.build_message",
        lambda item: pytest.fail("для score_removed начали строить сообщение"),
    )
    saved = _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)
    cur = _empty_cur()
    cur["events"].append({
        "id": "79",
        "media": "anime",
        "event": "completed",
        "score": 5,
        "recorded_at": "2026-04-01T00:00:00+00:00",
    })

    with caplog.at_level(logging.WARNING):
        result, returned_cur = await check_and_notify(DummyBot(), {999}, cur)

    assert result == {999, 125}
    assert saved == [{999, 125}]
    assert sent == []
    assert returned_cur["events"][0]["score"] is None
    assert not caplog.messages


@pytest.mark.asyncio
async def test_score_set_notifies_and_updates_completed_without_duplicate(monkeypatch):
    entry = {
        "id": 123,
        "description": "Оценено на <b>8</b>",
        "target": {"id": 77, "type": "Anime", "kind": "tv"},
    }
    _patch_history(monkeypatch, [entry])
    monkeypatch.setattr("handlers.get_media_info", lambda item: ("anime", "tv"))
    monkeypatch.setattr("handlers.is_relevant", lambda media_type, kind: True)
    monkeypatch.setattr("handlers.build_message", lambda item: "SCORE")
    _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)
    cur = _empty_cur()
    cur["events"].append({
        "id": "77",
        "media": "anime",
        "event": "completed",
        "score": None,
        "recorded_at": "2026-04-01T00:00:00+00:00",
    })

    _, returned_cur = await check_and_notify(DummyBot(), {999}, cur)

    assert sent == ["SCORE"]
    assert len(returned_cur["events"]) == 1
    assert returned_cur["events"][0]["score"] == 8


@pytest.mark.asyncio
async def test_score_change_updates_completion_through_handler(monkeypatch):
    entries = [
        {
            "id": 123,
            "description": "Просмотрено и оценено на <b>3</b>",
            "target": {"id": 77, "type": "Anime", "kind": "tv"},
        },
        {
            "id": 124,
            "description": "Изменена оценка c <b>3</b> на <b>9</b>",
            "target": {"id": 77, "type": "Anime", "kind": "tv"},
        },
    ]
    _patch_history(monkeypatch, entries)
    monkeypatch.setattr("handlers.get_media_info", lambda item: ("anime", "tv"))
    monkeypatch.setattr("handlers.is_relevant", lambda media_type, kind: True)
    monkeypatch.setattr("handlers.build_message", lambda item: f"EVENT-{item['id']}")
    _capture_saves(monkeypatch)
    sent = _capture_sends(monkeypatch)

    _, returned_cur = await check_and_notify(DummyBot(), {999}, _empty_cur())

    assert sent == ["EVENT-123", "EVENT-124"]
    assert len(returned_cur["events"]) == 1
    assert returned_cur["events"][0]["event"] == "completed"
    assert returned_cur["events"][0]["score"] == 9
