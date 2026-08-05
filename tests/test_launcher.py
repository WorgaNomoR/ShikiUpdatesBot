# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Консольный запуск portable exe и его диагностические режимы."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import launcher


def test_launcher_version_does_not_load_config(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "APP_VERSION", "v1.2.3")
    monkeypatch.setattr(launcher, "_load_config", lambda: (_ for _ in ()).throw(AssertionError))
    assert launcher.run(["--version"]) == 0
    assert "v1.2.3" in capsys.readouterr().out


def test_launcher_check_config_is_offline(monkeypatch, capsys, tmp_path):
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(main=lambda: None))
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


def test_launcher_reports_second_instance_without_traceback(monkeypatch, capsys):
    instance = MagicMock()
    instance.acquire.return_value = False
    pause = MagicMock()
    monkeypatch.setattr(launcher, "ensure_frozen_env", lambda: False)
    monkeypatch.setattr(
        launcher,
        "_load_config",
        lambda: SimpleNamespace(DATA_DIR="data", log=MagicMock()),
    )
    monkeypatch.setattr(launcher, "SingleInstance", lambda: instance)
    monkeypatch.setattr(launcher, "_pause_after_error", pause)

    assert launcher.run([]) == 3
    assert "уже запущен" in capsys.readouterr().out
    instance.release.assert_not_called()
    pause.assert_called_once_with()


def test_launcher_releases_instance_after_main(monkeypatch):
    async def fake_main():
        return None

    instance = MagicMock()
    instance.acquire.return_value = True
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(main=fake_main))
    monkeypatch.setattr(launcher, "ensure_frozen_env", lambda: False)
    monkeypatch.setattr(
        launcher,
        "_load_config",
        lambda: SimpleNamespace(DATA_DIR="data", log=MagicMock()),
    )
    monkeypatch.setattr(launcher, "SingleInstance", lambda: instance)

    assert launcher.run([]) == 0
    instance.release.assert_called_once_with()


def test_launcher_releases_instance_when_main_fails(monkeypatch):
    async def fake_main():
        raise RuntimeError("main failed")

    instance = MagicMock()
    instance.acquire.return_value = True
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(main=fake_main))
    monkeypatch.setattr(launcher, "ensure_frozen_env", lambda: False)
    monkeypatch.setattr(
        launcher,
        "_load_config",
        lambda: SimpleNamespace(DATA_DIR="data", log=MagicMock()),
    )
    monkeypatch.setattr(launcher, "SingleInstance", lambda: instance)
    monkeypatch.setattr(launcher, "_pause_after_error", lambda: None)

    assert launcher.run([]) == 1
    instance.release.assert_called_once_with()
