# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Healthcheck-сервер для ShikiUpdatesBot.

Изолированный модуль: ничего не импортирует из main.py.
Связь односторонняя — main.py зовёт heartbeat() и start_health_server().

Зачем:
  • Портативность: хостинги, требующие открытый порт / healthcheck endpoint
    (Render, Fly, Cloud Run и т.п.), смогут принять этот деплой.
  • Watchdog: эндпоинт отдаёт 200 только если бот «пульсирует» — успешно
    завершил итерацию polling_loop недавно. Если пульс протух (бот завис или
    цикл умер) — отдаём 503, и хостинг перезапускает контейнер.

Как пользоваться из main.py:
    from healthcheck import heartbeat, start_health_server
    # в конце успешной итерации polling_loop:
    heartbeat()
    # в main(), рядом с запуском polling_loop:
    await start_health_server(check_interval=CHECK_INTERVAL)

Зависимости: только aiohttp (уже есть в проекте — используется aiohttp.web).
"""

import logging
import os
import time

from aiohttp import web

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  ПУЛЬС
#  Монотонное время последнего успешного цикла polling_loop.
#  _has_beaten=False = пульса ещё не было (бот только стартовал).
#  Используем time.monotonic() — не зависит от перевода системных часов.
#  Отдельный флаг вместо проверки «ts <= 0», т.к. monotonic() может быть
#  любым (в т.ч. малым) значением в зависимости от точки отсчёта ОС.
# ─────────────────────────────────────────────
_last_healthy_ts: float = 0.0
_has_beaten: bool = False

# Порог живости (секунды). Выставляется в start_health_server из CHECK_INTERVAL.
# По умолчанию 45 минут — три пропущенных цикла при интервале 15 минут.
_health_threshold: float = 45 * 60


def heartbeat() -> None:
    """
    Отметить, что бот жив: вызывается в конце каждой успешной итерации
    polling_loop. Обновляет монотонную метку времени.
    """
    global _last_healthy_ts, _has_beaten
    _last_healthy_ts = time.monotonic()
    _has_beaten = True


def _seconds_since_heartbeat() -> float | None:
    """Сколько секунд прошло с последнего пульса. None — пульса ещё не было."""
    if not _has_beaten:
        return None
    return time.monotonic() - _last_healthy_ts


def _is_healthy() -> bool:
    """
    Бот считается живым, если:
      • пульс ещё не наступал (грейс-период старта — бот поднимается, это норма), ИЛИ
      • с последнего пульса прошло меньше порога.
    Грейс-период на старте важен: первая итерация (sync_stats_all + первый
    запрос истории) может занять время, и мы не хотим, чтобы хостинг убил
    контейнер ещё до первого пульса.
    """
    elapsed = _seconds_since_heartbeat()
    if elapsed is None:
        return True
    return elapsed < _health_threshold


async def _handle_health(request: web.Request) -> web.Response:
    """GET /health — 200 если живы, 503 если пульс протух."""
    elapsed = _seconds_since_heartbeat()
    if _is_healthy():
        body = "ok" if elapsed is None else f"ok (last heartbeat {int(elapsed)}s ago)"
        return web.Response(status=200, text=body)
    log.warning(
        "Healthcheck: пульс протух (%ss назад, порог %ss) — отдаём 503.",
        int(elapsed) if elapsed is not None else "?", int(_health_threshold),
    )
    return web.Response(status=503, text="unhealthy: heartbeat stale")


async def _handle_root(request: web.Request) -> web.Response:
    """GET / — простой ответ, чтобы хостинг видел открытый порт."""
    return web.Response(status=200, text="ShikiUpdatesBot is running")


async def start_health_server(
    check_interval: int,
    misses: int = 3,
    port: int | None = None,
) -> web.AppRunner:
    """
    Поднимает HTTP-сервер для healthcheck параллельно с ботом.

    check_interval — CHECK_INTERVAL бота (сек). Порог = check_interval * misses.
    misses         — сколько пропущенных циклов допускаем до «нездоров» (по умолч. 3).
    port           — порт. Если None — берём из env PORT, иначе 8080.
                     Хостинги обычно сами прокидывают PORT.

    Возвращает AppRunner (для корректного закрытия, если понадобится).
    При любой ошибке поднятия сервера — логируем и возвращаем None, бот
    продолжает работу без healthcheck (сервер не критичен для самого бота).
    """
    global _health_threshold
    _health_threshold = max(1, check_interval) * max(1, misses)

    if port is None:
        try:
            port = int(os.environ.get("PORT", "8080"))
        except (TypeError, ValueError):
            log.warning("start_health_server: некорректный PORT в env, берём 8080.")
            port = 8080

    app = web.Application()
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/", _handle_root)

    try:
        # access_log=None — гасим access-лог aiohttp: liveness-проба бьёт по
        # /health раз в минуту, иначе это ~1440 строк "GET /health 200" в сутки.
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=port)
        await site.start()
        log.info(
            "Healthcheck-сервер запущен на :%d (порог живости %d сек).",
            port, int(_health_threshold),
        )
        return runner
    except Exception as e:
        log.error("start_health_server: не удалось поднять сервер на :%s: %s", port, e)
        return None
