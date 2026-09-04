# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Владелецкая команда /version."""

from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

import handlers
from runtime_status import RuntimeSnapshot


@pytest.fixture(autouse=True)
def info_preview(monkeypatch):
    preview = MagicMock(name="info_preview")
    monkeypatch.setattr(handlers, "_info_preview_file_id", None)
    monkeypatch.setattr(handlers, "_load_info_preview", lambda: preview)
    return preview


@pytest.mark.asyncio
async def test_version_rejects_non_owner():
    message = AsyncMock()
    message.from_user = MagicMock(id=handlers.OWNER_ID + 1)
    await handlers.cmd_version(message)
    assert "только для владельца" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_version_refreshes_and_renders(monkeypatch):
    state = {
        "latest_main_version": "v1.3.0",
        "latest_version": "v1.2.0",
        "release_url": "https://release",
    }
    refresh = AsyncMock(return_value=state)
    monkeypatch.setattr(handlers, "refresh_update_state", refresh)
    monkeypatch.setattr(
        handlers,
        "load_subscription_backup_state",
        lambda: {"last_backup_at": None},
    )
    monkeypatch.setattr(
        handlers,
        "get_runtime_snapshot",
        lambda: RuntimeSnapshot(60, None, True),
    )
    message = AsyncMock()
    message.from_user = MagicMock(id=handlers.OWNER_ID)

    await handlers.cmd_version(message)

    refresh.assert_awaited_once_with(force=True)
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert "<b>ShikiUpdatesBot</b>" in text
    assert "Версия этого бота:" in text
    assert "Актуальная версия проекта: <code>v1.3.0</code>" in text
    assert "Последняя версия для Windows: <code>v1.2.0</code>" in text
    assert "v1.2.0" in text
    assert message.answer.await_args.kwargs["parse_mode"] == ParseMode.HTML
    assert keyboard.inline_keyboard[0][-1].url == "https://release"
    assert keyboard.inline_keyboard[1][0].callback_data == "version:refresh"


@pytest.mark.asyncio
async def test_info_is_public_cache_only_and_uses_html(monkeypatch, info_preview):
    state = {
        "latest_main_version": "v1.3.0",
        "latest_version": "v1.2.0",
        "release_url": "https://release",
    }
    refresh = AsyncMock(side_effect=AssertionError("/info вызвал GitHub refresh"))
    main_fetch = AsyncMock(side_effect=AssertionError("/info вызвал main fetch"))
    release_fetch = AsyncMock(side_effect=AssertionError("/info вызвал release fetch"))
    history_fetch = AsyncMock(side_effect=AssertionError("/info вызвал Shikimori fetch"))
    favourites_fetch = AsyncMock(side_effect=AssertionError("/info вызвал Shikimori fetch"))
    rates_fetch = AsyncMock(side_effect=AssertionError("/info вызвал Shikimori fetch"))
    monkeypatch.setattr(handlers, "refresh_update_state", refresh)
    monkeypatch.setattr("updates.fetch_main_version", main_fetch)
    monkeypatch.setattr("updates.fetch_latest_release", release_fetch)
    monkeypatch.setattr(handlers, "fetch_history", history_fetch)
    monkeypatch.setattr(handlers, "fetch_favourites", favourites_fetch)
    monkeypatch.setattr(handlers, "fetch_current_rates", rates_fetch)
    monkeypatch.setattr(handlers, "load_update_state", lambda: state.copy())
    monkeypatch.setattr(
        handlers,
        "load_subscription_backup_state",
        lambda: {"last_backup_at": 1_750_000_000},
    )
    monkeypatch.setattr(
        handlers,
        "get_runtime_snapshot",
        lambda: RuntimeSnapshot(3661, 1_750_000_100, False),
    )
    message = AsyncMock()
    message.from_user = MagicMock(id=handlers.OWNER_ID + 1)

    await handlers.cmd_info(message)

    refresh.assert_not_awaited()
    main_fetch.assert_not_awaited()
    release_fetch.assert_not_awaited()
    history_fetch.assert_not_awaited()
    favourites_fetch.assert_not_awaited()
    rates_fetch.assert_not_awaited()
    message.answer.assert_not_awaited()
    message.answer_photo.assert_awaited_once()
    assert message.answer_photo.await_args.args[0] is info_preview
    text = message.answer_photo.await_args.kwargs["caption"]
    assert "Работает: 01:01:01" in text
    assert "Проверка новых событий: остановлена" in text
    assert "Последняя автоматическая резервная копия:" in text
    assert "GNU General Public License версии 3 или более поздней" in text
    assert len(text) <= 1024
    assert message.answer_photo.await_args.kwargs["parse_mode"] == ParseMode.HTML
    assert "show_caption_above_media" not in message.answer_photo.await_args.kwargs
    keyboard = message.answer_photo.await_args.kwargs["reply_markup"]
    assert len(keyboard.inline_keyboard) == 1


