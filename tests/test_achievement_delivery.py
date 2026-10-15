# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Capture-before-send, попытки, restart/restore и terminal cleanup."""

import asyncio
import logging
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import (
    EditMessageText,
    SendMessage,
)

import achievement_delivery
import storage
from achievement_state import AchievementStateError
from notification_outbox import (
    LIFETIME,
    possible_delivery,
)


@pytest.fixture
def achievement_env(backup_env, monkeypatch, achievement_row_factory):
    clock = [1000.0]
    monkeypatch.setattr("achievement_delivery.time.time", lambda: clock[0])
    monkeypatch.setattr("achievement_delivery.asyncio.sleep", AsyncMock())
    source = AsyncMock(return_value={"user_id": 42, "rows": [achievement_row_factory()]})
    monkeypatch.setattr("achievement_delivery.fetch_achievements", source)
    return clock, source


@pytest.mark.asyncio
@pytest.mark.parametrize("labels", [{}, {("new_family", 3): ("Достижение из источника", "franchise")}])
async def test_optional_labels_are_frozen_only_for_new_achievements(
    achievement_env, achievement_row_factory, monkeypatch, labels,
):
    _, source = achievement_env
    metadata = AsyncMock(return_value=labels)
    monkeypatch.setattr("achievement_delivery.fetch_achievement_labels", metadata)
    await achievement_delivery.synchronize_achievements()
    metadata.assert_not_awaited()
    await achievement_delivery.synchronize_achievements()
    metadata.assert_not_awaited()
    source.return_value = {"user_id": 42, "rows": [achievement_row_factory("new_family", level=3)]}
    assert await achievement_delivery.synchronize_achievements()
    metadata.assert_awaited_once()
    frozen = deepcopy(storage.load_achievement_state()["pending"])
    expected = "Достижение из источника" if labels else "Достижение «new_family»"
    assert expected in frozen[-1]["parts"][0]["text"]
    assert storage.load_achievement_state()["earned"] == {"animelist": 1, "new_family": 3}
    metadata.return_value = {("new_family", 3): ("Изменившаяся подпись", "franchise")}
    await achievement_delivery.synchronize_achievements()
    assert storage.load_achievement_state()["pending"] == frozen
    assert metadata.await_count == 1


@pytest.mark.asyncio
async def test_restore_during_optional_metadata_lookup_rejects_obsolete_capture(
    achievement_env, achievement_row_factory, monkeypatch,
):
    _, source = achievement_env
    await achievement_delivery.synchronize_achievements()
    before = storage.load_achievement_state()
    source.return_value = {"user_id": 42, "rows": [achievement_row_factory(level=2)]}

    async def metadata(session):
        storage.mark_restorable_state_restored()
        return {("animelist", 2): ("Поздравление", "common")}

    monkeypatch.setattr("achievement_delivery.fetch_achievement_labels", metadata)
    assert await achievement_delivery.synchronize_achievements() is False
    assert storage.load_achievement_state() == before


@pytest.mark.asyncio
async def test_owner_outside_subscription_receives_frozen_baseline_once(achievement_env):
    _, source = achievement_env
    storage.save_subscribers({111: "subscriber"})
    bot = AsyncMock()
    assert await achievement_delivery.synchronize_achievements()
    frozen = storage.load_achievement_state()

    async def send(**kwargs):
        captured = storage.load_achievement_state()
        assert captured["pending"][0]["parts"][0]["delivery"]["attempts"][-1]["outcome"] == "uncertain"
        assert kwargs["text"] == frozen["pending"][0]["parts"][0]["text"]
        assert kwargs["chat_id"] == 999
        return object()

    bot.send_message.side_effect = send
    assert await achievement_delivery.dispatch_achievements(bot) is False
    assert storage.load_achievement_state()["pending"] == []
    assert storage.load_achievement_state()["earned"] == {"animelist": 1}
    assert storage.load_subscribers() == {111: "subscriber"}
    # Новое чтение диска после restart не создаёт повторную baseline.
    await achievement_delivery.synchronize_achievements()
    assert await achievement_delivery.dispatch_achievements(bot) is False
    assert bot.send_message.await_count == 1
    assert source.await_count == 2


