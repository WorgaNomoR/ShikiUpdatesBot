# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Shikimori History Watcher Bot
Следит за историей и избранным пользователя на Shikimori
и отправляет весёлые уведомления в Telegram.
"""

import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
)

from backup import (
    _shutdown_backup,
    )
from config import (
    BOT_TOKEN,
    CHECK_INTERVAL,
    DISPLAY_NAME,
)
from handlers import (
    BackupStates,
    BroadcastStates,
    backup_close_cb,
    backup_export_cb,
    backup_import_cb,
    backup_receive,
    broadcast_cancel_cb,
    broadcast_confirm_cb,
    broadcast_receive,
    cmd_backup,
    cmd_broadcast,
    cmd_cancel,
    cmd_favs,
    cmd_start,
    cmd_stats,
    cmd_status,
    cmd_stop,
    cmd_subs,
    probe_owner_and_start,
    stats_menu_cb,
)
from healthcheck import start_health_server

# ═══════════════════════════════════════════════════════════════
#  FSM — состояния для команды /broadcast
# ═══════════════════════════════════════════════════════════════


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())

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

    # FSM-обработчики для /broadcast
    dp.message.register(broadcast_receive, BroadcastStates.waiting_content)
    dp.callback_query.register(broadcast_confirm_cb, F.data == "broadcast_send",   BroadcastStates.waiting_confirm)
    dp.callback_query.register(broadcast_cancel_cb,  F.data == "broadcast_cancel", BroadcastStates.waiting_confirm)

    # FSM-обработчик и кнопки для /backup
    dp.message.register(backup_receive, BackupStates.waiting_import_file)
    dp.callback_query.register(backup_export_cb, F.data == "backup:export")
    dp.callback_query.register(backup_import_cb, F.data == "backup:import")
    dp.callback_query.register(backup_close_cb,  F.data == "backup:close")

    # Кнопки меню /stats (callback_data вида "stats:<ключ>")
    dp.callback_query.register(stats_menu_cb, F.data.startswith("stats:"))

    # Публичные команды в меню "/" — команды владельца не показываем
    await bot.set_my_commands([
        BotCommand(command="start",  description="Подписаться на уведомления 🥳"),
        BotCommand(command="status", description=f"Что сейчас смотрит и читает {DISPLAY_NAME} 👀"),
        BotCommand(command="stats",  description="Статистика: квартал или всё время 📊"),
        BotCommand(command="favs",   description="Избранное ❤️"),
        BotCommand(command="stop",   description="Отписаться 😢"),
    ])

    # polling_loop работает параллельно как фоновая задача
    # Healthcheck-сервер (для хостингов с обязательным портом + watchdog)
    await start_health_server(check_interval=CHECK_INTERVAL)

    # Финальный бэкап при остановке (aiogram ловит SIGTERM/SIGINT → emit_shutdown)
    dp.shutdown.register(_shutdown_backup)

    # owner-reachability gate: пробуем достучаться до владельца. Доставилось →
    # запускаем фоновый цикл; нет → апдейт-поллинг всё равно жив, /start добудит.
    await probe_owner_and_start(bot)

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
