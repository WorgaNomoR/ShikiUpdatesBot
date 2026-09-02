# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Owner-only оркестрация локального меню /pick и его FSM-сессии."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest

import handlers
import stats as smod
import storage


class _State:
    def __init__(self):
        self.state = None
        self.data = {}

    async def get_state(self):
        return self.state

    async def set_state(self, value):
        self.state = getattr(value, "state", value)

    async def get_data(self):
        return deepcopy(self.data)

    async def update_data(self, **kwargs):
        self.data.update(deepcopy(kwargs))

    async def clear(self):
        self.state = None
        self.data = {}


def _stats(*, unresolved=False):
    data = storage._empty_stats_all()
    data["updated_at"] = "2026-09-01T10:00:00+00:00"
    data["anime"]["titles"] = {
        "1": {
            "title": "A < B & C",
            "title_en": "A & C",
            "status": "planned",
            "release_status": "released",
            "kind": "tv",
            "url": "https://shikimori.one/animes/1-a",
            "poster_url": "https://cdn.example/anime-1.jpg",
            "year": 2011,
            "shiki_score": 8.5,
            "genres": ["Экшен", "Драма"],
            "themes": ["Путешествие"],
            "demographic": ["Сэйнэн"],
            "episodes_total": 12,
            "duration": 24,
            "rating": "R-17",
            "origin": "Манга",
            "studios": ["Studio & Co"],
        },
        "2": {
            "title": "Второй вариант",
            "status": "planned",
            "release_status": "released",
            "kind": "movie",
            "url": "/animes/2",
            "poster_url": "https://cdn.example/anime-2.jpg",
            "year": 1998,
            "genres": ["Комедия"],
        },
        "3": {
            "title": "Не planned",
            "status": "completed",
            "kind": "tv",
            "url": "/animes/3",
            "year": 2020,
            "genres": [],
        },
    }
    if unresolved:
        data["manga"]["titles"]["10"] = {
            "title": "Не определено",
            "status": "planned",
            "kind": "future_kind",
            "url": "/mangas/10",
            "year": None,
            "genres": [],
        }
    return data


def _message(*, owner=True, message_id=100):
    bot = SimpleNamespace(delete_message=AsyncMock())
    menu = SimpleNamespace(message_id=200)
    return SimpleNamespace(
        from_user=SimpleNamespace(id=handlers.OWNER_ID if owner else 777),
        chat=SimpleNamespace(id=55),
        message_id=message_id,
        bot=bot,
        answer=AsyncMock(),
        reply=AsyncMock(return_value=menu),
    )


def _callback(
    data,
    *,
    owner=True,
    message_id=200,
    with_message=True,
    replacement_id=201,
):
    message = None
    if with_message:
        message = SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=55),
            edit_text=AsyncMock(),
            edit_media=AsyncMock(),
            answer=AsyncMock(
                return_value=SimpleNamespace(message_id=replacement_id),
            ),
            delete=AsyncMock(),
            edit_reply_markup=AsyncMock(),
            reply_to_message=SimpleNamespace(delete=AsyncMock()),
        )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=handlers.OWNER_ID if owner else 777),
        message=message,
        answer=AsyncMock(),
    )


def _patch_snapshot(monkeypatch, data, *, state=storage.STATS_ALL_VALID):
    load = MagicMock(return_value=storage.StatsAllSnapshot(data, state))
    monkeypatch.setattr(handlers, "load_stats_all_snapshot", load)
    return load


