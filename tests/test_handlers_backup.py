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
from unittest.mock import ANY, AsyncMock, MagicMock, call

import pytest

import handlers

# ─────────────────────────────────────────────────────────────
#  Команда /backup и интеграция в под/отписку
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_backup_rejects_non_owner(backup_env):
    msg = MagicMock()
    msg.from_user.id = 1  # не владелец
    msg.answer = AsyncMock()
    await handlers.cmd_backup(msg)
    msg.answer.assert_awaited_once()
    # меню не показано (нет reply_markup)
    assert "reply_markup" not in msg.answer.call_args.kwargs


@pytest.mark.asyncio
async def test_cmd_backup_owner_shows_menu(backup_env):
    msg = MagicMock()
    msg.from_user.id = handlers.OWNER_ID
    msg.reply = AsyncMock()
    await handlers.cmd_backup(msg)
    kwargs = msg.reply.call_args.kwargs
    assert "reply_markup" in kwargs   # инлайн-меню есть


# ─────────────────────────────────────────────────────────────
#  Кнопка «Закрыть» в меню /backup (паттерн как у /stats)
# ─────────────────────────────────────────────────────────────

def test_backup_menu_has_close_button():
    kb = handlers._backup_menu_kb()
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "backup:close" in datas


@pytest.mark.asyncio
async def test_backup_close_deletes_menu_and_command(backup_env):
    cmd_msg = MagicMock()
    cmd_msg.delete = AsyncMock()
    menu = MagicMock()
    menu.delete = AsyncMock()
    menu.reply_to_message = cmd_msg
    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.message = menu
    cb.answer = AsyncMock()

    await handlers.backup_close_cb(cb, AsyncMock())

    menu.delete.assert_awaited_once()
    cmd_msg.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_backup_close_handles_missing_message(backup_env):
    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.message = None
    cb.answer = AsyncMock()
    await handlers.backup_close_cb(cb, AsyncMock())   # не должно бросить
    cb.answer.assert_awaited_once()   # ack колбэка отправлен даже без message


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
async def test_backup_close_clears_fsm_state(backup_env):
    state = AsyncMock()
    menu = MagicMock()
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
async def test_backup_export_rejects_non_owner(backup_env, monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(handlers, "send_backup", sent)

    cb = MagicMock()
    cb.from_user.id = 1                       # не владелец (OWNER_ID=999 в backup_env)
    cb.answer = AsyncMock()

    await handlers.backup_export_cb(cb)

    cb.answer.assert_awaited_once()
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    sent.assert_not_awaited()                 # архив НЕ собирали


@pytest.mark.asyncio
async def test_backup_export_owner_sends_archive(backup_env, monkeypatch):
    sent = AsyncMock(return_value=True)       # send_backup успешен
    monkeypatch.setattr(handlers, "send_backup", sent)
    deleted = AsyncMock()
    monkeypatch.setattr(handlers, "_safe_delete", deleted)

    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.answer = AsyncMock()
    cb.message.bot = AsyncMock()
    cb.message.chat.id = 999
    cb.message.message_id = 42

    await handlers.backup_export_cb(cb)

    sent.assert_awaited_once()                 # архив собран и отправлен
    deleted.assert_awaited_once_with(cb.message.bot, 999, 42)   # меню убрано: (bot, chat_id, message_id)
    cb.message.bot.send_message.assert_not_awaited()   # ошибки нет


@pytest.mark.asyncio
async def test_backup_export_reports_failure(backup_env, monkeypatch):
    monkeypatch.setattr(handlers, "send_backup", AsyncMock(return_value=False))  # сбой сборки
    monkeypatch.setattr(handlers, "_safe_delete", AsyncMock())

    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.answer = AsyncMock()
    cb.message.bot = AsyncMock()
    cb.message.chat.id = 999
    cb.message.message_id = 42

    await handlers.backup_export_cb(cb)

    cb.message.bot.send_message.assert_awaited_once()  # пользователю ушла ошибка
    assert "❌" in cb.message.bot.send_message.call_args.args[1]


@pytest.mark.asyncio
async def test_backup_export_propagates_send_backup_exception(backup_env, monkeypatch):
    """Контракт send_backup включает исключение (сеть/Telegram API). Обёртка
    backup_export_cb его НЕ глотает — фиксируем текущее поведение (пробрасывает)."""
    monkeypatch.setattr(handlers, "send_backup", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(handlers, "_safe_delete", AsyncMock())

    cb = MagicMock()
    cb.from_user.id = handlers.OWNER_ID
    cb.answer = AsyncMock()
    cb.message.bot = AsyncMock()
    cb.message.chat.id = 999
    cb.message.message_id = 42

    with pytest.raises(RuntimeError):
        await handlers.backup_export_cb(cb)


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
    cb.answer = AsyncMock()
    cb.message.edit_text = AsyncMock(return_value=MagicMock(message_id=555))

    await handlers.backup_import_cb(cb, state)

    cb.answer.assert_awaited_once()            # тихий ack (без show_alert)
    state.set_state.assert_awaited_once_with(handlers.BackupStates.waiting_import_file)
    cb.message.edit_text.assert_awaited_once()  # промпт-сообщение переписано
    state.update_data.assert_awaited_once_with(prompt_msg_id=555)  # id промпта сохранён для чистки


# ─────────────────────────────────────────────────────────────
#  backup_receive — приём .zip и восстановление (оркестрация)
# ─────────────────────────────────────────────────────────────

def _import_message(*, owner=True, with_doc=True, file_name="backup.zip"):
    """Мок Message для backup_receive. bot — AsyncMock (download awaitable),
    answer — AsyncMock. Реальное состояние не трогаем: I/O-границы мокаются в тесте."""
    msg = MagicMock()
    msg.from_user.id = handlers.OWNER_ID if owner else 1
    if with_doc:
        msg.document.file_name = file_name
    else:
        msg.document = None
    msg.chat.id = 999
    msg.message_id = 77
    msg.answer = AsyncMock()
    msg.bot = AsyncMock()
    return msg


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
async def test_backup_receive_download_failure(backup_env, monkeypatch):
    restore = MagicMock()
    monkeypatch.setattr(handlers, "restore_backup_zip", restore)
    monkeypatch.setattr(handlers, "_safe_delete", AsyncMock())
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"prompt_msg_id": 55})
    msg = _import_message()
    msg.bot.download = AsyncMock(side_effect=RuntimeError("boom <net> & fail"))

    await handlers.backup_receive(msg, state)

    msg.answer.assert_awaited_once()
    text = msg.answer.call_args.args[0]
    assert "❌" in text and "скачать" in text
    assert "&lt;net&gt;" in text and "&amp;" in text   # h(): текст исключения экранирован
    assert "<net>" not in text                         # сырой тег не протёк
    restore.assert_not_called()          # битую загрузку в restore не потащили


@pytest.mark.asyncio
async def test_backup_receive_restore_value_error(backup_env, monkeypatch):
    monkeypatch.setattr(handlers, "restore_backup_zip",
                        MagicMock(side_effect=ValueError("битый <b>zip</b>-архив & мусор")))
    monkeypatch.setattr(handlers, "_safe_delete", AsyncMock())
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"prompt_msg_id": 55})
    msg = _import_message()
    msg.bot.download = AsyncMock()

    await handlers.backup_receive(msg, state)

    msg.answer.assert_awaited_once()
    text = msg.answer.call_args.args[0]
    assert "❌" in text and "не восстановлен" in text
    assert "zip" in text and "мусор" in text          # причина проброшена пользователю
    assert "&lt;b&gt;" in text and "&amp;" in text    # h(): спецсимволы экранированы
    assert "<b>" not in text                          # сырой тег из str(e) не протёк


@pytest.mark.asyncio
async def test_backup_receive_success_reports_and_refreshes(backup_env, monkeypatch):
    monkeypatch.setattr(handlers, "restore_backup_zip", MagicMock(return_value={
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
    monkeypatch.setattr(handlers, "restore_backup_zip", MagicMock(return_value={
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
