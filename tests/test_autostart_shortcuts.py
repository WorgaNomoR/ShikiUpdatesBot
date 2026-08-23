# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контракты явных portable-помощников автозапуска Windows."""

import os
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PACKAGING = ROOT / "packaging" / "windows"
ENABLE_SOURCE = WINDOWS_PACKAGING / "Enable-Autostart.cmd"
DISABLE_SOURCE = WINDOWS_PACKAGING / "Disable-Autostart.cmd"
DOCKERIGNORE = ROOT / ".dockerignore"
POWERSHELL = shutil.which("powershell")
WINDOWS_TESTS_UNAVAILABLE = sys.platform != "win32" or POWERSHELL is None
STARTUP_RELATIVE = Path("Microsoft/Windows/Start Menu/Programs/Startup")


def _run_helper(helper: Path, *, appdata: Path):
    """Запустить cmd-помощник без shell-интерполяции тестовых путей."""
    env = os.environ.copy()
    env["APPDATA"] = str(appdata)
    env["SHIKI_TEST_HELPER"] = str(helper)
    return subprocess.run(  # nosec B603  # nosemgrep
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "& $env:SHIKI_TEST_HELPER; exit $LASTEXITCODE",
        ],
        cwd=helper.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _shortcut_properties(shortcut: Path) -> tuple[str, str]:
    """Прочитать target и working directory ярлыка через штатный COM API."""
    env = os.environ.copy()
    env["SHIKI_TEST_SHORTCUT"] = str(shortcut)
    command = (
        "$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut("
        "$env:SHIKI_TEST_SHORTCUT); "
        "[Console]::WriteLine($shortcut.TargetPath); "
        "[Console]::WriteLine($shortcut.WorkingDirectory)"
    )
    result = subprocess.run(  # nosec B603  # nosemgrep
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    target, working_directory = result.stdout.splitlines()
    return target, working_directory


def _write_wrong_shortcut(shortcut: Path) -> None:
    """Испортить свойства ярлыка, чтобы проверить замену повторным Enable."""
    env = os.environ.copy()
    env["SHIKI_TEST_SHORTCUT"] = str(shortcut)
    command = (
        "$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut("
        "$env:SHIKI_TEST_SHORTCUT); "
        "$shortcut.TargetPath = 'C:\\wrong.exe'; "
        "$shortcut.WorkingDirectory = 'C:\\'; "
        "$shortcut.Save()"
    )
    result = subprocess.run(  # nosec B603  # nosemgrep
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def portable_helpers(tmp_path):
    """Скопировать помощники в путь с shell-значимыми символами."""
    portable = tmp_path / "portable & helpers (test)!"
    portable.mkdir()
    enable = portable / ENABLE_SOURCE.name
    disable = portable / DISABLE_SOURCE.name
    shutil.copy2(ENABLE_SOURCE, enable)
    shutil.copy2(DISABLE_SOURCE, disable)
    return portable, enable, disable


def test_helper_sources_keep_narrow_transparent_boundaries():
    sources = {
        path.name: path.read_text(encoding="utf-8").lower()
        for path in (ENABLE_SOURCE, DISABLE_SOURCE)
    }
    combined = "\n".join(sources.values())
    forbidden = (
        "encodedcommand",
        "executionpolicy",
        "windowstyle",
        "currentversion\\run",
        "reg add",
        "schtasks",
        "new-service",
        "sc.exe",
        "invoke-webrequest",
        "downloadstring",
        "start-bitstransfer",
        "bitsadmin",
        "http://",
        "https://",
        "frombase64string",
    )

    assert all(token not in combined for token in forbidden)
    assert "-noprofile -command" in sources[ENABLE_SOURCE.name]
    assert "wscript.shell" in sources[ENABLE_SOURCE.name]
    assert "createshortcut" in sources[ENABLE_SOURCE.name]
    assert "$env:shiki_autostart_link" in sources[ENABLE_SOURCE.name]
    assert "$env:shiki_autostart_exe" in sources[ENABLE_SOURCE.name]
    assert combined.count("shikiupdatesbot.lnk") == 2


def test_helpers_are_excluded_from_docker_context():
    ignored = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "packaging/windows/" in ignored


@pytest.mark.skipif(
    WINDOWS_TESTS_UNAVAILABLE,
    reason="Поведенческие тесты ярлыков выполняются только под Windows",
)
def test_enable_requires_adjacent_executable_and_creates_nothing(portable_helpers, tmp_path):
    portable, enable, _ = portable_helpers
    appdata = tmp_path / "App & Data (test)!"
    executable = portable / "ShikiUpdatesBot.exe"
    shortcut = appdata / STARTUP_RELATIVE / "ShikiUpdatesBot.lnk"

    result = _run_helper(enable, appdata=appdata)

    assert result.returncode == 1
    assert str(executable).casefold() in result.stdout.casefold()
    assert "[ERROR]" in result.stdout
    assert not shortcut.exists()
    assert not appdata.exists()


@pytest.mark.skipif(
    WINDOWS_TESTS_UNAVAILABLE,
    reason="Поведенческие тесты ярлыков выполняются только под Windows",
)
def test_enable_creates_and_replaces_exact_shortcut(portable_helpers, tmp_path):
    portable, enable, _ = portable_helpers
    appdata = tmp_path / "App & Data (test)!"
    executable = portable / "ShikiUpdatesBot.exe"
    executable.write_bytes(b"test executable")
    shortcut = appdata / STARTUP_RELATIVE / "ShikiUpdatesBot.lnk"

    first = _run_helper(enable, appdata=appdata)

    assert first.returncode == 0, first.stderr
    assert str(shortcut) in first.stdout
    assert str(executable).casefold() in first.stdout.casefold()
    assert tuple(map(os.path.normcase, _shortcut_properties(shortcut))) == (
        os.path.normcase(str(executable)),
        os.path.normcase(str(portable)),
    )

    _write_wrong_shortcut(shortcut)
    second = _run_helper(enable, appdata=appdata)

    assert second.returncode == 0, second.stderr
    assert tuple(map(os.path.normcase, _shortcut_properties(shortcut))) == (
        os.path.normcase(str(executable)),
        os.path.normcase(str(portable)),
    )


@pytest.mark.skipif(
    WINDOWS_TESTS_UNAVAILABLE,
    reason="Поведенческие тесты ярлыков выполняются только под Windows",
)
def test_disable_is_idempotent_and_preserves_other_files(portable_helpers, tmp_path):
    portable, enable, disable = portable_helpers
    appdata = tmp_path / "App & Data (test)!"
    executable = portable / "ShikiUpdatesBot.exe"
    executable.write_bytes(b"test executable")
    env_file = portable / ".env"
    env_file.write_text("BOT_TOKEN=secret", encoding="utf-8")
    data_file = portable / "data" / "state.json"
    data_file.parent.mkdir()
    data_file.write_text("{}", encoding="utf-8")
    log_file = portable / "logs" / "bot.log"
    log_file.parent.mkdir()
    log_file.write_text("log", encoding="utf-8")
    shortcut = appdata / STARTUP_RELATIVE / "ShikiUpdatesBot.lnk"
    unrelated = shortcut.parent / "Unrelated.lnk"

    assert _run_helper(enable, appdata=appdata).returncode == 0
    unrelated.write_bytes(b"unrelated")
    first = _run_helper(disable, appdata=appdata)
    second = _run_helper(disable, appdata=appdata)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert str(shortcut) in first.stdout
    assert "already absent" in second.stdout
    assert not shortcut.exists()
    assert unrelated.read_bytes() == b"unrelated"
    assert executable.read_bytes() == b"test executable"
    assert env_file.read_text(encoding="utf-8") == "BOT_TOKEN=secret"
    assert data_file.read_text(encoding="utf-8") == "{}"
    assert log_file.read_text(encoding="utf-8") == "log"
