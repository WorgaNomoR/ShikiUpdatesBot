# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Регистрация только уже разрешённых и сопоставленных Telegram-событий."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import (
    Dispatcher,
    F,
)

import access_control
import storage
import user_registry


@pytest.fixture
def registry_env(monkeypatch, tmp_path):
    """Изолировать все состояния, с которыми не должен смешиваться реестр."""
    monkeypatch.setattr(storage, "KNOWN_USERS_FILE", tmp_path / "known_users.json")
    monkeypatch.setattr(storage, "USER_ALERTS_FILE", tmp_path / "user_alerts.json")
    monkeypatch.setattr(storage, "SUBS_FILE", tmp_path / "subscribers.json")
    monkeypatch.setattr(storage, "BLOCKED_USERS_FILE", tmp_path / "blocked_users.json")
    monkeypatch.setattr(storage, "OWNER_ID", 999)
    monkeypatch.setattr(user_registry, "OWNER_ID", 999)
    monkeypatch.setattr(access_control, "OWNER_ID", 999)
    return tmp_path


def _event(
    user_id=10,
    *,
    name="<Neo & Trinity>",
    username="the_one",
    with_message=True,
):
    return SimpleNamespace(
        from_user=(
            SimpleNamespace(id=user_id, full_name=name, username=username)
            if user_id is not None
            else None
        ),
        message=SimpleNamespace() if with_message else None,
    )


@pytest.mark.asyncio
async def test_first_allowed_message_registers_and_sends_one_safe_alert(registry_env):
    bot = SimpleNamespace(send_message=AsyncMock())
    handler = AsyncMock(return_value="handled")

    result = await user_registry.UserRegistryMiddleware()(
        handler,
        _event(),
        {"bot": bot},
    )

    assert result == "handled"
    handler.assert_awaited_once()
    saved = storage.get_known_user(10)
    assert saved is not None
    assert saved.display_name == "<Neo & Trinity>"
    assert saved.username == "the_one"
    assert saved.first_seen_at.endswith("Z")
    bot.send_message.assert_awaited_once()
    owner_id, text = bot.send_message.await_args.args
    assert owner_id == 999
    assert "&lt;Neo &amp; Trinity&gt;" in text
    assert "@the_one" in text
    assert "/useralerts off" in text
    assert "/useralerts on" in text


@pytest.mark.asyncio
async def test_supported_callback_without_message_uses_same_contract(registry_env):
    bot = SimpleNamespace(send_message=AsyncMock())
    handler = AsyncMock()

    await user_registry.UserRegistryMiddleware()(
        handler,
        _event(with_message=False),
        {"bot": bot},
    )

    assert storage.known_user_count() == 1
    handler.assert_awaited_once()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_name", ["message", "callback_query"])
async def test_unmatched_event_does_not_enter_inner_registry_middleware(
    registry_env,
    observer_name,
):
    dispatcher = Dispatcher()
    observer = getattr(dispatcher, observer_name)
    observer.middleware(user_registry.UserRegistryMiddleware())
    handled = []

    async def project_handler(_event):
        handled.append(True)

    if observer_name == "message":
        observer.register(project_handler, F.text == "supported")
        event = _event()
        event.text = "ignored"
    else:
        observer.register(project_handler, F.data == "supported")
        event = _event(with_message=False)
        event.data = "unknown"
    bot = SimpleNamespace(send_message=AsyncMock())

    await observer.trigger(event, bot=bot)

    assert storage.load_known_users() == {}
    assert handled == []
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_control_stops_blocked_user_before_registration(
    registry_env,
    monkeypatch,
):
    monkeypatch.setattr(access_control, "is_user_blocked", lambda _user_id: True)
    bot = SimpleNamespace(send_message=AsyncMock())
    message = _event()
    message.answer = AsyncMock()
    update = SimpleNamespace(message=message, callback_query=None, inline_query=None)
    project_handler = AsyncMock()
    registry = user_registry.UserRegistryMiddleware()

    async def after_access(_update, data):
        return await registry(project_handler, message, data)

    await access_control.AccessControlMiddleware()(after_access, update, {"bot": bot})

    assert storage.load_known_users() == {}
    project_handler.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, 0, -1, True, "10"])
async def test_missing_or_invalid_sender_runs_handler_without_registration(
    registry_env,
    user_id,
):
    bot = SimpleNamespace(send_message=AsyncMock())
    handler = AsyncMock(return_value="handled")

    assert await user_registry.UserRegistryMiddleware()(
        handler,
        _event(user_id),
        {"bot": bot},
    ) == "handled"

    assert storage.load_known_users() == {}
    handler.assert_awaited_once()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_runs_handler_without_registration_or_alert(registry_env):
    bot = SimpleNamespace(send_message=AsyncMock())
    handler = AsyncMock()

    await user_registry.UserRegistryMiddleware()(
        handler,
        _event(999),
        {"bot": bot},
    )

    assert storage.load_known_users() == {}
    handler.assert_awaited_once()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_events_create_one_record_and_one_alert(registry_env):
    bot = SimpleNamespace(send_message=AsyncMock())
    handlers = [AsyncMock() for _ in range(10)]
    middleware = user_registry.UserRegistryMiddleware()

    await asyncio.gather(
        *(
            middleware(handler, _event(), {"bot": bot})
            for handler in handlers
        )
    )

    assert storage.known_user_count() == 1
    assert all(handler.await_count == 1 for handler in handlers)
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_alerts_register_without_notification(registry_env):
    await storage.set_user_alerts_enabled(False)
    bot = SimpleNamespace(send_message=AsyncMock())

    await user_registry.UserRegistryMiddleware()(
        AsyncMock(),
        _event(),
        {"bot": bot},
    )

    assert storage.known_user_count() == 1
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_failure_does_not_break_handler_or_roll_back_registration(
    registry_env,
):
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("offline")))
    handler = AsyncMock(return_value="handled")

    result = await user_registry.UserRegistryMiddleware()(
        handler,
        _event(),
        {"bot": bot},
    )

    assert result == "handled"
    handler.assert_awaited_once()
    assert storage.known_user_count() == 1
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_registry_does_not_overwrite_or_block_handler(registry_env):
    path = registry_env / "known_users.json"
    original = "{broken"
    path.write_text(original, encoding="utf-8")
    bot = SimpleNamespace(send_message=AsyncMock())
    handler = AsyncMock(return_value="handled")

    result = await user_registry.UserRegistryMiddleware()(
        handler,
        _event(),
        {"bot": bot},
    )

    assert result == "handled"
    assert path.read_text(encoding="utf-8") == original
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_registration_does_not_change_subscription_or_inline_entitlement(
    registry_env,
    monkeypatch,
):
    storage.save_subscribers({20: "Morpheus"})
    monkeypatch.setattr(access_control, "is_user_blocked", lambda _user_id: False)
    before = access_control.inline_access_status(10)

    await user_registry.UserRegistryMiddleware()(
        AsyncMock(),
        _event(10),
        {"bot": SimpleNamespace(send_message=AsyncMock())},
    )

    assert storage.load_subscribers() == {20: "Morpheus"}
    assert storage.get_known_user(20) is None
    assert before == access_control.INLINE_ACCESS_UNSUBSCRIBED
    assert access_control.inline_access_status(10) == before
