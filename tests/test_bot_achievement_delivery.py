# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Отдельная authority подключена к общим durable attempt/lease контрактам."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

import bot_achievement_delivery
import storage
from achievement_plans import AchievementStateError
from notification_outbox import LIFETIME


@pytest.fixture
def reading_env(backup_env, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("bot_achievement_delivery.time.time", lambda: clock[0])
    monkeypatch.setattr("achievement_delivery.asyncio.sleep", AsyncMock())
    return clock


async def _sync(count):
    return await bot_achievement_delivery.capture_bot_achievement_sync(
        {"reader": {str(i): True for i in range(1, count + 1)}},
        generation=storage.restorable_restore_generation(),
    )


async def _pending(*, catalog=False):
    if catalog:
        from achievement_helpers import catalog_inputs

        for count in (0, 5):
            await bot_achievement_delivery.capture_bot_achievement_sync(
                catalog_inputs(count), generation=storage.restorable_restore_generation(),
            )
        return storage.load_bot_achievement_state()
    await _sync(0)
    await _sync(1)
    return storage.load_bot_achievement_state()


@pytest.mark.asyncio
async def test_silent_baseline_and_frozen_owner_job_are_independent_of_shikimori(reading_env):
    await _sync(51)
    bot = AsyncMock()
    assert await bot_achievement_delivery.dispatch_bot_achievements(bot) is False
    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    await _sync(100)
    captured = storage.load_bot_achievement_state()
    frozen = captured["pending"][0]["parts"][0]["text"]
    assert captured["earned"] == {"reader": 7}
    assert storage.load_achievement_state() is None
    assert await bot_achievement_delivery.dispatch_bot_achievements(bot) is False
    bot.send_message.assert_awaited_once_with(
        chat_id=999, text=frozen, parse_mode="HTML", disable_web_page_preview=True,
    )
    assert storage.load_subscribers() == {}
    assert storage.load_bot_achievement_state()["pending"] == []
    assert storage.load_bot_achievement_state()["earned"] == {"reader": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["capture", "marker", "acknowledgement"])
async def test_publication_failures_preserve_checkpoint_or_attempt_marker(reading_env, monkeypatch, phase):
    if phase == "capture":
        await _sync(0)
    else:
        await _pending()
    original = storage.BOT_ACHIEVEMENTS_FILE.read_bytes()
    real_write = storage._atomic_write
    writes = 0

    def failed(path, payload):
        nonlocal writes
        writes += 1
        if phase != "acknowledgement" or writes == 2:
            raise OSError("PRIVATE_PATH")
        return real_write(path, payload)

    monkeypatch.setattr("storage._atomic_write", failed)
    bot = AsyncMock()
    with pytest.raises(AchievementStateError, match="write"):
        if phase == "capture":
            await _sync(1)
        else:
            await bot_achievement_delivery.dispatch_bot_achievements(bot)
    if phase != "acknowledgement":
        assert storage.BOT_ACHIEVEMENTS_FILE.read_bytes() == original
        bot.send_message.assert_not_awaited()
    else:
        state = storage.load_bot_achievement_state()
        assert state["pending"][0]["parts"][0]["delivery"]["attempts"] == [{"at": 1000, "outcome": "uncertain"}]
        bot.send_message.assert_awaited_once()
    monkeypatch.setattr("storage._atomic_write", real_write)
    if phase == "capture":
        assert await _sync(1)
        assert storage.load_bot_achievement_state()["pending"][0]["awards"] == {"reader": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog", [False, True])
async def test_uncertain_job_survives_restart_decrease_and_refresh_failure(reading_env, monkeypatch, catalog):
    clock = reading_env
    state = await _pending(catalog=catalog)
    earned = deepcopy(state["earned"])
    frozen = state["pending"][0]["parts"][0]["text"]
    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError()
    assert await bot_achievement_delivery.dispatch_bot_achievements(bot)
    clock[0] = 1060
    await _sync(0)
    from stats import sync_stats_all
    monkeypatch.setattr("stats.fetch_list_export", AsyncMock(return_value=None))
    monkeypatch.setattr("stats.load_stats_all", lambda **kw: {})
    assert await sync_stats_all(session=object()) == ({}, False)
    # Перечитывание из файла — новый consumer не пользуется исходным state.
    bot.send_message.side_effect = None
    assert await bot_achievement_delivery.dispatch_bot_achievements(bot) is False
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == [frozen, frozen]
    state = storage.load_bot_achievement_state()
    assert state["earned"] == earned and state["families"]["reader"]["current_value"] == 0 and state["pending"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["generation", "identity", "plan", "lease"])
@pytest.mark.parametrize("catalog", [False, True])
async def test_stale_completion_cannot_acknowledge_restored_or_replaced_reader(reading_env, change, catalog):
    await _pending(catalog=catalog)
    replacement = None

    async def send(**kwargs):
        nonlocal replacement
        replacement = storage.load_bot_achievement_state()
        if change == "generation":
            storage.mark_restorable_state_restored()
        elif change == "identity":
            replacement["state_id"] = "b" * 32
        elif change == "plan":
            replacement["pending"][0]["plan_id"] = "b" * 32
        else:
            replacement["pending"][0]["parts"][0]["text"] = "Другой замороженный текст"
        storage.save_bot_achievement_state(replacement)
        return object()

    bot = AsyncMock()
    bot.send_message.side_effect = send
    assert await bot_achievement_delivery.dispatch_bot_achievements(bot)
    assert storage.load_bot_achievement_state() == replacement


@pytest.mark.asyncio
async def test_stale_dispatch_guard_sends_nothing(reading_env, monkeypatch):
    await _pending()
    real_save = bot_achievement_delivery.save_bot_achievement_state

    def restored(state):
        real_save(state)
        storage.mark_restorable_state_restored()

    monkeypatch.setattr("bot_achievement_delivery.save_bot_achievement_state", restored)
    bot = AsyncMock()
    assert await bot_achievement_delivery.dispatch_bot_achievements(bot)
    bot.send_message.assert_not_awaited()
    assert storage.load_bot_achievement_state()["pending"]


@pytest.mark.asyncio
async def test_capacity_refusal_does_not_lose_crossing(reading_env, monkeypatch):
    await _sync(0)
    original = storage.BOT_ACHIEVEMENTS_FILE.read_bytes()
    monkeypatch.setattr("bot_achievements.BOT_ACHIEVEMENTS_MAX_BYTES", len(original))
    with pytest.raises(AchievementStateError, match="capacity"):
        await _sync(1)
    assert storage.BOT_ACHIEVEMENTS_FILE.read_bytes() == original
    monkeypatch.setattr("bot_achievements.BOT_ACHIEVEMENTS_MAX_BYTES", 8 * 1024 * 1024)
    assert await _sync(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b'{', b'{"version":99}'])
async def test_corrupt_reader_never_reinitializes_or_sends(reading_env, raw):
    storage.BOT_ACHIEVEMENTS_FILE.write_bytes(raw)
    with pytest.raises(AchievementStateError):
        await _sync(51)
    bot = AsyncMock()
    with pytest.raises(AchievementStateError):
        await bot_achievement_delivery.dispatch_bot_achievements(bot)
    assert storage.BOT_ACHIEVEMENTS_FILE.read_bytes() == raw
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["expiry", "attempts"])
async def test_terminal_retirement_preserves_earned_checkpoint(reading_env, end):
    clock = reading_env
    await _pending()
    bot = AsyncMock()
    if end == "expiry":
        clock[0] += LIFETIME
    else:
        bot.send_message.side_effect = TimeoutError()
        for _ in range(6):
            pending = await bot_achievement_delivery.dispatch_bot_achievements(bot)
            state = storage.load_bot_achievement_state()
            if pending:
                clock[0] = state["pending"][0]["parts"][0]["delivery"]["next_attempt_at"]
        assert pending is False
        bot.send_message.reset_mock()
    assert await bot_achievement_delivery.dispatch_bot_achievements(bot) is False
    bot.send_message.assert_not_awaited()
    assert storage.load_bot_achievement_state()["pending"] == []
    assert storage.load_bot_achievement_state()["earned"] == {"reader": 1}