def _callback_data(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def _button_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_owner_command_opens_hidden_three_category_menu(monkeypatch):
    load = _patch_snapshot(monkeypatch, _stats(unresolved=True))
    message = _message()
    state = _State()

    await handlers.cmd_pick(message, state)

    load.assert_called_once_with()
    message.reply.assert_awaited_once()
    text = message.reply.await_args.args[0]
    markup = message.reply.await_args.kwargs["reply_markup"]
    assert "Не можешь решить, что посмотреть или почитать?" in text
    assert "давай найдём что-нибудь в списке «Запланировано»" in text
    assert "Часть запланированных тайтлов (<b>1</b>)" in text
    assert "01.09.2026 10:00 UTC" in text
    assert _callback_data(markup) == [
        "pick:anime",
        "pick:manga",
        "pick:ranobe",
        "pick:cancel",
    ]
    assert state.state == handlers.PickStates.active.state
    assert state.data["pick_menu_message_id"] == 200
    assert state.data["pick_menu_kind"] == handlers._PICK_MENU_TEXT
    assert state.data["pick_command_message_id"] == 100


@pytest.mark.asyncio
async def test_non_owner_command_and_forged_callback_are_rejected_before_local_read(
    monkeypatch,
):
    load = MagicMock(side_effect=AssertionError("non-owner прочитал stats_all"))
    monkeypatch.setattr(handlers, "load_stats_all_snapshot", load)
    state = _State()
    outsider = _message(owner=False)

    await handlers.cmd_pick(outsider, state)

    outsider.answer.assert_awaited_once_with("🚫 Эта команда только для владельца бота.")
    load.assert_not_called()

    state.state = handlers.PickStates.active.state
    state.data = {"pick_menu_chat_id": 55, "pick_menu_message_id": 200}
    forged = _callback("pick:anime", owner=False)
    await handlers.pick_menu_cb(forged, state)

    forged.answer.assert_awaited_once_with("🚫 Только для владельца.", show_alert=True)
    load.assert_not_called()
    assert state.state == handlers.PickStates.active.state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "other_state",
    [
        handlers.BroadcastStates.waiting_content.state,
        handlers.BroadcastStates.waiting_confirm.state,
        handlers.BackupStates.waiting_import_file.state,
        handlers.FactsStates.waiting_upload_file.state,
        handlers.FactsStates.waiting_apply_confirmation.state,
    ],
)
async def test_owner_command_preserves_other_active_owner_flow(monkeypatch, other_state):
    load = MagicMock(side_effect=AssertionError("чужой FSM прочитал stats_all"))
    monkeypatch.setattr(handlers, "load_stats_all_snapshot", load)
    state = _State()
    state.state = other_state
    state.data = {"sentinel": "unchanged"}
    message = _message()

    await handlers.cmd_pick(message, state)

    message.answer.assert_awaited_once_with(
        "⚠️ Сначала заверши текущую операцию или отправь /cancel."
    )
    message.reply.assert_not_awaited()
    message.bot.delete_message.assert_not_awaited()
    load.assert_not_called()
    assert state.state == other_state
    assert state.data == {"sentinel": "unchanged"}


