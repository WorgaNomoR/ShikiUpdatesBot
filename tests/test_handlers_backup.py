# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Тесты хендлеров флоу /backup (handlers.py): меню, экспорт, импорт, приём zip.

Оркестрация: мокаем только I/O-границы (send_backup, restore_backup_zip,
bot.download, _safe_delete, storage). Ядро backup.py (сборка/восстановление
zip, whitelist, авто-бэкап) живёт в test_backup.py. Фикстура backup_env —
в conftest.py (общая с test_backup.py). Дисциплина: тест падает на
непропатченном коде и проходит на пропатченном.
"""
from unittest.mock import (
    ANY,
    AsyncMock,
    MagicMock,
    call,
)

import pytest
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Chat,
    InaccessibleMessage,
)

import handlers
import main_menu
from backup import BackupLimitError
from event_time_stats import EventTimeStateError
from storage import QuarterDeliveryStateError

# ─────────────────────────────────────────────────────────────
#  Команда /backup и интеграция в под/отписку
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_backup_rejects_non_owner(backup_env):
    msg = MagicMock()
    msg.from_user.id = 1  # не владелец
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    await handlers.cmd_backup(msg)
    msg.answer.assert_awaited_once()
    msg.reply.assert_not_awaited()
    # меню не показано (нет reply_markup)
    assert "reply_markup" not in msg.answer.call_args.kwargs


@pytest.mark.asyncio
async def test_cmd_backup_owner_shows_menu(backup_env):
    msg = MagicMock()
    msg.from_user.id = handlers.OWNER_ID
    msg.chat.type = ChatType.PRIVATE
    msg.reply = AsyncMock()
    await handlers.cmd_backup(msg)
    kwargs = msg.reply.call_args.kwargs
    assert "reply_markup" in kwargs   # инлайн-меню есть
    assert msg.reply.call_args.args[0] == main_menu.owner_backup_view().text
    buttons = kwargs["reply_markup"].inline_keyboard
    labels = [row[0].text for row in buttons[:3]]
    expected = main_menu.owner_backup_view().keyboard.inline_keyboard
    assert labels == [row[0].text for row in expected[:3]]


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", [ChatType.GROUP, ChatType.SUPERGROUP])
async def test_cmd_backup_rejects_non_private_chat(backup_env, chat_type):
    message = MagicMock()
    message.from_user.id = handlers.OWNER_ID
    message.chat.type = chat_type
    message.answer = AsyncMock()
    message.reply = AsyncMock()

    await handlers.cmd_backup(message)

    message.reply.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert "личном чате" in message.answer.await_args.args[0]
    assert "reply_markup" not in message.answer.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [
    handlers.backup_recovery_cb,
    handlers.backup_export_cb,
    handlers.backup_import_cb,
    handlers.backup_close_cb,
], ids=["recovery", "export", "import", "close"])
@pytest.mark.parametrize("invalid", ["group", "supergroup", "missing", "inaccessible"])
async def test_backup_callbacks_reject_invalid_chat_before_work(backup_env, monkeypatch, handler, invalid):
    sent = AsyncMock(return_value=False)
    deleted = AsyncMock()
    cleanup = AsyncMock()
    monkeypatch.setattr("handlers.send_backup", sent)
    monkeypatch.setattr("handlers._safe_delete", deleted)
    monkeypatch.setattr("handlers._cleanup_inline_menu", cleanup)
    state = AsyncMock()
    callback = MagicMock()
    callback.from_user.id = handlers.OWNER_ID
    callback.answer = AsyncMock()
    menu = callback.message
    menu.chat.id = -100
    menu.chat.type = ChatType.GROUP if invalid == "group" else ChatType.SUPERGROUP
    menu.bot = AsyncMock()
    menu.photo = []
    menu.edit_text = AsyncMock(return_value=menu)
    if invalid == "missing":
        callback.message = None
    elif invalid == "inaccessible":
        callback.message = InaccessibleMessage(
            chat=Chat(id=handlers.OWNER_ID, type=ChatType.PRIVATE),
            message_id=42,
            date=0,
        )

    if handler in (handlers.backup_import_cb, handlers.backup_close_cb):
        await handler(callback, state)
    else:
        await handler(callback)

    sent.assert_not_awaited()
    menu.bot.send_message.assert_not_awaited()
    deleted.assert_not_awaited()
    cleanup.assert_not_awaited()
    menu.edit_text.assert_not_awaited()
    assert state.mock_calls == []
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert "личном чате" in callback.answer.await_args.args[0]


# ─────────────────────────────────────────────────────────────
#  Кнопка «Закрыть» в меню /backup (паттерн как у /stats)
# ─────────────────────────────────────────────────────────────

def test_backup_menu_has_close_button():
    kb = handlers._backup_menu_kb()
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "backup:close" in datas
    assert "backup:recovery" in datas
    assert "backup:export" in datas


@pytest.mark.asyncio
async def test_backup_close_delegates_cleanup(backup_env, monkeypatch):
    cleanup = AsyncMock()
    monkeypatch.setattr(handlers, "_cleanup_inline_menu", cleanup)
    menu = MagicMock()
    menu.chat.type = ChatType.PRIVATE
    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.message = menu
    cb.answer = AsyncMock()

    await handlers.backup_close_cb(cb, AsyncMock())

    cleanup.assert_awaited_once_with(menu)


@pytest.mark.asyncio
async def test_backup_close_rejects_non_owner(backup_env):
    cb = MagicMock()
    cb.from_user.id = 1
    cb.message = MagicMock()
    cb.message.delete = AsyncMock()
    cb.answer = AsyncMock()
    await handlers.backup_close_cb(cb, AsyncMock())
    cb.message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_backup_close_clears_fsm_state(backup_env, monkeypatch):
    monkeypatch.setattr(handlers, "_cleanup_inline_menu", AsyncMock())
    state = AsyncMock()
    menu = MagicMock()
    menu.chat.type = ChatType.PRIVATE
    menu.delete = AsyncMock()
    menu.reply_to_message = None
    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.message = menu
    cb.answer = AsyncMock()
    await handlers.backup_close_cb(cb, state)
    state.clear.assert_awaited_once()


# ─────────────────────────────────────────────────────────────
#  backup_export_cb — кнопка «📤 Экспорт» (оркестрация)
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [handlers.backup_recovery_cb, handlers.backup_export_cb])
async def test_backup_export_rejects_non_owner(backup_env, monkeypatch, handler):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(handlers, "send_backup", sent)

    cb = MagicMock()
    cb.from_user.id = 1                       # не владелец (OWNER_ID=999 в backup_env)
    cb.answer = AsyncMock()

    await handler(cb)

    cb.answer.assert_awaited_once()
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    sent.assert_not_awaited()                 # архив НЕ собирали


@pytest.mark.asyncio
@pytest.mark.parametrize("full_export", [False, True])
async def test_backup_export_owner_sends_archive(backup_env, monkeypatch, full_export):
    sent = AsyncMock(return_value=True)       # send_backup успешен
    monkeypatch.setattr(handlers, "send_backup", sent)
    deleted = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_delete", deleted)

    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.message.chat.type = ChatType.PRIVATE
    cb.answer = AsyncMock()
    cb.message.bot = AsyncMock()
    cb.message.chat.id = 999
    cb.message.message_id = 42

    handler = handlers.backup_export_cb if full_export else handlers.backup_recovery_cb
    await handler(cb)

    sent.assert_awaited_once()                 # архив собран и отправлен
    assert sent.await_args.kwargs == {"full_export": full_export}
    assert ("Архив для диагностики" in sent.await_args.args[1]) is full_export
    deleted.assert_awaited_once_with(cb.message.bot, 999, 42)   # меню убрано: (bot, chat_id, message_id)
    cb.message.bot.send_message.assert_not_awaited()   # ошибки нет


@pytest.mark.asyncio
async def test_backup_export_reports_failure(backup_env, monkeypatch):
    monkeypatch.setattr(handlers, "send_backup", AsyncMock(return_value=False))  # сбой сборки
    monkeypatch.setattr(handlers, "_safe_delete", AsyncMock())

    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.message.chat.type = ChatType.PRIVATE
    cb.answer = AsyncMock()
    cb.message.bot = AsyncMock()
    cb.message.chat.id = 999
    cb.message.message_id = 42

    await handlers.backup_export_cb(cb)

    cb.message.bot.send_message.assert_awaited_once()  # пользователю ушла ошибка
    assert "❌" in cb.message.bot.send_message.call_args.args[1]


@pytest.mark.asyncio
async def test_full_export_reports_safe_limit_reason(backup_env, monkeypatch):
    monkeypatch.setattr("handlers.send_backup", AsyncMock(side_effect=BackupLimitError("Суммарный размер архива больше 32 МиБ")))
    monkeypatch.setattr("handlers._safe_delete", AsyncMock())
    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.message.chat.type = ChatType.PRIVATE
    cb.answer = AsyncMock()
    cb.message.bot = AsyncMock()
    cb.message.chat.id = 999

    await handlers.backup_export_cb(cb)

    cb.message.bot.send_message.assert_awaited_once_with(999, "❌ Архив для диагностики не создан: Суммарный размер архива больше 32 МиБ.")


# ─────────────────────────────────────────────────────────────
#  backup_import_cb — кнопка «📥 Импорт»: вход в FSM ожидания .zip
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backup_import_rejects_non_owner(backup_env):
    state = AsyncMock()
    cb = MagicMock()
    cb.from_user.id = 1                        # не владелец (OWNER_ID=999 в backup_env)
    cb.answer = AsyncMock()

    await handlers.backup_import_cb(cb, state)

    cb.answer.assert_awaited_once()
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    state.set_state.assert_not_awaited()       # в FSM не вошли
    cb.message.edit_text.assert_not_called()   # промпт не трогали


@pytest.mark.asyncio
async def test_backup_import_enters_fsm_and_stores_prompt(backup_env):
    state = AsyncMock()
    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.message.chat.type = ChatType.PRIVATE
    cb.answer = AsyncMock()
    cb.message.edit_text = AsyncMock(return_value=MagicMock(message_id=555))

    await handlers.backup_import_cb(cb, state)

    cb.answer.assert_awaited_once()            # тихий ack (без show_alert)
    state.set_state.assert_awaited_once_with(handlers.BackupStates.waiting_import_file)
    cb.message.edit_text.assert_awaited_once()  # промпт-сообщение переписано
    assert "доступных обновлениях" in cb.message.edit_text.call_args.args[0]
    state.update_data.assert_awaited_once_with(prompt_msg_id=555)  # id промпта сохранён для чистки


@pytest.mark.asyncio
async def test_backup_import_edit_rejection_does_not_enter_fsm(backup_env):
    state = AsyncMock()
    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.message.chat.type = ChatType.PRIVATE
    cb.answer = AsyncMock()
    cb.message.photo = []
    cb.message.edit_text = AsyncMock(side_effect=TelegramBadRequest(
        method=MagicMock(),
        message="message can't be edited",
    ))

    await handlers.backup_import_cb(cb, state)

    cb.answer.assert_awaited_once_with(
        "Не удалось открыть импорт. Попробуй ещё раз.",
        show_alert=True,
    )
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()


# ─────────────────────────────────────────────────────────────
#  backup_receive — приём .zip и восстановление (оркестрация)
# ─────────────────────────────────────────────────────────────

def _import_message(
    *,
    owner=True,
    with_doc=True,
    file_name="backup.zip",
    file_size=1_024,
):
    """Мок Message для backup_receive. bot — AsyncMock (download awaitable),
    answer — AsyncMock. Реальное состояние не трогаем: I/O-границы мокаются в тесте."""
    msg = MagicMock()
    msg.from_user.id = handlers.OWNER_ID if owner else 1
    if with_doc:
        msg.document.file_name = file_name
        msg.document.file_size = file_size
    else:
        msg.document = None
    msg.chat.id = 999
    msg.chat.type = ChatType.PRIVATE
    msg.message_id = 77
    msg.answer = AsyncMock()
    msg.bot = AsyncMock()
    return msg


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", [ChatType.GROUP, ChatType.SUPERGROUP])
async def test_backup_receive_rejects_non_private_before_download(backup_env, monkeypatch, chat_type):
    restore = AsyncMock(return_value={"restored": [], "skipped": []})
    deleted = AsyncMock()
    monkeypatch.setattr("handlers.restore_backup_zip", restore)
    monkeypatch.setattr("handlers._safe_delete", deleted)
    state = AsyncMock()
    state.get_data.return_value = {"prompt_msg_id": 55}
    message = _import_message()
    message.chat.type = chat_type

    await handlers.backup_receive(message, state)

    message.bot.download.assert_not_awaited()
    restore.assert_not_awaited()
    deleted.assert_not_awaited()
    message.answer.assert_not_awaited()
    assert state.mock_calls == []


@pytest.mark.asyncio
async def test_backup_receive_rejects_non_owner(backup_env, monkeypatch):
    restore = MagicMock()
    monkeypatch.setattr(handlers, "restore_backup_zip", restore)
    state = AsyncMock()
    msg = _import_message(owner=False)

    await handlers.backup_receive(msg, state)

    msg.answer.assert_not_awaited()      # чужому — молчим (owner-only команда)
    restore.assert_not_called()          # архив не трогали
    state.clear.assert_not_awaited()     # чужой FSM не сбрасываем


@pytest.mark.asyncio
@pytest.mark.parametrize("with_doc, file_name", [
    (False, None),          # вложения нет вовсе
    (True, "state.txt"),    # не .zip
    (True, "backup.zip.exe"),  # .zip лишь в середине имени — не суффикс
])
async def test_backup_receive_rejects_non_zip(backup_env, monkeypatch, with_doc, file_name):
    restore = MagicMock()
    monkeypatch.setattr(handlers, "restore_backup_zip", restore)
    state = AsyncMock()
    msg = _import_message(with_doc=with_doc, file_name=file_name)

    await handlers.backup_receive(msg, state)

    msg.answer.assert_awaited_once()     # подсказали, что ждём .zip
    assert "📎" in msg.answer.call_args.args[0]
    restore.assert_not_called()          # до восстановления не дошли


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_size",
    [None, handlers.IMPORT_DOCUMENT_MAX_BYTES + 1],
    ids=["unknown", "oversized"],
)
async def test_backup_receive_rejects_invalid_size_before_download(
    backup_env,
    monkeypatch,
    file_size,
):
    restore = AsyncMock()
    monkeypatch.setattr(handlers, "restore_backup_zip", restore)
    state = AsyncMock()
    msg = _import_message(file_size=file_size)

    await handlers.backup_receive(msg, state)

    msg.bot.download.assert_not_awaited()
    restore.assert_not_awaited()
    state.clear.assert_not_awaited()
    assert "20 МиБ" in msg.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_backup_receive_accepts_exact_document_size(backup_env, monkeypatch):
    restore = AsyncMock(return_value={"restored": ["update_state.json"], "skipped": []})
    monkeypatch.setattr(handlers, "restore_backup_zip", restore)
    monkeypatch.setattr(handlers, "_safe_delete", AsyncMock())
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    msg = _import_message(file_size=handlers.IMPORT_DOCUMENT_MAX_BYTES)

    await handlers.backup_receive(msg, state)

    msg.bot.download.assert_awaited_once()
    restore.assert_awaited_once()
    assert "доступных обновлениях восстановлены" in msg.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_backup_receive_download_failure(backup_env, monkeypatch):
    restore = AsyncMock()
    monkeypatch.setattr(handlers, "restore_backup_zip", restore)
    monkeypatch.setattr(handlers, "_safe_delete", AsyncMock())
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"prompt_msg_id": 55})
    msg = _import_message()
    msg.bot.download = AsyncMock(side_effect=RuntimeError("boom <net> & fail"))

    await handlers.backup_receive(msg, state)

    msg.answer.assert_awaited_once()
    text = msg.answer.call_args.args[0]
    assert text == "❌ Не удалось скачать архив. Попробуй ещё раз."
    restore.assert_not_awaited()          # битую загрузку в restore не потащили


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    ValueError("битый <b>zip</b>-архив & мусор"),
    QuarterDeliveryStateError("progress_index"),
    QuarterDeliveryStateError("plan_integrity"),
    EventTimeStateError("event_time_structure"),
])
async def test_backup_receive_restore_value_error(backup_env, monkeypatch, error, caplog):
    restore = AsyncMock(side_effect=error)
    monkeypatch.setattr(handlers, "restore_backup_zip", restore)
    monkeypatch.setattr(handlers, "_safe_delete", AsyncMock())
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"prompt_msg_id": 55})
    msg = _import_message()
    msg.bot.download = AsyncMock()

    await handlers.backup_receive(msg, state)

    restore.assert_awaited_once()
    msg.answer.assert_awaited_once()
    text = msg.answer.call_args.args[0]
    assert text == "❌ Архив не восстановлен. Проверь формат и целостность файла."
    assert str(error) in caplog.text


@pytest.mark.asyncio
async def test_backup_receive_success_reports_and_refreshes(backup_env, monkeypatch):
    monkeypatch.setattr(handlers, "restore_backup_zip", AsyncMock(return_value={
        "restored": ["subscribers.json", "stats_current.json"],
        "skipped": ["junk.txt"],
    }))
    deleted = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_delete", deleted)
    subs = MagicMock(return_value={1: "a", 2: "b"})
    monkeypatch.setattr(handlers, "load_subscribers", subs)
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"prompt_msg_id": 55})
    msg = _import_message()
    msg.bot.download = AsyncMock()

    manager = MagicMock()
    manager.attach_mock(state.clear, "clear")
    manager.attach_mock(handlers.restore_backup_zip, "restore")

    await handlers.backup_receive(msg, state)

    state.clear.assert_awaited_once()                    # FSM закрыт до восстановления
    assert (manager.mock_calls.index(call.clear())
            < manager.mock_calls.index(call.restore(ANY)))  # clear ДО restore, не наоборот
    subs.assert_called_once()                            # refresh подписчиков (subscribers.json в restored)
    deleted.assert_any_await(msg.bot, msg.chat.id, 55)   # промпт убран
    deleted.assert_any_await(msg.bot, msg.chat.id, 77)   # само сообщение с архивом убрано
    msg.answer.assert_awaited_once()
    text = msg.answer.call_args.args[0]
    assert "✅" in text and "👥" in text                 # отчёт + строка про подписчиков
    assert "Пропущено" in text                           # skipped отражён


@pytest.mark.asyncio
async def test_backup_receive_success_without_subscribers_skips_refresh(backup_env, monkeypatch):
    monkeypatch.setattr(handlers, "restore_backup_zip", AsyncMock(return_value={
        "restored": ["stats_current.json"],
        "skipped": [],
    }))
    deleted = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_delete", deleted)
    subs = MagicMock(return_value={})
    monkeypatch.setattr(handlers, "load_subscribers", subs)
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})          # промпта нет — ветка без чистки промпта
    msg = _import_message()
    msg.bot.download = AsyncMock()

    await handlers.backup_receive(msg, state)

    subs.assert_not_called()                             # subscribers.json не восстановлен → refresh не нужен
    deleted.assert_awaited_once_with(msg.bot, msg.chat.id, 77)  # архив убран; промпта не было — второго _safe_delete нет
    msg.answer.assert_awaited_once()
    text = msg.answer.call_args.args[0]
    assert "✅" in text and "👥" not in text             # отчёт без строки про подписчиков
    assert "Пропущено" not in text                       # skipped пуст
