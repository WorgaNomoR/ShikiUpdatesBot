# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Общие portable/runtime-хелперы для config и замороженного launcher.

Нижний слой только на стандартной библиотеке: определяет PyInstaller, находит
физический каталог exe, настраивает ограниченные файловые логи и владеет
Windows-мьютексом одного экземпляра. Другие модули проекта не импортирует.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import shutil
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

IS_FROZEN = bool(getattr(sys, "frozen", False))
APP_ROOT = (
    Path(sys.executable).resolve().parent
    if IS_FROZEN
    else Path(__file__).resolve().parent
)
ENV_FILE = APP_ROOT / ".env"
ENV_EXAMPLE_FILE = APP_ROOT / ".env.example"
LOG_DIR = APP_ROOT / "logs"
DEFAULT_DATA_DIR = APP_ROOT / "data" if IS_FROZEN else Path("/data")

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def resolve_data_dir(raw: str | None) -> Path:
    """Определить DATA_DIR; относительный путь exe считается от portable-корня."""
    if not raw or not raw.strip():
        return DEFAULT_DATA_DIR
    path = Path(raw.strip()).expanduser()
    if IS_FROZEN and not path.is_absolute():
        return APP_ROOT / path
    return path


def ensure_frozen_env() -> bool:
    """Один раз создать .env из соседнего примера; вернуть True при создании."""
    if not IS_FROZEN or ENV_FILE.exists():
        return False
    if not ENV_EXAMPLE_FILE.is_file():
        raise RuntimeError(
            f"Не найден {ENV_EXAMPLE_FILE.name} рядом с программой: {ENV_EXAMPLE_FILE}"
        )
    try:
        shutil.copyfile(ENV_EXAMPLE_FILE, ENV_FILE)
    except OSError as e:
        raise RuntimeError(f"Не удалось создать {ENV_FILE}: {e}") from e
    return True


def configure_logging() -> logging.Logger:
    """Настроить консольный лог, а для exe ещё и ограниченные portable-логи."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if IS_FROZEN:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    LOG_DIR / "bot.log",
                    maxBytes=2 * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
            )
        except OSError as e:
            print(f"WARNING: не удалось включить файловый лог в {LOG_DIR}: {e}", file=sys.stderr)
    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        datefmt=_LOG_DATE_FORMAT,
        handlers=handlers,
        force=False,
    )
    return logging.getLogger("shikiupdatesbot")


class SingleInstance:
    """Именованный Windows-мьютекс, привязанный к физической portable-папке."""

    _ERROR_ALREADY_EXISTS = 183

    def __init__(self, root: Path = APP_ROOT) -> None:
        self._handle: int | None = None
        digest = hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()[:24]
        self.name = f"Local\\ShikiUpdatesBot-{digest}"

    def acquire(self) -> bool:
        if not IS_FROZEN or os.name != "nt":
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        self._handle = int(handle)
        if ctypes.get_last_error() == self._ERROR_ALREADY_EXISTS:
            self.release()
            return False
        return True

    def release(self) -> None:
        if self._handle is None or os.name != "nt":
            self._handle = None
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.CloseHandle(self._handle)
        self._handle = None

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise RuntimeError("ShikiUpdatesBot уже запущен из этой папки.")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class WindowsConsoleCloseGuard:
    """Дать asyncio-диспетчеру время по возможности завершиться при закрытии консоли."""

    _CLOSE_EVENTS = {2, 5, 6}  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT

    def __init__(self, loop, request_stop, timeout: float = 10.0) -> None:
        self._loop = loop
        self._request_stop = request_stop
        self._timeout = timeout
        self._completed = threading.Event()
        self._handler = None
        self._installed = False

    def install(self) -> bool:
        if not IS_FROZEN or os.name != "nt" or self._installed:
            return False
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        @callback_type
        def handler(event: int) -> bool:
            if event not in self._CLOSE_EVENTS:
                return False
            self._loop.call_soon_threadsafe(self._request_stop)
            self._completed.wait(self._timeout)
            return True

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleCtrlHandler.argtypes = (callback_type, ctypes.c_bool)
        kernel32.SetConsoleCtrlHandler.restype = ctypes.c_bool
        if not kernel32.SetConsoleCtrlHandler(handler, True):
            raise OSError(ctypes.get_last_error(), "SetConsoleCtrlHandler failed")
        self._handler = handler
        self._installed = True
        return True

    def complete(self) -> None:
        self._completed.set()

    def uninstall(self) -> None:
        if not self._installed or self._handler is None or os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleCtrlHandler(self._handler, False)
        self._handler = None
        self._installed = False
