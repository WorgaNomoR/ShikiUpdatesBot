# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Консольный запуск portable exe и его диагностические режимы."""

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import launcher
import project_meta


def test_source_launcher_resolves_root_metadata_without_pythonpath(tmp_path):
    root = Path(__file__).resolve().parents[1]
    app_root = tmp_path / "application"
    src = app_root / "src"
    src.mkdir(parents=True)
    shutil.copyfile(root / "project_meta.py", app_root / "project_meta.py")
    module_root = Path(launcher.__file__).parent
    for name in ("launcher.py", "build_info.py", "runtime.py"):
        shutil.copyfile(module_root / name, src / name)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    process = subprocess.run(
        [sys.executable, "-B", str(src / "launcher.py"), "--version"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == f"ShikiUpdatesBot {project_meta.PROJECT_VERSION}"


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


def test_launcher_first_run_stops_after_creating_env(monkeypatch):
    output = io.BytesIO()
    cp1252_stdout = io.TextIOWrapper(output, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", cp1252_stdout)
    monkeypatch.setattr(launcher, "ensure_frozen_env", lambda: True)
    monkeypatch.setattr(launcher, "_pause_after_error", lambda: None)

    assert launcher.run([]) == 2
    cp1252_stdout.flush()
    assert "Создан файл настроек" in output.getvalue().decode("utf-8")


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
    pause_after_error = MagicMock()
    monkeypatch.setattr(launcher, "_pause_after_error", pause_after_error)

    assert launcher.run([]) == 1
    pause_after_error.assert_called_once_with()
    instance.release.assert_called_once_with()
