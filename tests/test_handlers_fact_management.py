# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Owner-only оркестрация загрузки, применения и очистки facts.json."""

import json
from types import SimpleNamespace
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.exceptions import TelegramBadRequest

import fact_bank
import handlers
from inline_facts import INLINE_FACTS

_UNSET = object()


def _payload(facts, *, version="owner-bank") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "bank_version": version if facts else None,
            "facts": facts,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _fact(fact_id="extra-one", text="Дополнительный факт."):
    return {"id": fact_id, "text": text}


def _message(*, owner=True, raw=None, file_size=_UNSET):
    bot = SimpleNamespace(download=AsyncMock())
    if raw is not None:
        async def download(document, destination):
            destination.write(raw)

        bot.download.side_effect = download
    document = None
    if raw is not None or file_size is not _UNSET:
        document = SimpleNamespace(
            file_name="facts.json",
            file_size=len(raw) if file_size is _UNSET else file_size,
        )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=handlers.OWNER_ID if owner else 777),
        document=document,
        bot=bot,
        chat=SimpleNamespace(id=55),
        message_id=66,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=88)),
        reply=AsyncMock(return_value=SimpleNamespace(message_id=77)),
    )


def _callback(data, *, owner=True):
    message = SimpleNamespace(
        message_id=77,
        edit_text=AsyncMock(return_value=SimpleNamespace(message_id=77)),
        answer_document=AsyncMock(),
        bot=AsyncMock(),
        chat=SimpleNamespace(id=55),
        reply_to_message=None,
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=handlers.OWNER_ID if owner else 777),
        answer=AsyncMock(),
        message=message,
    )


def _menu_callback_data(message) -> list[str]:
    keyboard = message.reply.await_args.kwargs["reply_markup"]
    return [row[0].callback_data for row in keyboard.inline_keyboard]


async def _publish(raw: bytes):
    document = fact_bank.parse_fact_bank_bytes(raw)
    return await fact_bank.publish_fact_bank(
        document,
        expected_revision=fact_bank.get_fact_bank_snapshot().revision,
    )


@pytest.mark.parametrize(
    ("count", "word"),
    [
        (1, "факт"),
        (2, "факта"),
        (4, "факта"),
        (5, "фактов"),
        (11, "фактов"),
        (12, "фактов"),
        (14, "фактов"),
        (21, "факт"),
        (22, "факта"),
        (25, "фактов"),
        (111, "фактов"),
    ],
)
def test_fact_count_word_uses_russian_plural_rules(count, word):
    assert handlers._fact_count_word(count) == word


@pytest.mark.asyncio
async def test_facts_status_is_owner_only_and_hidden_state_is_not_read_for_non_owner(
    fact_bank_env,
    monkeypatch,
):
    state = AsyncMock()
    reload_bank = MagicMock(side_effect=AssertionError("non-owner прочитал файл"))
    monkeypatch.setattr(handlers, "reload_fact_bank", reload_bank)
    outsider = _message(owner=False)

    await handlers.cmd_facts(outsider, state)

    outsider.answer.assert_awaited_once_with("🚫 Эта команда только для владельца бота.")
    state.clear.assert_not_awaited()
    reload_bank.assert_not_called()


