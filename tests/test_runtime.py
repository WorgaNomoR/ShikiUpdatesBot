# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контракты portable runtime и Windows-интеграции."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import runtime


def _load_runtime_roots(module_path):
    """Вычислить настоящие константы runtime для заданного расположения файла."""
    namespace = {"__file__": str(module_path), "__name__": "runtime_roots"}
    source = Path(runtime.__file__).read_text(encoding="utf-8")
    exec(compile(source, str(module_path), "exec"), namespace)
    return namespace


def test_source_roots_stay_above_src_independently_of_working_directory(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.chdir(tmp_path)
    app_root = tmp_path / "application"

    roots = _load_runtime_roots(app_root / "src" / "runtime.py")

    assert roots["APP_ROOT"] == app_root
    assert roots["RESOURCE_ROOT"] == app_root
    assert roots["ENV_FILE"] == app_root / ".env"
    assert roots["ENV_EXAMPLE_FILE"] == app_root / ".env.example"
    assert roots["LOG_DIR"] == app_root / "logs"
    assert roots["DEFAULT_DATA_DIR"] == Path("/data")


def test_frozen_roots_separate_physical_executable_and_bundled_resources(
    monkeypatch,
    tmp_path,
):
    app_root = tmp_path / "portable"
    resource_root = tmp_path / "extracted"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app_root / "ShikiUpdatesBot.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(resource_root), raising=False)
    monkeypatch.chdir(tmp_path)

    roots = _load_runtime_roots(resource_root / "runtime.py")

    assert roots["APP_ROOT"] == app_root
    assert roots["RESOURCE_ROOT"] == resource_root
    assert roots["ENV_FILE"] == app_root / ".env"
    assert roots["ENV_EXAMPLE_FILE"] == app_root / ".env.example"
    assert roots["LOG_DIR"] == app_root / "logs"
    assert roots["DEFAULT_DATA_DIR"] == app_root / "data"
    assert roots["resolve_data_dir"]("custom-data") == app_root / "custom-data"


def test_frozen_relative_data_dir_stays_beside_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "IS_FROZEN", True)
    monkeypatch.setattr(runtime, "APP_ROOT", tmp_path)
    assert runtime.resolve_data_dir("custom-data") == tmp_path / "custom-data"


def test_source_relative_data_dir_keeps_existing_semantics(monkeypatch):
    monkeypatch.setattr(runtime, "IS_FROZEN", False)
    assert runtime.resolve_data_dir("custom-data") == Path("custom-data")


def test_ensure_frozen_env_copies_once(monkeypatch, tmp_path):
    example = tmp_path / ".env.example"
    env = tmp_path / ".env"
    example.write_text("BOT_TOKEN=placeholder\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "IS_FROZEN", True)
    monkeypatch.setattr(runtime, "ENV_EXAMPLE_FILE", example)
    monkeypatch.setattr(runtime, "ENV_FILE", env)

    assert runtime.ensure_frozen_env() is True
    assert env.read_text(encoding="utf-8") == "BOT_TOKEN=placeholder\n"
    env.write_text("BOT_TOKEN=real\n", encoding="utf-8")
    assert runtime.ensure_frozen_env() is False
    assert env.read_text(encoding="utf-8") == "BOT_TOKEN=real\n"


def test_ensure_frozen_env_source_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "IS_FROZEN", False)
    monkeypatch.setattr(runtime, "ENV_FILE", tmp_path / ".env")
    assert runtime.ensure_frozen_env() is False


def test_source_logging_keeps_console_handler(monkeypatch):
    configured = {}
    monkeypatch.setattr(runtime, "IS_FROZEN", False)
    monkeypatch.setattr(
        runtime.logging,
        "basicConfig",
        lambda **kwargs: configured.update(kwargs),
    )

    runtime.configure_logging()

    assert configured["level"] == runtime.logging.INFO
    assert configured["format"] == runtime._LOG_FORMAT
    assert len(configured["handlers"]) == 1
    assert isinstance(configured["handlers"][0], runtime.logging.StreamHandler)


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_single_instance_mutex_is_scoped_to_portable_root(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "IS_FROZEN", True)
    first = runtime.SingleInstance(tmp_path)
    second = runtime.SingleInstance(tmp_path)
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        second.release()
        first.release()


@pytest.mark.skipif(os.name != "nt", reason="Windows console control handler")
def test_console_close_guard_requests_async_shutdown(monkeypatch):
    monkeypatch.setattr(runtime, "IS_FROZEN", True)
    loop = MagicMock()
    request_stop = MagicMock()
    guard = runtime.WindowsConsoleCloseGuard(loop, request_stop, timeout=0)
    assert guard.install() is True
    try:
        assert guard._handler(2) == 1
        loop.call_soon_threadsafe.assert_called_once_with(request_stop)
    finally:
        guard.complete()
        guard.uninstall()


@pytest.mark.skipif(os.name != "nt", reason="Windows console control handler")
def test_console_close_guard_handles_closed_event_loop(monkeypatch):
    monkeypatch.setattr(runtime, "IS_FROZEN", True)
    loop = MagicMock()
    loop.call_soon_threadsafe.side_effect = RuntimeError("loop closed")
    request_stop = MagicMock()
    guard = runtime.WindowsConsoleCloseGuard(loop, request_stop, timeout=0)
    assert guard.install() is True
    try:
        assert guard._handler(2) == 1
        loop.call_soon_threadsafe.assert_called_once_with(request_stop)
    finally:
        guard.complete()
        guard.uninstall()


def test_console_close_guard_default_timeout_fits_windows_limit():
    guard = runtime.WindowsConsoleCloseGuard(MagicMock(), MagicMock())
    assert guard._timeout == 4.0