@pytest.mark.asyncio
async def test_pending_survives_failure_and_source_refresh(achievement_env, achievement_row_factory):
    _, source = achievement_env
    await achievement_delivery.synchronize_achievements()
    original = deepcopy(storage.load_achievement_state()["pending"])
    source.return_value = None
    assert await achievement_delivery.synchronize_achievements() is False
    assert storage.load_achievement_state()["pending"] == original
    source.return_value = {"user_id": 42, "rows": [achievement_row_factory(level=4)]}
    await achievement_delivery.synchronize_achievements()
    state = storage.load_achievement_state()
    assert state["pending"][:1] == original
    assert state["pending"][1]["awards"] == {"animelist": 4}
    source.return_value = None
    bot = AsyncMock()
    assert await achievement_delivery.dispatch_achievements(bot) is False
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_multipart_restart_resumes_frozen_suffix(achievement_env, achievement_row_factory):
    clock, source = achievement_env
    source.return_value = {"user_id": 42, "rows": []}
    await achievement_delivery.synchronize_achievements()
    await achievement_delivery.dispatch_achievements(AsyncMock())
    source.return_value = {"user_id": 42, "rows": [achievement_row_factory("😀<&>" * 1500)]}
    await achievement_delivery.synchronize_achievements()
    frozen = [part["text"] for part in storage.load_achievement_state()["pending"][0]["parts"]]
    assert len(frozen) > 1
    bot = AsyncMock()
    bot.send_message.side_effect = [object(), TimeoutError()]
    assert await achievement_delivery.dispatch_achievements(bot) is True
    assert storage.load_achievement_state()["pending"][0]["parts"][0]["index"] == 1
    source.return_value = None
    await achievement_delivery.synchronize_achievements()
    clock[0] = 1060
    bot.send_message.side_effect = None
    assert await achievement_delivery.dispatch_achievements(bot) is False
    texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert texts == [frozen[0], frozen[1], *frozen[1:]]
    assert storage.load_achievement_state()["earned"] == {"😀<&>" * 1500: 1}


@pytest.mark.asyncio
async def test_refresh_during_send_merges_ack_without_losing_new_capture(achievement_env, achievement_row_factory):
    _, source = achievement_env
    await achievement_delivery.synchronize_achievements()
    bot = AsyncMock()
    calls = [0]

    async def send(**kwargs):
        calls[0] += 1
        if calls[0] == 1:
            source.return_value = {"user_id": 42, "rows": [achievement_row_factory(level=2)]}
            assert await achievement_delivery.synchronize_achievements()
        return object()

    bot.send_message.side_effect = send
    assert await achievement_delivery.dispatch_achievements(bot) is False
    assert bot.send_message.await_count == 2
    assert storage.load_achievement_state()["earned"] == {"animelist": 2}
    assert storage.load_achievement_state()["pending"] == []


@pytest.mark.asyncio
async def test_uncertain_then_success_has_duplicate_evidence(achievement_env, caplog):
    caplog.set_level("INFO", logger="shikiupdatesbot")
    clock, _ = achievement_env
    await achievement_delivery.synchronize_achievements()
    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError()
    assert await achievement_delivery.dispatch_achievements(bot) is True
    first = storage.load_achievement_state()["pending"][0]["parts"][0]["delivery"]
    assert first["attempts"] == [{"at": 1000.0, "outcome": "uncertain"}]
    assert possible_delivery(first)
    assert first["next_attempt_at"] == 1060
    assert await achievement_delivery.dispatch_achievements(bot) is True
    assert bot.send_message.await_count == 1
    clock[0] = 1060
    bot.send_message.side_effect = None
    assert await achievement_delivery.dispatch_achievements(bot) is False
    assert "Возможно повторное выполнение" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["forbidden", "rate_limit"])