@pytest.mark.asyncio
async def test_category_more_and_contrast_are_repeat_free_and_render_safe_html(
    monkeypatch,
):
    _patch_snapshot(monkeypatch, _stats())
    monkeypatch.setattr(smod.random, "choice", lambda items: items[0])
    state = _State()
    message = _message()
    await handlers.cmd_pick(message, state)
    callback = _callback("pick:anime")

    await handlers.pick_menu_cb(callback, state)

    first_media = callback.message.edit_media.await_args.args[0]
    first_text = first_media.caption
    first_kwargs = callback.message.edit_media.await_args.kwargs
    first_markup = first_kwargs["reply_markup"]
    assert "A &lt; B &amp; C" in first_text
    assert "<i>A &amp; C</i>" in first_text
    assert "<b>TV-сериал · 2011</b>" in first_text
    assert "⭐ Оценка Shikimori: 8.5" in first_text
    assert "⏱ 12 эп. · 24 мин." in first_text
    assert "📖 Первоисточник: Манга" in first_text
    assert "🔖 Возрастной рейтинг: R-17" in first_text
    assert "🎞 Студия: Studio &amp; Co" in first_text
    assert "👥 <b>Демография:</b> Сэйнэн" in first_text
    assert "🎭 <b>Жанры:</b> Экшен · Драма" in first_text
    assert "🏷 <b>Темы:</b> Путешествие" in first_text
    assert first_media.media == "https://cdn.example/anime-1.jpg"
    assert first_media.parse_mode == handlers.ParseMode.HTML
    assert first_media.show_caption_above_media is False
    assert handlers.parsed_caption_length(first_text) <= handlers.PHOTO_CAPTION_LIMIT
    assert "link_preview_options" not in first_kwargs
    assert _button_texts(first_markup) == [
        "🔄 Ещё вариант",
        "🎲 Что-нибудь совсем другое",
        "❌ Закрыть",
    ]
    assert first_text.count(handlers.SHIKI_BASE_URL.rstrip("/")) == 1
    assert "shikimori.onehttps://" not in first_text
    assert state.data["pick_shown_ids"] == ["1"]
    assert state.data["pick_anchor"]["id"] == "1"
    assert state.data["pick_menu_kind"] == handlers._PICK_MENU_PHOTO

    callback.data = "pick:more"
    callback.message.edit_media.reset_mock()
    callback.answer.reset_mock()
    await handlers.pick_menu_cb(callback, state)

    second_media = callback.message.edit_media.await_args.args[0]
    assert "Второй вариант" in second_media.caption
    assert second_media.media == "https://cdn.example/anime-2.jpg"
    assert state.data["pick_shown_ids"] == ["1", "2"]

    callback.data = "pick:contrast"
    callback.message.edit_media.reset_mock()
    callback.answer.reset_mock()
    await handlers.pick_menu_cb(callback, state)

    assert state.data["pick_anchor"]["id"] == "1"
    assert state.data["pick_shown_ids"] == ["1"]


def test_pick_renderer_bounds_long_untrusted_title_and_genres():
    stats = storage._empty_stats_all()
    stats["anime"]["titles"] = {
        "1": {
            "title": "<опасно & длинно>" * 500,
            "status": "planned",
            "kind": "tv",
            "url": "https://shikimori.one/animes/1",
            "year": 2026,
            "genres": [f"Жанр & {index}" * 30 for index in range(30)],
        },
    }
    catalog = smod.build_pick_catalog(stats)

    text = handlers._pick_candidate_text(catalog.anime[0], catalog)

    assert handlers.parsed_caption_length(text) < handlers._TELEGRAM_MESSAGE_LIMIT
    assert "&lt;опасно &amp; длинно&gt;" in text
    assert "Жанр &amp; 0" in text
    assert "…" in text


def test_pick_renderer_bounds_escaped_html_and_drops_anomalously_long_url():
    stats = storage._empty_stats_all()
    stats["anime"]["titles"] = {
        "1": {
            "title": "&" * 1000,
            "status": "planned",
            "kind": "tv",
            "url": f"/animes/{'x' * 5000}",
            "year": 2026,
            "genres": ["&" * 1000 for _ in range(20)],
        },
    }
    catalog = smod.build_pick_catalog(stats)

    text = handlers._pick_candidate_text(catalog.anime[0], catalog)

    assert handlers.parsed_caption_length(text) < handlers._TELEGRAM_MESSAGE_LIMIT
    assert '<a href="' not in text
    assert "&amp;…" in text
    assert "&am…" not in text