@pytest.mark.asyncio
async def test_info_reuses_telegram_file_id_after_first_upload(
    monkeypatch,
    info_preview,
):
    load_preview = MagicMock(return_value=info_preview)
    monkeypatch.setattr(handlers, "_load_info_preview", load_preview)
    monkeypatch.setattr(handlers, "load_update_state", lambda: {})
    monkeypatch.setattr(handlers, "load_subscription_backup_state", lambda: {})
    first = AsyncMock()
    first.from_user = MagicMock(id=handlers.OWNER_ID + 1)
    first.answer_photo.return_value = MagicMock(
        photo=[MagicMock(file_id="telegram-info-preview")],
    )
    second = AsyncMock()
    second.from_user = MagicMock(id=handlers.OWNER_ID + 1)

    await handlers.cmd_info(first)
    await handlers.cmd_info(second)

    assert first.answer_photo.await_args.args[0] is info_preview
    assert second.answer_photo.await_args.args[0] == "telegram-info-preview"
    load_preview.assert_called_once_with()
    assert handlers._info_preview_file_id == "telegram-info-preview"


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_preview", [None, "expired-file-id"])
async def test_info_falls_back_to_text_when_photo_is_rejected(
    monkeypatch,
    info_preview,
    cached_preview,
):
    monkeypatch.setattr(handlers, "_info_preview_file_id", cached_preview)
    monkeypatch.setattr(handlers, "load_update_state", lambda: {})
    monkeypatch.setattr(handlers, "load_subscription_backup_state", lambda: {})
    message = AsyncMock()
    message.from_user = MagicMock(id=handlers.OWNER_ID + 1)
    message.answer_photo.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="wrong file identifier",
    )

    await handlers.cmd_info(message)

    preview = cached_preview or info_preview
    message.answer_photo.assert_awaited_once()
    photo_call = message.answer_photo.await_args
    assert photo_call.args[0] is preview
    message.answer.assert_awaited_once()
    text_call = message.answer.await_args
    assert text_call.args[0] == photo_call.kwargs["caption"]
    assert text_call.kwargs["parse_mode"] == ParseMode.HTML
    assert text_call.kwargs["reply_markup"] is photo_call.kwargs["reply_markup"]
    assert handlers._info_preview_file_id is None


@pytest.mark.parametrize("photos", [None, []])
def test_info_preview_file_id_ignores_missing_or_empty_photo(monkeypatch, photos):
    monkeypatch.setattr(handlers, "_info_preview_file_id", "existing-file-id")
    message = MagicMock(photo=photos)

    handlers._remember_info_preview_file_id(message)

    assert handlers._info_preview_file_id == "existing-file-id"


@pytest.mark.asyncio
async def test_info_degrades_without_exposing_local_error(monkeypatch):
    monkeypatch.setattr(handlers, "load_update_state", lambda: {})
    monkeypatch.setattr(
        handlers,
        "load_subscription_backup_state",
        MagicMock(side_effect=RuntimeError("C:/secret/private.json")),
    )
    monkeypatch.setattr(handlers, "_load_info_preview", lambda: None)
    message = AsyncMock()
    message.from_user = MagicMock(id=handlers.OWNER_ID + 1)

    await handlers.cmd_info(message)

    text = message.answer.await_args.args[0]
    assert "Последняя автоматическая резервная копия: неизвестно" in text
    assert "C:/secret" not in text
    assert "RuntimeError" not in text


@pytest.mark.asyncio
async def test_info_owner_gets_guarded_refresh_button_without_fetch(monkeypatch):
    refresh = AsyncMock()
    monkeypatch.setattr(handlers, "refresh_update_state", refresh)
    monkeypatch.setattr(
        handlers,
        "load_update_state",
        lambda: {"release_url": "https://release"},
    )
    monkeypatch.setattr(handlers, "load_subscription_backup_state", lambda: {})
    message = AsyncMock()
    message.from_user = MagicMock(id=handlers.OWNER_ID)

    await handlers.cmd_info(message)

    refresh.assert_not_awaited()
    keyboard = message.answer_photo.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[1][0].callback_data == "version:refresh"