async def test_known_failure_policy(achievement_env, failure):
    clock, _ = achievement_env
    await achievement_delivery.synchronize_achievements()
    method = SendMessage(chat_id=999, text="test")
    bot = AsyncMock()
    bot.send_message.side_effect = (
        TelegramForbiddenError(method=method, message="Forbidden") if failure == "forbidden"
        else TelegramRetryAfter(method=method, message="Flood", retry_after=100000)
    )
    pending = await achievement_delivery.dispatch_achievements(bot)
    state = storage.load_achievement_state()
    assert pending == (failure == "rate_limit")
    assert state["earned"] == {"animelist": 1}
    if pending:
        delivery = state["pending"][0]["parts"][0]["delivery"]
        assert delivery["attempts"][-1]["outcome"] == "confirmed_rejection"
        assert not possible_delivery(delivery)
        assert delivery["next_attempt_at"] == clock[0] + 21600


@pytest.mark.asyncio
async def test_cancellation_leaves_restart_marker(achievement_env):
    await achievement_delivery.synchronize_achievements()
    bot = AsyncMock()
    bot.send_message.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await achievement_delivery.dispatch_achievements(bot)
    assert storage.load_achievement_state()["pending"][0]["parts"][0]["delivery"]["attempts"][0]["outcome"] == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["capture", "marker", "ack"])
@pytest.mark.parametrize("startup", [False, True])
async def test_write_failure_never_allows_uncaptured_send(achievement_env, monkeypatch, phase, startup):
    clock, _ = achievement_env
    if startup:
        achievement_delivery.register_achievement_startup(321, "🟢 Бот запущен")
    if phase != "capture":
        await achievement_delivery.synchronize_achievements()
    original = storage.ACHIEVEMENTS_FILE.read_bytes() if storage.ACHIEVEMENTS_FILE.exists() else None
    real_write = storage._atomic_write
    writes = [0]

    def fail(path, payload):
        writes[0] += 1
        if phase != "ack" or writes[0] == 2:
            raise OSError("disk failure")
        real_write(path, payload)

    bot = AsyncMock()
    operation = bot.edit_message_text if startup else bot.send_message
    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", fail)
        with pytest.raises(AchievementStateError, match="write"):
            if phase == "capture":
                await achievement_delivery.synchronize_achievements()
            else:
                await achievement_delivery.dispatch_achievements(bot)
    if phase == "capture":
        assert not storage.ACHIEVEMENTS_FILE.exists()
    elif phase == "marker":
        assert storage.ACHIEVEMENTS_FILE.read_bytes() == original
    else:
        assert storage.load_achievement_state()["pending"][0]["parts"][0]["delivery"]["attempts"][-1]["outcome"] == "uncertain"
        assert operation.await_count == 1
        clock[0] = 1060
        await achievement_delivery.dispatch_achievements(bot)
        assert operation.await_count == 2
    if phase != "ack":
        operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_capacity_failure_preserves_snapshot_and_checkpoint(achievement_env, achievement_row_factory, monkeypatch):
    _, source = achievement_env
    await achievement_delivery.synchronize_achievements()
    original = storage.ACHIEVEMENTS_FILE.read_bytes()
    source.return_value = {"user_id": 42, "rows": [achievement_row_factory(level=2)]}
    monkeypatch.setattr("achievement_state.ACHIEVEMENTS_MAX_BYTES", len(original) + 500)
    with pytest.raises(AchievementStateError, match="capacity"):
        await achievement_delivery.synchronize_achievements()
    assert storage.ACHIEVEMENTS_FILE.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b'{', b'{"version":99}', b'\xff'])
async def test_corruption_preserved_and_no_http_or_send(achievement_env, raw):
    _, source = achievement_env
    storage.ACHIEVEMENTS_FILE.write_bytes(raw)
    with pytest.raises(AchievementStateError):
        await achievement_delivery.synchronize_achievements()
    bot = AsyncMock()
    with pytest.raises(AchievementStateError):
        await achievement_delivery.dispatch_achievements(bot)
    source.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert storage.ACHIEVEMENTS_FILE.read_bytes() == raw