def test_pick_renderer_applies_one_limit_to_long_facts_and_taxonomy():
    stats = storage._empty_stats_all()
    stats["manga"]["titles"] = {
        "1": {
            "title": "&" * 1000,
            "title_en": "<" * 1000,
            "status": "planned",
            "kind": "manga",
            "url": "/mangas/1",
            "year": 2026,
            "chapters_total": 999,
            "volumes_total": 99,
            "publishers": [f"Издатель & {index}" * 20 for index in range(30)],
            "demographic": [f"Демография & {index}" * 20 for index in range(30)],
            "genres": [f"Жанр & {index}" * 20 for index in range(30)],
            "themes": [f"Тема & {index}" * 20 for index in range(30)],
        },
        "2": {
            "title": "Не определено",
            "status": "planned",
            "kind": "future_kind",
            "url": "/mangas/2",
        },
    }
    catalog = smod.build_pick_catalog(stats)

    text = handlers._pick_candidate_text(
        catalog.manga[0],
        catalog,
        limit=handlers.PHOTO_CAPTION_LIMIT,
    )

    assert handlers.parsed_caption_length(text) <= handlers.PHOTO_CAPTION_LIMIT
    assert "🏷 <b>Темы:</b>" not in text
    assert text.count("<blockquote>") == text.count("</blockquote>") == 1
    assert "Часть запланированных тайтлов (<b>1</b>)" in text


def test_pick_renderer_shows_unresolved_notice_only_for_manga_domain():
    stats = _stats(unresolved=True)
    stats["manga"]["titles"]["12"] = {
        "title": "Манга",
        "status": "planned",
        "kind": "manga",
        "url": "/mangas/12",
        "year": 2021,
        "genres": [],
    }
    stats["manga"]["titles"]["11"] = {
        "title": "Ранобэ",
        "status": "planned",
        "kind": "light_novel",
        "url": "/ranobe/11",
        "year": 2020,
        "genres": [],
    }
    catalog = smod.build_pick_catalog(stats)

    anime_text = handlers._pick_candidate_text(catalog.anime[0], catalog)
    manga_text = handlers._pick_candidate_text(catalog.manga[0], catalog)
    ranobe_text = handlers._pick_candidate_text(catalog.ranobe[0], catalog)

    assert "Как насчёт этого аниме?" in anime_text
    assert "Как насчёт этой манги?" in manga_text
    assert "Как насчёт этого ранобэ?" in ranobe_text
    assert "поэтому я их не предлагаю" not in anime_text
    assert "Часть запланированных тайтлов (<b>1</b>)" in ranobe_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot_state", "payload", "expected"),
    [
        (storage.STATS_ALL_MISSING, storage._empty_stats_all(), "ещё не готов"),
        (
            storage.STATS_ALL_INVALID,
            storage._empty_stats_all(),
            "Не получилось прочитать",
        ),
        (
            storage.STATS_ALL_VALID,
            {"anime": {}, "manga": {"titles": {}}},
            "Не получилось прочитать",
        ),
    ],
)
async def test_missing_malformed_and_structurally_invalid_snapshot_are_distinct_and_safe(
    monkeypatch,
    snapshot_state,
    payload,
    expected,
):
    _patch_snapshot(monkeypatch, payload, state=snapshot_state)
    message = _message()
    state = _State()

    await handlers.cmd_pick(message, state)

    text = message.reply.await_args.args[0]
    assert expected in text
    assert "пока нечего выбирать" not in text
    assert state.state == handlers.PickStates.active.state


@pytest.mark.asyncio
async def test_valid_empty_category_keeps_root_menu_usable(monkeypatch):
    _patch_snapshot(monkeypatch, storage._empty_stats_all())
    state = _State()
    await handlers.cmd_pick(_message(), state)
    callback = _callback("pick:manga")

    await handlers.pick_menu_cb(callback, state)

    text = callback.message.edit_text.await_args.args[0]
    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert "В категории «Манга» пока нечего выбирать" in text
    assert _callback_data(markup) == [
        "pick:anime",
        "pick:manga",
        "pick:ranobe",
        "pick:cancel",
    ]
    assert state.data["pick_category"] is None


@pytest.mark.asyncio
async def test_category_empty_because_of_unknown_kind_discloses_unresolved_count(
    monkeypatch,
):
    _patch_snapshot(monkeypatch, _stats(unresolved=True))
    state = _State()
    await handlers.cmd_pick(_message(), state)
    callback = _callback("pick:manga")

    await handlers.pick_menu_cb(callback, state)

    text = callback.message.edit_text.await_args.args[0]
    assert "В категории «Манга» пока нечего выбирать" in text
    assert "Часть запланированных тайтлов (<b>1</b>)" in text


