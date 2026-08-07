# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Проверка GitHub Releases для уведомлений в замороженных сборках."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from html import escape

import aiohttp
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from build_info import (
    APP_VERSION,
    HAS_RELEASE_INFO,
    LATEST_RELEASE_API,
    RELEASES_URL,
    REPOSITORY_URL,
    semver_tuple,
)
from config import OWNER_ID, log
from project_meta import PROJECT_SUMMARY
from runtime import IS_FROZEN
from storage import load_update_state, save_update_state
from utils import _parse_iso_utc, _utcnow, h

UPDATE_CHECK_INTERVAL = timedelta(hours=24)
UPDATE_INITIAL_DELAY = 5.0
UPDATE_HTTP_TIMEOUT = 10.0


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    url: str


def release_checks_available() -> bool:
    """Можно ли обратиться к GitHub Releases для этой версии проекта."""
    return HAS_RELEASE_INFO and bool(LATEST_RELEASE_API)


def update_checks_enabled() -> bool:
    """Включена ли автоматическая суточная проверка для релизного exe."""
    return IS_FROZEN and release_checks_available()


def build_version_keyboard(release_url: str | None = None) -> InlineKeyboardMarkup | None:
    """Собрать кнопки репозитория и последнего релиза."""
    buttons = []
    if REPOSITORY_URL:
        buttons.append(InlineKeyboardButton(text="Репозиторий", url=REPOSITORY_URL))
    target_release = release_url or RELEASES_URL
    if target_release:
        buttons.append(InlineKeyboardButton(text="Последний релиз", url=target_release))
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


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
        return "ещё не проверялась"
    checked = _parse_iso_utc(value)
    if checked is None:
        return "время последней проверки неизвестно"
    return checked.strftime("%d.%m.%Y, %H:%M UTC")


async def refresh_update_state(*, force: bool = False) -> dict:
    """Обновить кэш последнего релиза без исключений для вызывающего кода."""
    state = load_update_state()
    if not release_checks_available():
        return state
    if not force and (not update_checks_enabled() or _checked_recently(state)):
        return state
    state["last_checked_at"] = _utcnow().isoformat()
    latest = await fetch_latest_release()
    if latest is not None:
        state["latest_version"] = latest.version
        state["release_url"] = latest.url
    save_update_state(state)
    return state


def build_version_text(state: dict) -> str:
    latest = state.get("latest_version") or "неизвестна"
    checked = _format_checked_at(state.get("last_checked_at"))
    launch_mode = "portable Windows exe" if IS_FROZEN else "Python/source"
    if update_checks_enabled():
        check_mode = "автоматически раз в сутки и вручную через /version"
    elif release_checks_available():
        check_mode = "вручную через /version"
    else:
        check_mode = "недоступна для промежуточной dev-сборки"
    repository = (
        f'<a href="{escape(REPOSITORY_URL, quote=True)}">Исходный код и документация</a>'
        if REPOSITORY_URL
        else "Ссылка на репозиторий не встроена"
    )
    return (
        "🎌 <b>ShikiUpdatesBot</b>\n"
        f"{h(PROJECT_SUMMARY)}\n\n"
        f"Версия: <code>{h(APP_VERSION)}</code>\n"
        f"Режим запуска: {launch_mode}\n"
        f"Последняя версия: <code>{h(latest)}</code>\n"
        f"Проверено: {h(checked)}\n"
        f"Проверка релиза: {check_mode}\n\n"
        f"{repository}"
    )


async def check_and_notify_update(bot: Bot) -> None:
    """Один раз уведомить владельца о новой версии после успешной доставки."""
    state = await refresh_update_state()
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
    """Идемпотентно запустить проверку обновлений для релизной exe-сборки."""
    global _update_task
    if not update_checks_enabled():
        return False
    if _update_task is not None and not _update_task.done():
        return False
    _update_task = asyncio.create_task(update_loop(bot))
    _update_task.add_done_callback(_on_update_done)
    return True