@pytest.mark.asyncio
async def test_restore_during_source_fetch_rejects_capture(achievement_env):
    _, source = achievement_env
    payload = source.return_value

    async def restored(_session):
        storage.mark_restorable_state_restored()
        return payload

    source.side_effect = restored
    assert await achievement_delivery.synchronize_achievements() is False
    assert not storage.ACHIEVEMENTS_FILE.exists()


@pytest.mark.asyncio
async def test_numeric_profile_conflict_blocks_old_work_even_after_source_failure(achievement_env):
    _, source = achievement_env
    await achievement_delivery.synchronize_achievements()
    original = storage.ACHIEVEMENTS_FILE.read_bytes()
    source.return_value = {"user_id": 43, "rows": []}
    with pytest.raises(AchievementStateError, match="profile"):
        await achievement_delivery.synchronize_achievements()
    source.return_value = None
    assert await achievement_delivery.synchronize_achievements() is False
    bot = AsyncMock()
    with pytest.raises(AchievementStateError, match="profile"):
        await achievement_delivery.dispatch_achievements(bot)
    bot.send_message.assert_not_awaited()
    assert storage.ACHIEVEMENTS_FILE.read_bytes() == original


@pytest.mark.asyncio
async def test_restore_during_send_rejects_obsolete_ack(achievement_env):
    await achievement_delivery.synchronize_achievements()
    bot = AsyncMock()

    async def restored(**kwargs):
        storage.mark_restorable_state_restored()
        return object()

    bot.send_message.side_effect = restored
    assert await achievement_delivery.dispatch_achievements(bot) is True
    delivery = storage.load_achievement_state()["pending"][0]["parts"][0]["delivery"]
    assert delivery["attempts"][-1]["outcome"] == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["lifetime", "attempt_budget"])
async def test_terminal_cleanup_preserves_deduplication(achievement_env, reason):
    clock, _ = achievement_env
    await achievement_delivery.synchronize_achievements()
    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError()
    if reason == "lifetime":
        clock[0] += LIFETIME
    else:
        for _ in range(6):
            await achievement_delivery.dispatch_achievements(bot)
            state = storage.load_achievement_state()
            if state["pending"]:
                clock[0] = state["pending"][0]["parts"][0]["delivery"]["next_attempt_at"]
    assert await achievement_delivery.dispatch_achievements(bot) is False
    state = storage.load_achievement_state()
    assert state["pending"] == [] and state["earned"] == {"animelist": 1}
    await achievement_delivery.synchronize_achievements()
    assert storage.load_achievement_state()["pending"] == []