@pytest.mark.asyncio
async def test_edit_failure_keeps_previous_anchor_and_fsm_state(monkeypatch):
    _patch_snapshot(monkeypatch, _stats())
    state = _State()
    await handlers.cmd_pick(_message(), state)
    before = deepcopy(state.data)
    callback = _callback("pick:anime")
    callback.message.edit_media.side_effect = RuntimeError("cannot edit")

    await handlers.pick_menu_cb(callback, state)

    callback.answer.assert_awaited_once_with(
        "Не удалось обновить меню. Попробуй ещё раз.",
        show_alert=True,
    )
    assert state.data == before


@pytest.mark.asyncio
async def test_rejected_photo_card_retries_as_text_without_web_preview(monkeypatch):
    stats = _stats()
    stats["anime"]["titles"].pop("2")
    _patch_snapshot(monkeypatch, stats)
    state = _State()
    await handlers.cmd_pick(_message(), state)
    callback = _callback("pick:anime")
    callback.message.edit_media.side_effect = handlers.TelegramBadRequest(
        method=MagicMock(),
        message="failed to get HTTP URL content",
    )

    await handlers.pick_menu_cb(callback, state)

    callback.message.edit_media.assert_awaited_once()
    callback.message.edit_text.assert_awaited_once()
    fallback_options = callback.message.edit_text.await_args.kwargs[
        "link_preview_options"
    ]
    assert fallback_options.is_disabled is True
    assert state.data["pick_anchor"]["id"] == "1"
    assert state.data["pick_menu_kind"] == handlers._PICK_MENU_TEXT
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_missing_first_poster_stays_text_without_web_preview(monkeypatch):
    stats = _stats()
    stats["anime"]["titles"].pop("2")
    stats["anime"]["titles"]["1"].pop("poster_url")
    _patch_snapshot(monkeypatch, stats)
    state = _State()
    await handlers.cmd_pick(_message(), state)
    callback = _callback("pick:anime")

    await handlers.pick_menu_cb(callback, state)

    callback.message.edit_media.assert_not_awaited()
    callback.message.edit_text.assert_awaited_once()
    options = callback.message.edit_text.await_args.kwargs["link_preview_options"]
    assert options.is_disabled is True
    assert state.data["pick_menu_message_id"] == 200
    assert state.data["pick_menu_kind"] == handlers._PICK_MENU_TEXT
    assert state.data["pick_anchor"]["id"] == "1"
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_missing_poster_after_photo_replaces_menu_with_text(monkeypatch):
    stats = _stats()
    stats["anime"]["titles"]["2"].pop("poster_url")
    _patch_snapshot(monkeypatch, stats)
    monkeypatch.setattr(smod.random, "choice", lambda items: items[0])
    state = _State()
    await handlers.cmd_pick(_message(), state)
    callback = _callback("pick:anime")

    await handlers.pick_menu_cb(callback, state)
    callback.data = "pick:more"
    callback.answer.reset_mock()
    await handlers.pick_menu_cb(callback, state)

    callback.message.answer.assert_awaited_once()
    text = callback.message.answer.await_args.args[0]
    options = callback.message.answer.await_args.kwargs["link_preview_options"]
    reply = callback.message.answer.await_args.kwargs["reply_parameters"]
    assert "Второй вариант" in text
    assert options.is_disabled is True
    assert reply.message_id == 100
    callback.message.delete.assert_awaited_once_with()
    assert state.data["pick_menu_message_id"] == 201
    assert state.data["pick_menu_kind"] == handlers._PICK_MENU_TEXT
    assert state.data["pick_anchor"]["id"] == "2"
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_second_command_invalidates_first_and_stale_callbacks_do_not_mutate(
    monkeypatch,
):
    _patch_snapshot(monkeypatch, _stats())
    state = _State()
    first = _message(message_id=100)
    await handlers.cmd_pick(first, state)
    second = _message(message_id=101)
    second.reply.return_value = SimpleNamespace(message_id=201)

    await handlers.cmd_pick(second, state)

    second.bot.delete_message.assert_any_await(55, 200)
    second.bot.delete_message.assert_any_await(55, 100)
    assert state.data["pick_menu_message_id"] == 201
    before = deepcopy(state.data)
    stale = _callback("pick:anime", message_id=200)
    await handlers.pick_menu_cb(stale, state)

    stale.answer.assert_awaited_once_with("Это меню уже неактивно.", show_alert=True)
    stale.message.edit_text.assert_not_awaited()
    assert state.data == before


