# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Shikimori History Watcher Bot
Следит за историей и избранным пользователя на Shikimori
и отправляет весёлые уведомления в Telegram.
"""

import asyncio

from aiogram import (
    Bot,
    Dispatcher,
    F,
)
from aiogram.filters import (
    Command,
    StateFilter,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from access_control import AccessControlMiddleware
from backup import _shutdown_backup
from config import (
    BOT_TOKEN,
    CHECK_INTERVAL,
    DISPLAY_NAME,
    log,
)
from fact_bank import reload_fact_bank
from handlers import (
    FACTS_APPLY_CALLBACK_PREFIX,
    FACTS_ASK_CLEAR_CALLBACK_PREFIX,
    FACTS_CONFIRM_CLEAR_CALLBACK_PREFIX,
    BackupStates,
    BroadcastStates,
    FactsStates,
    backup_close_cb,
    backup_export_cb,
    backup_import_cb,
    backup_receive,
    broadcast_cancel_cb,
    broadcast_confirm_cb,
    broadcast_receive,
    cmd_backup,
    cmd_block,
    cmd_blocklist,
    cmd_broadcast,
    cmd_cancel,
    cmd_fact,
    cmd_facts,
    cmd_favs,
    cmd_info,
    cmd_inline_search,
    cmd_pick,
    cmd_start,
    cmd_stats,
    cmd_status,
    cmd_stop,
    cmd_subs,
    cmd_unblock,
    cmd_useralerts,
    cmd_version,
    fact_next_cb,
    facts_apply_cb,
    facts_ask_clear_cb,
    facts_cancel_cb,
    facts_close_cb,
    facts_confirm_clear_cb,
    facts_download_cb,
    facts_example_cb,
    facts_receive,
    facts_upload_cb,
    pick_menu_cb,
    probe_owner_and_start,
    stats_menu_cb,
    version_refresh_cb,
)
from healthcheck import start_health_server
from runtime import (
    IS_FROZEN,
    WindowsConsoleCloseGuard,
)
from storage import (
    BlockedUsersStateError,
    reconcile_blocked_subscribers,
)
from updates import start_update_loop
from user_registry import UserRegistryMiddleware


async def main() -> None:
    # Перечитываем дополнительный банк перед запуском обработчиков. Ошибочный
    # файл уже деградирует к неизменяемой базе внутри fact_bank.
    reload_fact_bank()

    # До любых Telegram-обработчиков и фоновых задач восстанавливаем инвариант,
    # если процесс прервался между публикацией двух access-control файлов.
    try:
        await reconcile_blocked_subscribers()
    except BlockedUsersStateError:
        # Центральная граница продолжит запрещать доступ всем, кроме владельца;
        # владелец сможет восстановить корректное состояние через /backup.
        log.error(
            "Не удалось проверить подписчиков по списку блокировок; "
            "доступ обычных пользователей останется закрыт."
        )

    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())

    # Глобальная проверка списка блокировок — первый проектный middleware.
    dp.update.outer_middleware(AccessControlMiddleware())

    # Только сопоставленные message/callback регистрируются после общего gate.
    user_registry_middleware = UserRegistryMiddleware()
    dp.message.middleware(user_registry_middleware)
    dp.callback_query.middleware(user_registry_middleware)

    # Регистрируем команды
    dp.message.register(cmd_start,     Command("start"))
    dp.message.register(cmd_stop,      Command("stop"))
    dp.message.register(cmd_subs,      Command("subs"))
    dp.message.register(cmd_backup,    Command("backup"))
    dp.message.register(cmd_status,    Command("status"))
    dp.message.register(cmd_broadcast, Command("broadcast"))
    dp.message.register(cmd_cancel,    Command("cancel"))
    dp.message.register(cmd_stats,     Command("stats"))
    dp.message.register(cmd_favs,      Command("favs"))
    dp.message.register(cmd_fact,      Command("fact"))
    dp.message.register(cmd_facts,     Command("facts"))
    dp.message.register(cmd_pick,      Command("pick"))
    dp.message.register(cmd_info,      Command("info"))
    dp.message.register(cmd_version,   Command("version"))
    dp.message.register(cmd_block,     Command("block"))
    dp.message.register(cmd_unblock,   Command("unblock"))
    dp.message.register(cmd_blocklist, Command("blocklist"))
    dp.message.register(cmd_useralerts, Command("useralerts"))
    dp.inline_query.register(cmd_inline_search)

    # FSM-обработчики для /broadcast
    dp.message.register(broadcast_receive, BroadcastStates.waiting_content)
    dp.callback_query.register(broadcast_confirm_cb, F.data == "broadcast_send",   BroadcastStates.waiting_confirm)
    dp.callback_query.register(broadcast_cancel_cb,  F.data == "broadcast_cancel", BroadcastStates.waiting_confirm)

    # FSM-обработчик и кнопки для /backup
    dp.message.register(backup_receive, BackupStates.waiting_import_file)
    dp.callback_query.register(backup_export_cb, F.data == "backup:export")
    dp.callback_query.register(backup_import_cb, F.data == "backup:import")
    dp.callback_query.register(backup_close_cb,  F.data == "backup:close")

    # FSM и кнопки скрытого owner-only управления дополнительными фактами
    dp.message.register(
        facts_receive,
        StateFilter(
            FactsStates.waiting_upload_file,
            FactsStates.waiting_apply_confirmation,
        ),
    )
    dp.callback_query.register(facts_upload_cb, F.data == "facts:upload")
    dp.callback_query.register(facts_download_cb, F.data == "facts:download")
    dp.callback_query.register(facts_example_cb, F.data == "facts:example")
    dp.callback_query.register(facts_cancel_cb, F.data == "facts:cancel")
    dp.callback_query.register(facts_close_cb, F.data == "facts:close")
    dp.callback_query.register(
        facts_apply_cb,
        F.data.startswith(FACTS_APPLY_CALLBACK_PREFIX),
    )
    dp.callback_query.register(
        facts_ask_clear_cb,
        F.data.startswith(FACTS_ASK_CLEAR_CALLBACK_PREFIX),
    )
    dp.callback_query.register(
        facts_confirm_clear_cb,
        F.data.startswith(FACTS_CONFIRM_CLEAR_CALLBACK_PREFIX),
    )

    # Кнопки меню /stats (callback_data вида "stats:<ключ>")
    dp.callback_query.register(stats_menu_cb, F.data.startswith("stats:"))

    # Кнопка обновления сведений о версиях
    dp.callback_query.register(version_refresh_cb, F.data == "version:refresh")

    # Локальная ротация публичных фактов
    dp.callback_query.register(fact_next_cb, F.data.startswith("fact:next:"))

    # Скрытый owner-only локальный выбор из planned snapshot
    dp.callback_query.register(
        pick_menu_cb,
        F.data.startswith("pick:"),
    )

    # Публичные команды в меню "/" — команды владельца не показываем
    await bot.set_my_commands([
        BotCommand(command="start",  description="Подписаться на уведомления 🥳"),
        BotCommand(command="status", description=f"Что сейчас смотрит и читает {DISPLAY_NAME} 👀"),
        BotCommand(command="stats",  description="Статистика: квартал или всё время 📊"),
        BotCommand(command="favs",   description="Избранное ❤️"),
        BotCommand(command="fact",   description="Интересный факт 💡"),
        BotCommand(command="info",   description="О боте ℹ️"),
        BotCommand(command="stop",   description="Отписаться 😢"),
    ])

    # Healthcheck и финальный бэкап нужны source/Docker-хостингу. Portable exe
    # хранит данные постоянно и не должен слать архив при каждом выключении ПК.
    if not IS_FROZEN:
        await start_health_server(check_interval=CHECK_INTERVAL)
        dp.shutdown.register(_shutdown_backup)

    # owner-reachability gate: пробуем достучаться до владельца. Доставилось →
    # запускаем фоновый цикл; нет → апдейт-поллинг всё равно жив, /start добудит.
    await probe_owner_and_start(bot)
    start_update_loop(bot)

    close_guard = None
    if IS_FROZEN:
        loop = asyncio.get_running_loop()

        def request_stop() -> None:
            asyncio.create_task(dp.stop_polling())

        close_guard = WindowsConsoleCloseGuard(loop, request_stop)
        try:
            close_guard.install()
        except OSError as e:
            log.warning("Не удалось установить обработчик закрытия консоли: %s", e)

    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "inline_query"],
        )
    finally:
        if close_guard is not None:
            close_guard.complete()
            close_guard.uninstall()


if __name__ == "__main__":
    asyncio.run(main())