@pytest.mark.asyncio
async def test_russian_logs_distinguish_startup_success_uncertainty_and_award_retry(
    achievement_env, achievement_row_factory, monkeypatch, caplog,
):
    caplog.set_level(logging.INFO, logger="shikiupdatesbot")
    clock, source = achievement_env
    achievement_delivery.register_achievement_startup(321, "Старт\n\n🏆 Ожидаем данные")
    await achievement_delivery.synchronize_achievements()
    bot = AsyncMock()
    await achievement_delivery.dispatch_achievements(bot)
    assert "дополнение стартового сообщения — успешно; достижений: 1" in caplog.text
    assert "часть 1/1; попытка 1/6" in caplog.text
    source.return_value = {"user_id": 42, "rows": [achievement_row_factory(level=2)]}
    monkeypatch.setattr("achievement_delivery.fetch_achievement_labels", AsyncMock(return_value={}))
    await achievement_delivery.synchronize_achievements()
    bot.send_message.side_effect = TimeoutError("PRIVATE_PAYLOAD")
    await achievement_delivery.dispatch_achievements(bot)
    assert "отправка поздравления владельцу — результат не подтверждён" in caplog.text
    assert "повтор не раньше чем через 60 с" in caplog.text
    assert "Предыдущая попытка могла выполниться" in caplog.text
    clock[0] = storage.load_achievement_state()["pending"][0]["parts"][0]["delivery"]["next_attempt_at"]
    bot.send_message.side_effect = None
    await achievement_delivery.dispatch_achievements(bot)
    assert "отправка поздравления владельцу — успешно" in caplog.text
    assert "Возможно повторное выполнение" in caplog.text
    assert "PRIVATE_" not in caplog.text and "possible_delivery=" not in caplog.text
    assert "confirmed_success" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,expected", [
    ("expiry", "истёк срок доставки"), ("rejection", "отклонено Telegram; повтора не будет"),
    ("unavailable", "стартовое сообщение недоступно"),
])
async def test_terminal_logs_have_russian_reason_and_operation(achievement_env, caplog, scenario, expected):
    caplog.set_level(logging.INFO, logger="shikiupdatesbot")
    clock, _ = achievement_env
    if scenario == "unavailable":
        achievement_delivery.register_achievement_startup(0, "Старт")
    await achievement_delivery.synchronize_achievements()
    bot = AsyncMock()
    if scenario == "expiry":
        clock[0] += LIFETIME
    elif scenario == "rejection":
        bot.send_message.side_effect = TelegramForbiddenError(method=SendMessage(chat_id=999, text="x"), message="Forbidden")
    await achievement_delivery.dispatch_achievements(bot)
    assert expected in caplog.text
    assert "possible_delivery=" not in caplog.text and "expired/" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_first_baseline_freezes_startup_edit_before_operation(achievement_env, empty):
    _, source = achievement_env
    if empty:
        source.return_value = {"user_id": 42, "rows": []}
    achievement_delivery.register_achievement_startup(321, "🟢 Бот запущен\n\n🏆 Ожидаем данные")
    await achievement_delivery.synchronize_achievements()
    state = storage.load_achievement_state()
    assert state["version"] == 2
    plan = state["pending"][0]
    assert plan["kind"] == "startup" and plan["startup_message_id"] == 321
    assert f"<b>{0 if empty else 1}</b>" in plan["parts"][0]["text"]
    assert "Ожидаем данные" not in plan["parts"][0]["text"]
    bot = AsyncMock()

    async def edit(**kwargs):
        current = storage.load_achievement_state()
        assert current["pending"][0]["parts"][0]["delivery"]["attempts"][-1]["outcome"] == "uncertain"
        assert kwargs["message_id"] == 321 and kwargs["chat_id"] == 999
        assert kwargs["text"] == plan["parts"][0]["text"]
        return object()

    bot.edit_message_text.side_effect = edit
    assert await achievement_delivery.dispatch_achievements(bot) is False
    assert storage.load_achievement_state()["pending"] == []
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["uncertain", "cancel", "rejected", "no_target"])
async def test_startup_failure_keeps_original_operation_without_message_fallback(achievement_env, caplog, failure):
    caplog.set_level(logging.INFO, logger="shikiupdatesbot")
    clock, _ = achievement_env
    achievement_delivery.register_achievement_startup(0 if failure == "no_target" else 321, "🟢 Бот запущен")
    await achievement_delivery.synchronize_achievements()
    frozen = deepcopy(storage.load_achievement_state()["pending"])
    bot = AsyncMock()
    method = EditMessageText(chat_id=999, message_id=321, text=frozen[0]["parts"][0]["text"])
    if failure == "cancel":
        bot.edit_message_text.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await achievement_delivery.dispatch_achievements(bot)
    else:
        bot.edit_message_text.side_effect = (
            TimeoutError() if failure == "uncertain" else
            TelegramBadRequest(method=method, message="Bad Request: message can't be edited")
        )
        await achievement_delivery.dispatch_achievements(bot)
    state = storage.load_achievement_state()
    if failure in {"uncertain", "cancel"}:
        assert state["pending"][0]["parts"][0]["delivery"]["attempts"][0]["outcome"] == "uncertain"
        # Новый startup при restart не переписывает замороженный адрес/текст старого долга.
        achievement_delivery.register_achievement_startup(555, "Другой старт")
        await achievement_delivery.synchronize_achievements()
        clock[0] = 1060
        bot.edit_message_text.side_effect = None
        await achievement_delivery.dispatch_achievements(bot)
        assert bot.edit_message_text.await_args.kwargs["message_id"] == 321
        assert bot.edit_message_text.await_args.kwargs["text"] == frozen[0]["parts"][0]["text"]
    else:
        assert state["pending"] == []
    if failure == "rejected":
        assert "дополнение стартового сообщения — отклонено Telegram" in caplog.text
        assert "дополнение стартового сообщения — успешно" not in caplog.text
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("description", [
    "Bad Request: message is not modified",
    "message is not modified",
    "Bad Request: MESSAGE IS NOT MODIFIED",
    "Bad Request: message is not modified: specified new message content and reply markup "
    "are exactly the same as a current content and reply markup of the message.",
])
async def test_already_applied_startup_edit_is_confirmed_despite_error_description_variants(
    achievement_env, caplog, description,
):
    caplog.set_level(logging.INFO, logger="shikiupdatesbot")
    achievement_delivery.register_achievement_startup(321, "🟢 Бот запущен")
    await achievement_delivery.synchronize_achievements()
    plan = storage.load_achievement_state()["pending"][0]
    method = EditMessageText(chat_id=999, message_id=321, text=plan["parts"][0]["text"])
    bot = AsyncMock()
    bot.edit_message_text.side_effect = TelegramBadRequest(method=method, message=description)

    assert await achievement_delivery.dispatch_achievements(bot) is False

    assert storage.load_achievement_state()["pending"] == []
    bot.edit_message_text.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert "дополнение стартового сообщения — успешно" in caplog.text
    assert "отклонено Telegram" not in caplog.text