@pytest.mark.asyncio
async def test_facts_status_reports_missing_valid_empty_and_invalid_file(fact_bank_env):
    state = AsyncMock()
    message = _message()

    await handlers.cmd_facts(message, state)
    assert "файл ещё не создан" in message.reply.await_args.args[0]
    assert f"Встроенных: <b>{len(INLINE_FACTS)}</b>" in message.reply.await_args.args[0]
    assert _menu_callback_data(message) == [
        "facts:upload",
        "facts:example",
        "facts:close",
    ]

    fact_bank_env.write_text(
        fact_bank.serialize_fact_bank(fact_bank.empty_fact_bank_document()),
        encoding="utf-8",
    )
    await handlers.cmd_facts(message, state)
    assert "файл корректен" in message.reply.await_args.args[0]
    assert "Дополнительных: <b>0</b>" in message.reply.await_args.args[0]
    assert _menu_callback_data(message) == [
        "facts:upload",
        "facts:example",
        "facts:close",
    ]

    fact_bank_env.write_bytes(b"{broken")
    await handlers.cmd_facts(message, state)
    assert "повреждён" in message.reply.await_args.args[0]
    assert _menu_callback_data(message) == [
        "facts:upload",
        "facts:example",
        "facts:close",
    ]

    snapshot = await _publish(_payload([_fact("active")]))
    await handlers.cmd_facts(message, state)
    assert _menu_callback_data(message) == [
        "facts:upload",
        "facts:download",
        f"facts:ask-clear:{snapshot.revision}",
        "facts:close",
    ]


@pytest.mark.asyncio
async def test_upload_callback_enters_fsm_and_promises_preview(fact_bank_env):
    state = AsyncMock()
    callback = _callback("facts:upload")

    await handlers.facts_upload_cb(callback, state)

    state.set_state.assert_awaited_once_with(handlers.FactsStates.waiting_upload_file)
    text = callback.message.edit_text.await_args.args[0]
    assert "Применить" in text
    assert "/cancel" in text
    state.update_data.assert_awaited_once_with(prompt_msg_id=77)


@pytest.mark.asyncio
async def test_invalid_upload_preserves_disk_snapshot_and_waiting_state(
    fact_bank_env,
    monkeypatch,
):
    before = await _publish(_payload([_fact("current")]))
    old_file = fact_bank_env.read_bytes()
    message = _message(raw=b"{broken")
    state = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_delete", AsyncMock())

    await handlers.facts_receive(message, state)

    assert "не прошёл проверку" in message.answer.await_args.args[0]
    assert fact_bank_env.read_bytes() == old_file
    assert fact_bank.get_fact_bank_snapshot() == before
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("file_size", [None, fact_bank.FACT_BANK_MAX_BYTES + 1])
async def test_upload_rejects_unknown_or_oversized_document_before_download(
    fact_bank_env,
    file_size,
):
    message = _message(raw=b"{}", file_size=file_size)
    state = AsyncMock()

    await handlers.facts_receive(message, state)

    message.bot.download.assert_not_awaited()
    assert "512 КиБ" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_upload_preview_has_independent_delta_and_does_not_mutate(
    fact_bank_env,
    monkeypatch,
):
    before = await _publish(
        _payload([_fact("changed", "Старый."), _fact("removed", "Удаляемый.")])
    )
    old_file = fact_bank_env.read_bytes()
    candidate = _payload([
        _fact("changed", "Новый."),
        _fact("added", "Добавленный."),
    ], version="preview-v2")
    message = _message(raw=candidate)
    state = AsyncMock()
    state.get_data.return_value = {"prompt_msg_id": 77, "control_msg_id": 88}
    deleted = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_delete", deleted)

    await handlers.facts_receive(message, state)

    text = message.answer.await_args.args[0]
    assert "Добавится: <b>1</b>" in text
    assert "Изменится: <b>1</b>" in text
    assert "Удалится: <b>1</b>" in text
    assert fact_bank_env.read_bytes() == old_file
    assert fact_bank.get_fact_bank_snapshot() == before
    state.set_state.assert_awaited_once_with(
        handlers.FactsStates.waiting_apply_confirmation
    )
    saved = state.update_data.await_args.kwargs
    assert saved["expected_revision"] == before.revision
    callback_data = (
        message.answer.await_args.kwargs["reply_markup"]
        .inline_keyboard[0][0].callback_data
    )
    assert callback_data == (
        f"{handlers.FACTS_APPLY_CALLBACK_PREFIX}{before.revision}:"
        f"{saved['candidate_revision']}"
    )
    deleted.assert_any_await(message.bot, 55, 77)
    deleted.assert_any_await(message.bot, 55, 88)
    deleted.assert_any_await(message.bot, 55, 66)


