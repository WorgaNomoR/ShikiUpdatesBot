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
    _build_backup_zip,  # noqa: F401  (re-export for test_backup; reader moved to backup)
    _is_allowed_import_member,  # noqa: F401  (re-export for test_backup; reader moved to backup)
    _shutdown_backup,
    _subscriber_link,  # noqa: F401  (re-export for test_backup; reader moved to backup)
    _valid_import_payload,  # noqa: F401  (re-export for test_backup; reader moved to backup)
    )
from config import (
    BOT_TOKEN,
    CHECK_INTERVAL,
    DISPLAY_NAME,
    log,
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
    polling_loop,
    stats_menu_cb,
)
from healthcheck import start_health_server
from messages import (
    MESSAGES,  # noqa: F401  (re-export for test_favourites; reader moved to messages)
    _avg_score_from_dist,  # noqa: F401  (re-export for test_stats; reader moved to stats)
    _fmt_kinds,  # noqa: F401  (re-export for test_stats; reader moved to messages)
    _fmt_mono_rows,  # noqa: F401  (re-export for test_stats; reader moved to messages)
    _score_dist_block,  # noqa: F401  (re-export for test_stats; reader moved to stats)
    _section_header,  # noqa: F401  (re-export for test_stats; reader moved to stats)
    _strip_html,  # noqa: F401  (re-export for test_parsers; reader moved to messages)
    _top_block,  # noqa: F401  (re-export for test_stats; reader moved to stats)
    )
from stats import (
    _KIND_RU_ANIME,  # noqa: F401  (re-export for test_stats)
    recompute_aggregates,  # noqa: F401  (re-export for test_stats)
    )
from storage import (
    _atomic_write,  # noqa: F401  (re-export for test_storage; readers moved to storage/backup)
    _empty_stats_all,  # noqa: F401  (re-export for tests; reader is storage)
    )
from utils import (
    _rel_url,  # noqa: F401  (re-export for tests; reader moved to stats)
    _safe_float,  # noqa: F401  (re-export for tests; reader moved to shiki_api)
    _safe_int,  # noqa: F401  (re-export for tests; reader moved to stats)
    _utcnow,  # noqa: F401  (re-export for test_backup; reader moved to backup)
    quarter_start,  # noqa: F401  (re-export for tests; reader moved to storage)
)

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
    _polling_task = asyncio.create_task(polling_loop(bot))

    def _on_polling_done(task: asyncio.Task) -> None:
        """Логируем если polling_loop завершился неожиданно."""
        if task.cancelled():
            log.warning("polling_loop: задача отменена.")
        elif exc := task.exception():
            log.critical(
                "polling_loop завершился с необработанной ошибкой: %s",
                exc,
                exc_info=exc,
            )

    _polling_task.add_done_callback(_on_polling_done)

    # Healthcheck-сервер (для хостингов с обязательным портом + watchdog)
    await start_health_server(check_interval=CHECK_INTERVAL)

    # Финальный бэкап при остановке (aiogram ловит SIGTERM/SIGINT → emit_shutdown)
    dp.shutdown.register(_shutdown_backup)

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