@pytest.mark.asyncio
async def test_unknown_missing_message_and_close_paths_are_safe(monkeypatch):
    _patch_snapshot(monkeypatch, _stats())
    state = _State()
    await handlers.cmd_pick(_message(), state)

    unknown = _callback("pick:future")
    await handlers.pick_menu_cb(unknown, state)
    unknown.answer.assert_awaited_once_with("Неизвестное действие.", show_alert=True)
    assert state.state == handlers.PickStates.active.state

    missing = _callback("pick:close", with_message=False)
    await handlers.pick_menu_cb(missing, state)
    missing.answer.assert_awaited_once_with(
        "Меню устарело. Отправь /pick ещё раз.",
        show_alert=True,
    )
    assert state.state == handlers.PickStates.active.state

    cleanup = AsyncMock()
    monkeypatch.setattr(handlers, "_cleanup_inline_menu", cleanup)
    close = _callback("pick:close")
    await handlers.pick_menu_cb(close, state)
    close.answer.assert_awaited_once_with()
    cleanup.assert_awaited_once_with(close.message)
    assert state.state is None


@pytest.mark.asyncio
async def test_cancel_clears_pick_fsm_and_tolerates_cleanup_failures(monkeypatch):
    _patch_snapshot(monkeypatch, _stats())
    state = _State()
    command = _message()
    await handlers.cmd_pick(command, state)
    cancel = _message(message_id=300)
    cancel.bot = command.bot
    cancel.bot.delete_message.side_effect = RuntimeError("Telegram cleanup failed")

    await handlers.cmd_cancel(cancel, state)

    assert state.state is None
    cancel.answer.assert_awaited_once_with("❌ Отменено.")
    assert cancel.bot.delete_message.await_count == 3


@pytest.mark.asyncio
async def test_every_pick_path_is_isolated_from_network_entitlement_and_inline_state(
    monkeypatch,
):
    _patch_snapshot(monkeypatch, _stats())

    def forbidden(*args, **kwargs):
        raise AssertionError("/pick вызвал запрещённую внешнюю границу")

    async def forbidden_async(*args, **kwargs):
        raise AssertionError("/pick вызвал запрещённую внешнюю границу")

    for name in (
        "fetch_current_rates",
        "fetch_favourites",
        "fetch_history",
        "get_media_info",
        "sync_stats_all",
        "refresh_update_state",
    ):
        monkeypatch.setattr(handlers, name, forbidden_async)
    for name in ("load_subscribers", "inline_access_status", "parse_inline_query"):
        monkeypatch.setattr(handlers, name, forbidden)
    monkeypatch.setattr(handlers._inline_search_service, "debounce", forbidden_async)
    monkeypatch.setattr(handlers._inline_search_service, "get_page", forbidden_async)

    state = _State()
    await handlers.cmd_pick(_message(), state)
    callback = _callback("pick:anime")
    await handlers.pick_menu_cb(callback, state)
    callback.data = "pick:more"
    await handlers.pick_menu_cb(callback, state)
    callback.data = "pick:contrast"
    await handlers.pick_menu_cb(callback, state)
    callback.data = "pick:close"
    await handlers.pick_menu_cb(callback, state)

    assert state.state is None