@pytest.mark.asyncio
async def test_startup_restore_during_edit_rejects_ack(achievement_env):
    achievement_delivery.register_achievement_startup(321, "🟢 Бот запущен")
    await achievement_delivery.synchronize_achievements()
    bot = AsyncMock()

    async def edit(**kwargs):
        storage.mark_restorable_state_restored()
        return object()

    bot.edit_message_text.side_effect = edit
    assert await achievement_delivery.dispatch_achievements(bot) is True
    assert storage.load_achievement_state()["pending"][0]["parts"][0]["delivery"]["attempts"][0]["outcome"] == "uncertain"
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_startup_target_cannot_ack_old_operation(achievement_env):
    achievement_delivery.register_achievement_startup(321, "🟢 Бот запущен")
    await achievement_delivery.synchronize_achievements()
    bot = AsyncMock()

    async def edit(**kwargs):
        state = storage.load_achievement_state()
        state["pending"][0]["startup_message_id"] = 555
        storage.save_achievement_state(state)
        return object()

    bot.edit_message_text.side_effect = edit
    assert await achievement_delivery.dispatch_achievements(bot) is True
    pending = storage.load_achievement_state()["pending"][0]
    assert pending["startup_message_id"] == 555
    assert pending["parts"][0]["delivery"]["attempts"][0]["outcome"] == "uncertain"


@pytest.mark.asyncio
async def test_legacy_baseline_is_not_rewritten_by_new_startup(achievement_env):
    await achievement_delivery.synchronize_achievements()
    frozen = deepcopy(storage.load_achievement_state())
    achievement_delivery.register_achievement_startup(321, "🟢 Бот запущен")
    await achievement_delivery.synchronize_achievements()
    assert storage.load_achievement_state() == frozen
    bot = AsyncMock()
    await achievement_delivery.dispatch_achievements(bot)
    assert bot.send_message.await_args.kwargs["text"] == frozen["pending"][0]["parts"][0]["text"]
    bot.edit_message_text.assert_not_awaited()