@pytest.mark.asyncio
async def test_capture_checks_generation_and_concurrent_delivery(reading_env, monkeypatch):
    await _pending()
    original = storage.load_bot_achievement_state()
    real_capture = bot_achievement_delivery.capture_bot_achievements

    def changed(*args, **kwargs):
        candidate = real_capture(*args, **kwargs)
        updated = deepcopy(original)
        updated["pending"] = []
        storage.save_bot_achievement_state(updated)
        return candidate

    monkeypatch.setattr("bot_achievement_delivery.capture_bot_achievements", changed)
    assert await _sync(5) is False
    assert storage.load_bot_achievement_state()["pending"] == []
    assert storage.load_bot_achievement_state()["earned"] == {"reader": 1}
    assert await bot_achievement_delivery.capture_bot_achievement_sync({}, generation=-1) is False


@pytest.mark.asyncio
async def test_cancelled_send_keeps_conservative_marker(reading_env):
    await _pending()
    bot = AsyncMock()
    bot.send_message.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await bot_achievement_delivery.dispatch_bot_achievements(bot)
    assert storage.load_bot_achievement_state()["pending"][0]["parts"][0]["delivery"]["attempts"][0]["outcome"] == "uncertain"


@pytest.mark.asyncio
async def test_diagnostic_send_failure_is_bounded_and_preserves_reader_state(reading_env, caplog):
    await _pending()
    original = storage.BOT_ACHIEVEMENTS_FILE.read_bytes()
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("PRIVATE_DIAGNOSTIC_ERROR")
    error = AchievementStateError("PRIVATE_READER_STATE")
    caplog.set_level("WARNING", logger="shikiupdatesbot")

    await bot_achievement_delivery.bot_achievement_diagnostic(bot, error)
    await bot_achievement_delivery.bot_achievement_diagnostic(bot, error)

    bot.send_message.assert_awaited_once()
    assert "Достижения бота временно не обрабатываются" in bot.send_message.await_args.args[1]
    assert storage.BOT_ACHIEVEMENTS_FILE.read_bytes() == original
    assert "PRIVATE_" not in caplog.text and "PRIVATE_" not in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_multiple_families_share_capture_frozen_queue_and_owner_consumer(reading_env, bot_catalog):
    async def capture(inputs):
        return await bot_achievement_delivery.capture_bot_achievement_sync(
            inputs, generation=storage.restorable_restore_generation(),
        )

    await capture({"reader": {}, "anime_minutes": {"minutes": 0}})
    await capture({"reader": {"1": True}, "anime_minutes": {"minutes": 185}})
    saved = storage.load_bot_achievement_state()
    frozen = [part["text"] for plan in saved["pending"] for part in plan["parts"]]
    assert [plan["awards"] for plan in saved["pending"]] == [{"reader": 1, "anime_minutes": 180}]
    assert len(frozen) == 1
    bot = AsyncMock()
    assert await bot_achievement_delivery.dispatch_bot_achievements(bot) is False
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == frozen
    after = storage.load_bot_achievement_state()
    assert after["pending"] == [] and after["earned"] == saved["earned"]
    assert storage.load_achievement_state() is None
    assert [path.name for path in storage.BOT_ACHIEVEMENTS_FILE.parent.glob("*.json")] == ["bot_achievements.json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["calculation", "html"])
async def test_family_calculation_failure_preserves_old_job_and_usable_sibling(reading_env, bot_catalog, monkeypatch, caplog, failure):
    from dataclasses import replace

    from bot_achievements import achievement_catalog

    await _pending()
    original = storage.load_bot_achievement_state()

    def failed(previous, source):
        raise RuntimeError("PRIVATE_CALCULATOR_DATA")

    broken = replace(bot_catalog["reader"], **(
        {"calculate": failed} if failure == "calculation" else {"award_text": lambda level, value: "<b>"}
    ))
    monkeypatch.setattr("bot_achievement_catalog.BOT_ACHIEVEMENTS", achievement_catalog(broken, bot_catalog["anime_minutes"]))
    caplog.set_level("WARNING", logger="shikiupdatesbot")
    assert await bot_achievement_delivery.capture_bot_achievement_sync(
        {"reader": {str(index): True for index in range(1, 6)}, "anime_minutes": {"minutes": 120}},
        generation=storage.restorable_restore_generation(),
    )
    current = storage.load_bot_achievement_state()
    assert current["families"]["reader"] == original["families"]["reader"]
    assert current["pending"] == original["pending"]
    assert current["earned"] == {"reader": 1, "anime_minutes": 60}
    assert ("RuntimeError" if failure == "calculation" else "ValueError") in caplog.text and "PRIVATE_" not in caplog.text
    assert await bot_achievement_delivery.dispatch_bot_achievements(AsyncMock()) is False




@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["privacy", "cancellation"])
async def test_calculator_control_signals_abort_publication(reading_env, bot_catalog, monkeypatch, kind):
    from dataclasses import replace

    from bot_achievements import achievement_catalog
    from shiki_api import ProfilePrivacyError

    await _sync(0)
    original = storage.BOT_ACHIEVEMENTS_FILE.read_bytes()
    error = ProfilePrivacyError("private") if kind == "privacy" else asyncio.CancelledError()

    def interrupted(previous, source):
        raise error

    monkeypatch.setattr("bot_achievement_catalog.BOT_ACHIEVEMENTS", achievement_catalog(
        bot_catalog["reader"], replace(bot_catalog["anime_minutes"], calculate=interrupted),
    ))
    with pytest.raises(type(error)):
        await bot_achievement_delivery.capture_bot_achievement_sync(
            {"reader": {"1": True}, "anime_minutes": {"minutes": 185}},
            generation=storage.restorable_restore_generation(),
        )
    assert storage.BOT_ACHIEVEMENTS_FILE.read_bytes() == original