@pytest.mark.asyncio
async def test_apply_replaces_bank_without_subscriber_media_or_backup_side_effects(
    fact_bank_env,
    monkeypatch,
):
    before = await _publish(_payload([_fact("old")]))
    candidate = fact_bank.parse_fact_bank_bytes(
        _payload([_fact("new")], version="apply-v2")
    )
    canonical = fact_bank.serialize_fact_bank(candidate)
    candidate_revision = fact_bank.fact_bank_candidate_revision(candidate)
    callback = _callback(
        f"facts:apply:{before.revision}:{candidate_revision}"
    )
    state = AsyncMock()
    state.get_data.return_value = {
        "candidate_json": canonical,
        "candidate_revision": candidate_revision,
        "expected_revision": before.revision,
    }
    monkeypatch.setattr(
        handlers,
        "inline_access_status",
        MagicMock(side_effect=AssertionError("прочитаны подписчики")),
    )
    monkeypatch.setattr(
        handlers,
        "send_backup",
        AsyncMock(side_effect=AssertionError("отправлен backup")),
    )
    monkeypatch.setattr(
        handlers._inline_search_service,
        "get_page",
        AsyncMock(side_effect=AssertionError("запущен media search")),
    )

    await handlers.facts_apply_cb(callback, state)

    assert [
        fact.id for fact in fact_bank.get_fact_bank_snapshot().additional_facts
    ] == ["new"]
    assert "old" not in fact_bank_env.read_text(encoding="utf-8")
    state.clear.assert_awaited_once()
    callback.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_and_global_cancel_preserve_candidate_and_active_bank(
    fact_bank_env,
    monkeypatch,
):
    before = await _publish(_payload([_fact("current")]))
    callback = _callback("facts:cancel")
    state = AsyncMock()

    await handlers.facts_cancel_cb(callback, state)

    state.clear.assert_awaited_once()
    assert fact_bank.get_fact_bank_snapshot().additional_facts == before.additional_facts

    command = _message()
    state.reset_mock()
    state.get_state.return_value = handlers.FactsStates.waiting_upload_file.state
    state.get_data.return_value = {
        "prompt_msg_id": 77,
        "control_msg_id": 88,
        "preview_msg_ids": [],
    }
    deleted = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_delete", deleted)

    await handlers.cmd_cancel(command, state)

    state.clear.assert_awaited_once()
    assert fact_bank.get_fact_bank_snapshot().additional_facts == before.additional_facts
    assert command.answer.await_args.args[0] == "❌ Отменено."


@pytest.mark.asyncio
async def test_download_returns_populated_and_empty_canonical_documents(fact_bank_env):
    await _publish(_payload([_fact("downloaded")], version="download-v1"))
    callback = _callback("facts:download")

    await handlers.facts_download_cb(callback)

    sent = callback.message.answer_document.await_args.args[0]
    assert sent.filename == "facts.json"
    assert json.loads(sent.data) == {
        "schema_version": 1,
        "bank_version": "download-v1",
        "facts": [{"id": "downloaded", "text": "Дополнительный факт."}],
    }

    fact_bank_env.write_bytes(b"{broken")
    callback.message.answer_document.reset_mock()
    await handlers.facts_download_cb(callback)
    empty = callback.message.answer_document.await_args.args[0]
    assert json.loads(empty.data)["facts"] == []


@pytest.mark.asyncio
async def test_example_button_sends_valid_bundled_five_fact_document(fact_bank_env):
    callback = _callback("facts:example")

    await handlers.facts_example_cb(callback)

    sent = callback.message.answer_document.await_args.args[0]
    document = fact_bank.parse_fact_bank_bytes(sent.data)
    assert sent.filename == "facts.json"
    assert document.bank_version == "example-1"
    assert len(document.facts) == 5
    callback.answer.assert_awaited_once_with("Готовлю пример facts.json...")


