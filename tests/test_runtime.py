# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контракты portable runtime и замороженной точки входа."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import build_info
import launcher
import project_meta
import runtime


def test_semver_tuple_is_strict():
    assert build_info.semver_tuple("v1.2.3") == (1, 2, 3)
    assert build_info.semver_tuple("1.2.3") == (1, 2, 3)
    assert build_info.semver_tuple("v1.2") is None
    assert build_info.semver_tuple("v01.2.3") is None
    assert build_info.semver_tuple("dev") is None


def test_source_mode_has_canonical_project_version_and_repository():
    assert build_info.APP_VERSION == project_meta.PROJECT_VERSION
    assert build_info.APP_REPOSITORY == project_meta.PROJECT_REPOSITORY


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
        assert guard._handler(2) is True
        loop.call_soon_threadsafe.assert_called_once_with(request_stop)
    finally:
        guard.complete()
        guard.uninstall()


def test_launcher_version_does_not_load_config(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "APP_VERSION", "v1.2.3")
    monkeypatch.setattr(launcher, "_load_config", lambda: (_ for _ in ()).throw(AssertionError))
    assert launcher.run(["--version"]) == 0
    assert "v1.2.3" in capsys.readouterr().out


def test_launcher_check_config_is_offline(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(launcher, "ensure_frozen_env", lambda: False)
    monkeypatch.setattr(
        launcher,
        "_load_config",
        lambda: SimpleNamespace(DATA_DIR=tmp_path),
    )
    assert launcher.run(["--check-config"]) == 0
    assert str(tmp_path) in capsys.readouterr().out


def test_launcher_first_run_stops_after_creating_env(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "ensure_frozen_env", lambda: True)
    monkeypatch.setattr(launcher, "_pause_after_error", lambda: None)
    assert launcher.run([]) == 2
    assert "Создан файл настроек" in capsys.readouterr().out
