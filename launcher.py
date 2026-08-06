# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Консольная точка входа PyInstaller с диагностикой первого portable-запуска."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

from build_info import APP_VERSION
from runtime import APP_ROOT, ENV_FILE, IS_FROZEN, SingleInstance, ensure_frozen_env


def _configure_standard_streams() -> None:
    """Перевести реальные текстовые потоки в безопасный UTF-8 до диагностики."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _pause_after_error() -> None:
    if IS_FROZEN and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nНажмите Enter, чтобы закрыть окно...")
        except (EOFError, OSError):
            pass


def _load_config() -> object:
    import config

    return config


def run(argv: Sequence[str] | None = None) -> int:
    _configure_standard_streams()
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(f"ShikiUpdatesBot {APP_VERSION}")
        return 0
    if args and args != ["--check-config"]:
        print("Использование: ShikiUpdatesBot.exe [--version | --check-config]")
        return 2

    try:
        if ensure_frozen_env():
            print(
                "Создан файл настроек:\n"
                f"  {ENV_FILE}\n\n"
                "Заполните BOT_TOKEN, OWNER_ID и SHIKI_USER, затем запустите бот снова."
            )
            _pause_after_error()
            return 2

        config = _load_config()
        if args == ["--check-config"]:
            import main  # noqa: F401 - frozen-smoke загружает полный граф приложения

            print(f"Конфигурация корректна. DATA_DIR: {config.DATA_DIR}")
            return 0

        instance = SingleInstance()
        if not instance.acquire():
            print("ShikiUpdatesBot уже запущен из этой папки.")
            _pause_after_error()
            return 3
        try:
            config.log.info("ShikiUpdatesBot %s запускается из %s", APP_VERSION, APP_ROOT)
            from main import main as app_main

            asyncio.run(app_main())
        finally:
            instance.release()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        try:
            import logging

            logging.getLogger("shikiupdatesbot").exception("Критическая ошибка запуска: %s", e)
        except Exception:
            print(f"Ошибка запуска: {e}", file=sys.stderr)
        _pause_after_error()
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