@pytest.mark.asyncio
async def test_missing_bundled_example_degrades_to_owner_alert(
    fact_bank_env,
    monkeypatch,
    tmp_path,
):
    callback = _callback("facts:example")
    monkeypatch.setattr(
        handlers,
        "FACT_BANK_EXAMPLE_PATH",
        tmp_path / "missing-facts.json",
    )

    await handlers.facts_example_cb(callback)

    callback.message.answer_document.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "Пример facts.json недоступен в этой сборке.",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_clear_is_exactly_menu_press_then_one_dynamic_confirmation(
    fact_bank_env,
    monkeypatch,
):
    snapshot = await _publish(_payload([_fact("one"), _fact("two")]))
    callback = _callback(f"facts:ask-clear:{snapshot.revision}")
    ask_state = AsyncMock()
    send_backup = AsyncMock(side_effect=AssertionError("clear отправил backup"))
    monkeypatch.setattr(handlers, "send_backup", send_backup)

    await handlers.facts_ask_clear_cb(callback, ask_state)

    keyboard = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert len(keyboard.inline_keyboard) == 2
    assert keyboard.inline_keyboard[0][0].text == "Да, удалить 2 факта"
    confirm_data = keyboard.inline_keyboard[0][0].callback_data
    assert confirm_data == f"facts:confirm-clear:{snapshot.revision}"
    ask_state.clear.assert_awaited_once()

    confirm = _callback(confirm_data)
    confirm_state = AsyncMock()
    await handlers.facts_confirm_clear_cb(confirm, confirm_state)

    active = fact_bank.get_fact_bank_snapshot()
    assert active.additional_facts == ()
    assert len(active.facts) == len(INLINE_FACTS)
    assert json.loads(fact_bank_env.read_text(encoding="utf-8"))["facts"] == []
    confirm_state.clear.assert_awaited_once()
    send_backup.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_clear_confirmation_cannot_remove_newer_bank(fact_bank_env):
    old = await _publish(_payload([_fact("old")], version="old"))
    newer = fact_bank.parse_fact_bank_bytes(
        _payload([_fact("newer")], version="newer")
    )
    newer_snapshot = await fact_bank.publish_fact_bank(
        newer,
        expected_revision=old.revision,
    )
    callback = _callback(f"facts:confirm-clear:{old.revision}")
    state = AsyncMock()

    await handlers.facts_confirm_clear_cb(callback, state)

    assert fact_bank.get_fact_bank_snapshot() == newer_snapshot
    callback.answer.assert_awaited_once_with(
        "Банк уже изменился. Очистка не выполнена.",
        show_alert=True,
    )
    state.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_replayed_apply_without_fsm_candidate_is_rejected(fact_bank_env):
    snapshot = fact_bank.get_fact_bank_snapshot()
    callback = _callback(f"facts:apply:{snapshot.revision}:{'0' * 16}")
    state = AsyncMock()
    state.get_data.return_value = {}

    await handlers.facts_apply_cb(callback, state)

    callback.answer.assert_awaited_once_with(
        "Это подтверждение уже недействительно. Отправь /facts ещё раз.",
        show_alert=True,
    )
    assert fact_bank.get_fact_bank_snapshot() == snapshot


