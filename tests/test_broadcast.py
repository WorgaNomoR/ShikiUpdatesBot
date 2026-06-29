"""
Тесты ветки broadcast-cleanup.

Покрывают механику чистки служебных сообщений в /broadcast-флоу
(удаление промпта + сообщения владельца + превью; редактирование контрола
в результат) и универсальный примитив _safe_delete, который позже
переиспользует FSM-импорт ветки backup.

Дисциплина репо:
  - import main ВНУТРИ теста (env ставит conftest.py)
  - @pytest.mark.asyncio на async-тестах
  - тест обязан падать на непропатченном коде и проходить на пропатченном
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import handlers

# ─────────────────────────────────────────────────────────────────────────
#  Фабрики фейков
# ─────────────────────────────────────────────────────────────────────────

_CONTENT_ATTRS = ("sticker", "photo", "video", "animation", "document", "voice", "text")


def _make_message(*, message_id=12, chat_id=123, answer_id=600, **content):
    """Фейк aiogram Message с занулёнными типами контента.

    content переопределяет нужные поля, например text='hi' или
    sticker=MagicMock(file_id='f'). bot.send_* настроены так, чтобы
    возвращать Message с предсказуемым message_id (для захвата превью).
    """
    m = MagicMock()
    for attr in _CONTENT_ATTRS:
        setattr(m, attr, None)
    m.caption = None
    for k, v in content.items():
        setattr(m, k, v)

    m.message_id = message_id
    m.chat = MagicMock(id=chat_id)
    m.from_user = MagicMock(id=0)  # перепишем на OWNER_ID в тесте

    m.bot = AsyncMock()
    m.bot.send_message.return_value   = MagicMock(message_id=501)
    m.bot.send_sticker.return_value   = MagicMock(message_id=502)
    m.bot.send_photo.return_value     = MagicMock(message_id=501)
    m.bot.send_video.return_value     = MagicMock(message_id=501)
    m.bot.send_animation.return_value = MagicMock(message_id=501)
    m.bot.send_document.return_value  = MagicMock(message_id=501)
    m.bot.send_voice.return_value     = MagicMock(message_id=501)

    m.answer = AsyncMock(return_value=MagicMock(message_id=answer_id))
    return m


def _make_callback(*, control_id=600, chat_id=123):
    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.message_id = control_id
    cb.message.chat = MagicMock(id=chat_id)
    cb.message.bot = AsyncMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    return cb


def _make_state(data=None):
    st = AsyncMock()
    st.get_data.return_value = dict(data or {})
    return st


# ─────────────────────────────────────────────────────────────────────────
#  Юнит 1 — _safe_delete (универсальный verify-тест, переиспользуется в backup)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_safe_delete_swallows_errors():
    """_safe_delete не должен пробрасывать исключение наружу.
    FAIL на старом коде: функции _safe_delete не существует (AttributeError)."""

    bot = AsyncMock()
    bot.delete_message = AsyncMock(side_effect=Exception("message to delete not found"))

    await handlers._safe_delete(bot, 123, 99)  # не должно бросить

    bot.delete_message.assert_awaited_once_with(123, 99)


@pytest.mark.asyncio
async def test_safe_delete_calls_bot():

    bot = AsyncMock()
    await handlers._safe_delete(bot, 5, 7)
    bot.delete_message.assert_awaited_once_with(5, 7)


# ─────────────────────────────────────────────────────────────────────────
#  Юнит 2 — _send_broadcast_message возвращает список Message
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_broadcast_returns_one_message_for_text():
    """FAIL на старом: helper возвращал None."""

    bot = AsyncMock()
    bot.send_message.return_value = MagicMock(message_id=501)

    sent = await handlers._send_broadcast_message(bot, 123, {"msg_type": "text", "user_text": "hi"})

    assert [m.message_id for m in sent] == [501]


@pytest.mark.asyncio
async def test_send_broadcast_returns_two_messages_for_sticker():
    """Стикер = шапка + стикер. FAIL на старом: None, два id не захватить."""

    bot = AsyncMock()
    bot.send_message.return_value = MagicMock(message_id=501)
    bot.send_sticker.return_value = MagicMock(message_id=502)

    sent = await handlers._send_broadcast_message(
        bot, 123, {"msg_type": "sticker", "file_id": "f", "user_text": ""}
    )

    assert [m.message_id for m in sent] == [501, 502]


# ─────────────────────────────────────────────────────────────────────────
#  Юнит 3 — cmd_broadcast: удаляет команду, запоминает промпт
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_broadcast_deletes_command_and_stores_prompt():
    """FAIL на старом: команда не удаляется, prompt_msg_id не сохраняется."""

    msg = _make_message(message_id=10, answer_id=11)
    msg.from_user.id = config.OWNER_ID
    state = _make_state()

    await handlers.cmd_broadcast(msg, state)

    msg.bot.delete_message.assert_any_await(123, 10)  # сама /broadcast
    state.update_data.assert_any_await(prompt_msg_id=11)


@pytest.mark.asyncio
async def test_cmd_broadcast_rejects_non_owner():

    msg = _make_message(message_id=10)
    msg.from_user.id = config.OWNER_ID + 999
    state = _make_state()

    await handlers.cmd_broadcast(msg, state)

    msg.answer.assert_awaited()                 # получил отказ
    msg.bot.delete_message.assert_not_called()  # ничего не удаляем чужому


# ─────────────────────────────────────────────────────────────────────────
#  Юнит 4 — broadcast_receive: чистит A и B, шлёт превью + контрол
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_broadcast_receive_text_cleans_and_stores_preview(monkeypatch):
    """FAIL на старом: ни промпт, ни сообщение владельца не удаляются;
    preview_msg_ids не сохраняется."""
    monkeypatch.setattr("handlers.load_subscribers", lambda: {123: "owner"})

    msg = _make_message(message_id=12, chat_id=123, text="hello")
    state = _make_state({"prompt_msg_id": 11})

    await handlers.broadcast_receive(msg, state)

    msg.bot.delete_message.assert_any_await(123, 11)  # промпт A
    msg.bot.delete_message.assert_any_await(123, 12)  # сообщение владельца B
    state.update_data.assert_any_await(preview_msg_ids=[501], control_msg_id=600)
    state.set_state.assert_awaited_with(handlers.BroadcastStates.waiting_confirm)


@pytest.mark.asyncio
async def test_broadcast_receive_sticker_preview_has_two_ids(monkeypatch):
    """Превью-стикер = 2 сообщения; оба id обязаны попасть в preview_msg_ids,
    иначе шапка осиротеет. FAIL на старом по той же причине."""
    monkeypatch.setattr("handlers.load_subscribers", lambda: {123: "owner"})

    msg = _make_message(message_id=12, chat_id=123, sticker=MagicMock(file_id="f"))
    state = _make_state({"prompt_msg_id": 11})

    await handlers.broadcast_receive(msg, state)

    state.update_data.assert_any_await(preview_msg_ids=[501, 502], control_msg_id=600)


@pytest.mark.asyncio
async def test_broadcast_receive_unsupported_type_keeps_state(monkeypatch):
    """Неподдержанный тип: предупреждаем, состояние не трогаем, ничего не удаляем."""
    monkeypatch.setattr("handlers.load_subscribers", lambda: {123: "owner"})

    msg = _make_message(message_id=12)  # все типы None
    state = _make_state({"prompt_msg_id": 11})

    await handlers.broadcast_receive(msg, state)

    msg.answer.assert_awaited()
    msg.bot.delete_message.assert_not_called()
    state.set_state.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
#  Юнит 5 — confirm / cancel
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_broadcast_confirm_deletes_preview_and_edits_control(monkeypatch):
    """FAIL на старом: превью не удаляется; результат шлётся .answer(),
    а не редактированием контрола."""
    monkeypatch.setattr("handlers.load_subscribers", lambda: {123: "owner"})
    monkeypatch.setattr("handlers.save_subscribers", lambda *_: None)

    cb = _make_callback(control_id=600, chat_id=123)
    state = _make_state({
        "msg_type": "text", "user_text": "hi", "file_id": None,
        "preview_msg_ids": [501],
    })

    await handlers.broadcast_confirm_cb(cb, state)

    cb.message.bot.delete_message.assert_any_await(123, 501)   # превью убрали
    cb.message.edit_text.assert_awaited()                      # контрол → результат
    sent_text = cb.message.edit_text.await_args.args[0] if cb.message.edit_text.await_args.args \
        else cb.message.edit_text.await_args.kwargs.get("text", "")
    assert "Отправлено" in sent_text


@pytest.mark.asyncio
async def test_broadcast_confirm_no_subs_edits_control(monkeypatch):
    monkeypatch.setattr("handlers.load_subscribers", lambda: {})

    cb = _make_callback()
    state = _make_state({"msg_type": "text", "user_text": "hi", "preview_msg_ids": [501]})

    await handlers.broadcast_confirm_cb(cb, state)

    cb.message.bot.delete_message.assert_any_await(123, 501)
    cb.message.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_broadcast_cancel_deletes_preview_and_control(monkeypatch):
    """FAIL на старом: ни превью, ни контрол не удаляются; подписчикам
    при этом не должно уйти НИЧЕГО."""
    monkeypatch.setattr("handlers.load_subscribers", lambda: {123: "owner"})

    cb = _make_callback(control_id=600, chat_id=123)
    state = _make_state({"msg_type": "text", "user_text": "hi", "preview_msg_ids": [501]})

    await handlers.broadcast_cancel_cb(cb, state)

    cb.message.bot.delete_message.assert_any_await(123, 501)   # превью
    cb.message.bot.delete_message.assert_any_await(123, 600)   # контрол
    cb.message.bot.send_message.assert_not_called()            # ничего не разослали


@pytest.mark.asyncio
async def test_cmd_cancel_in_confirm_cleans_preview_and_control():
    """waiting_confirm: /cancel обязан убрать промпт, превью И контрол.
    FAIL на версии юнита 6, чистившей только промпт."""
    msg = _make_message(message_id=20, chat_id=123)
    msg.from_user.id = config.OWNER_ID
    state = _make_state({"prompt_msg_id": 11, "preview_msg_ids": [100], "control_msg_id": 101})
    state.get_state.return_value = handlers.BroadcastStates.waiting_confirm

    await handlers.cmd_cancel(msg, state)

    msg.bot.delete_message.assert_any_await(123, 11)   # промпт
    msg.bot.delete_message.assert_any_await(123, 100)  # превью
    msg.bot.delete_message.assert_any_await(123, 101)  # контрол
    msg.bot.delete_message.assert_any_await(123, 20)   # эхо /cancel


@pytest.mark.asyncio
async def test_cmd_cancel_in_content_cleans_prompt_only():
    """waiting_content: превью ещё нет — чистим только промпт и эхо."""
    msg = _make_message(message_id=20, chat_id=123)
    msg.from_user.id = config.OWNER_ID
    state = _make_state({"prompt_msg_id": 11})
    state.get_state.return_value = handlers.BroadcastStates.waiting_content

    await handlers.cmd_cancel(msg, state)

    msg.bot.delete_message.assert_any_await(123, 11)
    msg.bot.delete_message.assert_any_await(123, 20)
    assert msg.bot.delete_message.await_count == 2
