# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Безопасный GitHub version cache, renderer и portable-уведомления."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from html import escape
from urllib.parse import urlsplit

import aiohttp
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from build_info import (
    APP_VERSION,
    HAS_RELEASE_INFO,
    LATEST_RELEASE_API,
    MAIN_VERSION_API,
    RELEASES_URL,
    REPOSITORY_URL,
    semver_tuple,
)
from config import (
    OWNER_ID,
    log,
)
from project_meta import PROJECT_SUMMARY
from runtime import IS_FROZEN
from runtime_status import (
    RuntimeSnapshot,
    get_runtime_snapshot,
)
from storage import (
    load_update_state,
    restorable_state_transaction,
    save_update_state,
)
from utils import (
    _parse_iso_utc,
    _utcnow,
    h,
)

UPDATE_CHECK_INTERVAL = timedelta(hours=24)
UPDATE_INITIAL_DELAY = 5.0
UPDATE_HTTP_TIMEOUT = 10.0
_PROJECT_VERSION_LINE_RE = re.compile(
    r'^PROJECT_VERSION = "(v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"$',
    re.MULTILINE,
)
_PROJECT_VERSION_NAME_RE = re.compile(r"^PROJECT_VERSION\b", re.MULTILINE)


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    url: str


def release_checks_available() -> bool:
    """Можно ли обратиться к GitHub Releases для этой версии проекта."""
    return HAS_RELEASE_INFO and bool(LATEST_RELEASE_API)


def update_checks_enabled() -> bool:
    """Можно ли фоново обновлять сведения о версиях в текущей сборке."""
    return release_checks_available()


def _safe_http_url(value: object) -> str | None:
    """Принять только абсолютную HTTP(S)-ссылку без credentials и пробелов."""
    if (
        not isinstance(value, str)
        or not value
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
    except (TypeError, ValueError):
        return None
    return value


def build_version_keyboard(
    release_url: str | None = None,
    *,
    include_refresh: bool = False,
) -> InlineKeyboardMarkup | None:
    """Собрать кнопки репозитория и последнего релиза."""
    link_buttons = []
    repository_url = _safe_http_url(REPOSITORY_URL)
    if repository_url:
        link_buttons.append(InlineKeyboardButton(text="О проекте", url=repository_url))
    target_release = _safe_http_url(release_url) or _safe_http_url(RELEASES_URL)
    if target_release:
        link_buttons.append(InlineKeyboardButton(text="Версия для Windows", url=target_release))
    rows = [link_buttons] if link_buttons else []
    if include_refresh:
        rows.append([
            InlineKeyboardButton(
                text="Обновить сведения",
                callback_data="version:refresh",
            ),
        ])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _expected_windows_asset(payload: dict) -> bool:
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return False
    return any(
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"].lower().endswith("-windows-x64.zip")
        for item in assets
    )


async def fetch_latest_release(session: aiohttp.ClientSession | None = None) -> ReleaseInfo | None:
    """Получить последний полный релиз; любая ошибка сети или тела даёт None."""
    if not release_checks_available():
        return None
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    assert session is not None
    try:
        timeout = aiohttp.ClientTimeout(total=UPDATE_HTTP_TIMEOUT)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"ShikiUpdatesBot/{APP_VERSION}",
        }
        async with session.get(LATEST_RELEASE_API, headers=headers, timeout=timeout) as response:
            if response.status != 200:
                log.warning("Проверка GitHub Release: HTTP %s", response.status)
                return None
            payload = await response.json()
        if not isinstance(payload, dict) or not _expected_windows_asset(payload):
            log.warning("Проверка GitHub Release: в релизе нет Windows x64 ZIP.")
            return None
        version = payload.get("tag_name")
        if not isinstance(version, str) or semver_tuple(version) is None:
            log.warning("Проверка GitHub Release: неподдерживаемый тег %r.", version)
            return None
        return ReleaseInfo(version=version, url=RELEASES_URL)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as e:
        log.warning("Проверка GitHub Release не удалась: %s", e)
        return None
    finally:
        if own_session:
            await session.close()


def parse_main_version(source: object) -> str | None:
    """Извлечь одно строгое присваивание версии без исполнения Python-кода."""
    if not isinstance(source, str):
        return None
    normalized = source.replace("\r\n", "\n")
    if len(_PROJECT_VERSION_NAME_RE.findall(normalized)) != 1:
        return None
    matches = _PROJECT_VERSION_LINE_RE.findall(normalized)
    if len(matches) != 1 or semver_tuple(matches[0]) is None:
        return None
    return matches[0]