@pytest.mark.asyncio
async def test_stale_apply_callback_preserves_newer_bank(fact_bank_env):
    old = await _publish(_payload([_fact("old")], version="old"))
    candidate = fact_bank.parse_fact_bank_bytes(
        _payload([_fact("candidate")], version="candidate")
    )
    candidate_json = fact_bank.serialize_fact_bank(candidate)
    candidate_revision = fact_bank.fact_bank_candidate_revision(candidate)
    newer = fact_bank.parse_fact_bank_bytes(
        _payload([_fact("newer")], version="newer")
    )
    newer_snapshot = await fact_bank.publish_fact_bank(
        newer,
        expected_revision=old.revision,
    )
    callback = _callback(
        f"facts:apply:{old.revision}:{candidate_revision}"
    )
    state = AsyncMock()
    state.get_data.return_value = {
        "candidate_json": candidate_json,
        "candidate_revision": candidate_revision,
        "expected_revision": old.revision,
    }

    await handlers.facts_apply_cb(callback, state)

    assert fact_bank.get_fact_bank_snapshot() == newer_snapshot
    callback.answer.assert_awaited_once_with(
        "Банк уже изменился. Открой /facts и проверь новое состояние.",
        show_alert=True,
    )
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_clears_fsm_and_removes_owner_menu(fact_bank_env):
    callback = _callback("facts:close")
    callback.message.delete = AsyncMock()
    command = SimpleNamespace(delete=AsyncMock())
    callback.message.reply_to_message = command
    state = AsyncMock()

    await handlers.facts_close_cb(callback, state)

    state.clear.assert_awaited_once()
    callback.answer.assert_awaited_once_with()
    callback.message.delete.assert_awaited_once_with()
    command.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_non_owner_cannot_download_example_or_clear_fact_bank(fact_bank_env):
    snapshot = await _publish(_payload([_fact("protected")]))
    download = _callback("facts:download", owner=False)
    clear = _callback(
        f"facts:confirm-clear:{snapshot.revision}",
        owner=False,
    )
    clear_state = AsyncMock()
    example = _callback("facts:example", owner=False)

    await handlers.facts_download_cb(download)
    await handlers.facts_example_cb(example)
    await handlers.facts_confirm_clear_cb(clear, clear_state)

    download.message.answer_document.assert_not_awaited()
    assert fact_bank.get_fact_bank_snapshot() == snapshot
    download.answer.assert_awaited_once_with(
        "🚫 Только для владельца.",
        show_alert=True,
    )
    clear_state.clear.assert_not_awaited()
    clear.answer.assert_awaited_once_with(
        "🚫 Только для владельца.",
        show_alert=True,
    )
    example.message.answer_document.assert_not_awaited()
    example.answer.assert_awaited_once_with(
        "🚫 Только для владельца.",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_successful_apply_survives_noneditable_telegram_menu(fact_bank_env):
    before = await _publish(_payload([_fact("old")], version="old"))
    candidate = fact_bank.parse_fact_bank_bytes(
        _payload([_fact("new")], version="new")
    )
    candidate_json = fact_bank.serialize_fact_bank(candidate)
    candidate_revision = fact_bank.fact_bank_candidate_revision(candidate)
    callback = _callback(
        f"facts:apply:{before.revision}:{candidate_revision}"
    )
    callback.message.edit_text.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="Bad Request: message to edit not found",
    )
    state = AsyncMock()
    state.get_data.return_value = {
        "candidate_json": candidate_json,
        "candidate_revision": candidate_revision,
        "expected_revision": before.revision,
    }

    await handlers.facts_apply_cb(callback, state)

    assert [
        fact.id for fact in fact_bank.get_fact_bank_snapshot().additional_facts
    ] == ["new"]
    state.clear.assert_awaited_once()
    callback.answer.assert_awaited_once_with("Дополнительный банк применён.")


@pytest.mark.asyncio
async def test_upload_menu_edit_failure_clears_fsm(fact_bank_env):
    callback = _callback("facts:upload")
    callback.message.edit_text.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="Bad Request: message is not modified",
    )
    state = AsyncMock()

    await handlers.facts_upload_cb(callback, state)

    state.set_state.assert_awaited_once_with(handlers.FactsStates.waiting_upload_file)
    state.clear.assert_awaited_once()
    callback.answer.assert_awaited_once_with(
        "Не удалось открыть меню. Отправь /facts ещё раз.",
        show_alert=True,
    )