@pytest.mark.asyncio
async def test_info_reads_last_automatic_backup_without_writing(monkeypatch):
    current = {"last_backup_at": 1_750_000_000, "events": []}
    load = MagicMock(return_value=current)
    save = MagicMock()
    monkeypatch.setattr(handlers, "load_update_state", lambda: {})
    monkeypatch.setattr(handlers, "load_subscription_backup_state", load)
    monkeypatch.setattr(handlers, "save_subscriber_state", save)
    message = AsyncMock()
    message.from_user = MagicMock(id=handlers.OWNER_ID + 1)

    await handlers.cmd_info(message)

    load.assert_called_once_with()
    assert current == {"last_backup_at": 1_750_000_000, "events": []}
    assert "15.06.2025, 15:06 UTC" in message.answer_photo.await_args.kwargs["caption"]
    save.assert_not_called()


@pytest.mark.asyncio
async def test_forged_version_refresh_callback_is_rejected(monkeypatch):
    refresh = AsyncMock()
    monkeypatch.setattr(handlers, "refresh_update_state", refresh)
    callback = AsyncMock()
    callback.from_user = MagicMock(id=handlers.OWNER_ID + 1)
    callback.message = AsyncMock()
    callback.message.photo = [MagicMock()]

    await handlers.version_refresh_cb(callback)

    refresh.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    callback.message.edit_caption.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "Только для владельца бота.",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_owner_version_refresh_callback_updates_html(monkeypatch):
    state = {"latest_main_version": "v1.3.0", "latest_version": "v1.2.0"}
    refresh = AsyncMock(return_value=state)
    monkeypatch.setattr(handlers, "refresh_update_state", refresh)
    monkeypatch.setattr(
        handlers,
        "load_subscription_backup_state",
        lambda: {"last_backup_at": None},
    )
    callback = AsyncMock()
    callback.from_user = MagicMock(id=handlers.OWNER_ID)
    callback.message = AsyncMock()
    callback.message.photo = None

    await handlers.version_refresh_cb(callback)

    refresh.assert_awaited_once_with(force=True)
    kwargs = callback.message.edit_text.await_args.kwargs
    assert kwargs["parse_mode"] == ParseMode.HTML
    text = callback.message.edit_text.await_args.args[0]
    assert "Актуальная версия проекта: <code>v1.3.0</code>" in text
    callback.answer.assert_awaited_once_with("Обновляю сведения…")


@pytest.mark.asyncio
async def test_owner_refresh_edits_photo_caption_with_html(monkeypatch):
    state = {"latest_main_version": "v1.3.0", "latest_version": "v1.2.0"}
    monkeypatch.setattr(
        handlers,
        "refresh_update_state",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr(handlers, "load_subscription_backup_state", lambda: {})
    callback = AsyncMock()
    callback.from_user = MagicMock(id=handlers.OWNER_ID)
    callback.message = AsyncMock()
    callback.message.photo = [MagicMock()]

    await handlers.version_refresh_cb(callback)

    callback.message.edit_text.assert_not_awaited()
    kwargs = callback.message.edit_caption.await_args.kwargs
    assert kwargs["parse_mode"] == ParseMode.HTML
    assert "Актуальная версия проекта: <code>v1.3.0</code>" in kwargs["caption"]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_photo", [False, True])
async def test_owner_refresh_handles_non_editable_message(monkeypatch, has_photo):
    monkeypatch.setattr(
        handlers,
        "refresh_update_state",
        AsyncMock(return_value={"latest_main_version": "v1.3.0"}),
    )
    monkeypatch.setattr(handlers, "load_subscription_backup_state", lambda: {})
    callback = AsyncMock()
    callback.from_user = MagicMock(id=handlers.OWNER_ID)
    callback.message = AsyncMock()
    callback.message.photo = [MagicMock()] if has_photo else None
    edit = (
        callback.message.edit_caption
        if has_photo
        else callback.message.edit_text
    )
    edit.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="message is not modified",
    )

    await handlers.version_refresh_cb(callback)

    edit.assert_awaited_once()
    callback.answer.assert_awaited_once_with("Обновляю сведения…")