async def fetch_main_version(session: aiohttp.ClientSession | None = None) -> str | None:
    """Получить строгую PROJECT_VERSION из project_meta.py ветки main."""
    if not release_checks_available() or not MAIN_VERSION_API:
        return None
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    assert session is not None
    try:
        timeout = aiohttp.ClientTimeout(total=UPDATE_HTTP_TIMEOUT)
        headers = {
            "Accept": "application/vnd.github.raw+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"ShikiUpdatesBot/{APP_VERSION}",
        }
        async with session.get(
            MAIN_VERSION_API,
            params={"ref": "main"},
            headers=headers,
            timeout=timeout,
        ) as response:
            if response.status != 200:
                log.warning("Проверка версии main: HTTP %s", response.status)
                return None
            source = await response.text()
        version = parse_main_version(source)
        if version is None:
            log.warning("Проверка версии main: project_meta.py имеет неподдерживаемый формат.")
        return version
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        UnicodeError,
        ValueError,
        TypeError,
    ) as e:
        log.warning("Проверка версии main не удалась: %s", e)
        return None
    finally:
        if own_session:
            await session.close()


def _checked_recently(state: dict) -> bool:
    checked = _parse_iso_utc(state.get("last_checked_at"))
    if checked is None:
        return False
    age = _utcnow() - checked
    return timedelta(0) <= age < UPDATE_CHECK_INTERVAL


def _is_newer(latest: str | None) -> bool:
    current_tuple = semver_tuple(APP_VERSION)
    latest_tuple = semver_tuple(latest or "")
    return bool(current_tuple and latest_tuple and latest_tuple > current_tuple)


def _format_checked_at(value) -> str:
    """Показать сохранённый ISO timestamp как короткое время UTC."""
    if not isinstance(value, str) or not value:
        return "ещё не выполнялось"
    checked = _parse_iso_utc(value)
    if checked is None:
        return "время неизвестно"
    return checked.strftime("%d.%m.%Y, %H:%M UTC")


async def refresh_update_state(*, force: bool = False) -> dict:
    """Независимо обновить кэш main и Windows-релиза без исключений наружу."""
    state = load_update_state()
    if not release_checks_available():
        return state
    if not force and (not update_checks_enabled() or _checked_recently(state)):
        return state
    checked_at = _utcnow().isoformat()
    main_result, release_result = await asyncio.gather(
        fetch_main_version(),
        fetch_latest_release(),
        return_exceptions=True,
    )
    if isinstance(main_result, asyncio.CancelledError):
        raise main_result
    if isinstance(main_result, Exception):
        log.warning("Непредвиденная ошибка проверки версии main: %s", main_result)
        main_result = None
    if isinstance(release_result, asyncio.CancelledError):
        raise release_result
    if isinstance(release_result, Exception):
        log.warning("Непредвиденная ошибка проверки Windows-релиза: %s", release_result)
        release_result = None
    async with restorable_state_transaction():
        state = load_update_state()
        stored_checked_at = _parse_iso_utc(state.get("last_checked_at"))
        incoming_checked_at = _parse_iso_utc(checked_at)
        if (
            stored_checked_at is not None
            and incoming_checked_at is not None
            and stored_checked_at > incoming_checked_at
        ):
            return state
        state["last_checked_at"] = checked_at
        if isinstance(main_result, str):
            state["latest_main_version"] = main_result
        if isinstance(release_result, ReleaseInfo):
            state["latest_version"] = release_result.version
            state["release_url"] = release_result.url
        save_update_state(state)
    return state


def _nonnegative_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value < 0:
            return None
    except (OverflowError, TypeError, ValueError):
        return None
    return value


def _format_uptime(value: object) -> str:
    number = _nonnegative_number(value)
    if number is None:
        return "неизвестно"
    seconds = int(number)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    prefix = f"{days} д. " if days else ""
    return f"{prefix}{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_wall_time(value: object, *, missing: str) -> str:
    if value is None:
        return missing
    number = _nonnegative_number(value)
    if number is None:
        return "неизвестно"
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).strftime(
            "%d.%m.%Y, %H:%M UTC"
        )
    except (OSError, OverflowError, ValueError):
        return "неизвестно"


def _cached_version(value: object, *, missing: str) -> str:
    """Не отображать повреждённые значения восстановленного version cache."""
    if (
        isinstance(value, str)
        and value.startswith("v")
        and semver_tuple(value) is not None
    ):
        return value
    return missing


def build_version_text(
    state: dict,
    *,
    runtime: RuntimeSnapshot | None = None,
    last_backup_at: object = None,
) -> str:
    """Собрать общий безопасный HTML для /info и /version."""
    if not isinstance(state, dict):
        state = {}
    runtime = runtime or get_runtime_snapshot()
    latest_main = _cached_version(
        state.get("latest_main_version"),
        missing="неизвестна",
    )
    latest_release = _cached_version(
        state.get("latest_version"),
        missing="неизвестна",
    )
    checked = _format_checked_at(state.get("last_checked_at"))
    launch_mode = (
        "portable-версия для Windows"
        if IS_FROZEN
        else "Python/source или Docker"
    )
    repository_url = _safe_http_url(REPOSITORY_URL)
    repository = ""
    if repository_url:
        repository = (
            f'<a href="{escape(repository_url, quote=True)}">'
            "Сайт проекта и документация</a>"
        )
    polling = "работает" if runtime.polling_active else "остановлена"
    repository_line = f"\n{repository}" if repository else ""
    return (
        "🎌 <b>ShikiUpdatesBot</b>\n"
        f"{h(PROJECT_SUMMARY)}\n\n"
        "<b>Версии</b>\n"
        f"Версия этого бота: <code>{h(APP_VERSION)}</code>\n"
        f"Актуальная версия проекта: <code>{h(latest_main)}</code>\n"
        f"Последняя версия для Windows: <code>{h(latest_release)}</code>\n"
        f"Способ запуска: {launch_mode}\n"
        f"Последнее обновление сведений: {h(checked)}\n\n"
        "<b>Состояние</b>\n"
        f"Работает: {_format_uptime(runtime.uptime_seconds)}\n"
        "Последнее полное обновление данных: "
        f"{_format_wall_time(runtime.last_full_sync_at, missing='ещё не завершалось')}\n"
        "Последняя плановая резервная копия: "
        f"{_format_wall_time(last_backup_at, missing='ещё не выполнялась')}\n"
        f"Проверка новых событий: {polling}\n\n"
        "Copyright © 2026 WorgaNomoR.\n"
        "ShikiUpdatesBot распространяется на условиях GNU General Public License "
        f"версии 3 или более поздней.{repository_line}"
    )


async def check_and_notify_update(bot: Bot) -> None:
    """Один раз уведомить владельца о новой версии после успешной доставки."""
    state = await refresh_update_state()
    if not IS_FROZEN:
        return
    latest = state.get("latest_version")
    if not _is_newer(latest) or state.get("last_notified_version") == latest:
        return
    text = (
        "🆕 <b>Доступна новая версия ShikiUpdatesBot</b>\n\n"
        f"Установлена: <code>{h(APP_VERSION)}</code>\n"
        f"Последняя: <code>{h(latest)}</code>\n\n"
        "Останови бот и замени только <code>ShikiUpdatesBot.exe</code>. "
        "Файлы <code>.env</code>, <code>data/</code> и <code>logs/</code> останутся на месте."
    )
    try:
        await bot.send_message(
            OWNER_ID,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=build_version_keyboard(state.get("release_url")),
        )
    except Exception as e:
        log.warning("Уведомление об обновлении не доставлено: %s", e)
        return
    async with restorable_state_transaction():
        state = load_update_state()
        if state.get("latest_version") != latest:
            return
        state["last_notified_version"] = latest
        save_update_state(state)


async def update_loop(bot: Bot) -> None:
    await asyncio.sleep(UPDATE_INITIAL_DELAY)
    while True:
        try:
            await check_and_notify_update(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Непредвиденная ошибка цикла обновлений: %s", e)
        await asyncio.sleep(UPDATE_CHECK_INTERVAL.total_seconds())


_update_task: asyncio.Task | None = None


def _on_update_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        log.error("Цикл проверки обновлений остановился: %s", error)


def start_update_loop(bot: Bot) -> bool:
    """Идемпотентно запустить суточное обновление сведений о версиях."""
    global _update_task
    if not update_checks_enabled():
        return False
    if _update_task is not None and not _update_task.done():
        return False
    _update_task = asyncio.create_task(update_loop(bot))
    _update_task.add_done_callback(_on_update_done)
    return True
