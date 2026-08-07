# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии Windows release workflow."""

import os
import re
import shutil

# Модуль нужен только контролируемой тестовой функции без shell-выполнения.
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest
import yaml

import release_security

WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows-exe.yml"
WORKFLOW = WORKFLOW_PATH.read_text(encoding="utf-8")
SPEC = yaml.safe_load(WORKFLOW)
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
POWERSHELL_TESTS_UNAVAILABLE = sys.platform != "win32" or POWERSHELL is None


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _value_paths(value, needle: str, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _value_paths(child, needle, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _value_paths(child, needle, (*path, index))
    elif isinstance(value, str) and needle in value:
        yield path


def _run_powershell(script: str, workdir: Path, env: dict[str, str]):
    script_path = workdir / "workflow-step.ps1"
    script_path.write_text(script, encoding="utf-8-sig", newline="\n")
    process_env = os.environ.copy()
    process_env.update(env)
    # Команда, аргументы и временный файл задаются тестом; shell не используется.
    return subprocess.run(  # nosec B603  # nosemgrep
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        cwd=workdir,
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_virustotal_secret_is_scoped_to_single_release_step():
    build = SPEC["jobs"]["build"]
    scan_steps = [
        (job_name, step)
        for job_name, job in SPEC["jobs"].items()
        for step in job["steps"]
        if "VIRUSTOTAL_API_KEY" in (step.get("env") or {})
    ]

    assert "VIRUSTOTAL_API_KEY" not in (SPEC.get("env") or {})
    assert all(
        "VIRUSTOTAL_API_KEY" not in (job.get("env") or {})
        for job in SPEC["jobs"].values()
    )
    assert len(scan_steps) == 1
    job_name, scan_step = scan_steps[0]
    assert job_name == "build"
    assert scan_step["name"] == "Scan release executable with VirusTotal"
    assert scan_step["if"] == "steps.version.outputs.is_release == 'true'"
    assert scan_step["continue-on-error"] is True
    assert scan_step["env"] == {
        "VIRUSTOTAL_API_KEY": "${{ secrets.VIRUSTOTAL_API_KEY }}"
    }
    scan_index = build["steps"].index(scan_step)
    assert list(_value_paths(SPEC, "secrets.VIRUSTOTAL_API_KEY")) == [
        ("jobs", "build", "steps", scan_index, "env", "VIRUSTOTAL_API_KEY")
    ]
    assert "python release_security.py" in scan_step["run"]
    assert "--notes-path .\\release\\security-notes.md" in scan_step["run"]
    assert "--summary-path $env:GITHUB_STEP_SUMMARY" in scan_step["run"]

    version_step = _step(build, "Resolve and validate build version")
    assert build["outputs"]["is_release"] == "${{ steps.version.outputs.is_release }}"
    assert '$env:GITHUB_EVENT_NAME -eq "push"' in version_step["run"]
    assert '$env:GITHUB_REF -like "refs/tags/*"' in version_step["run"]
    assert (
        '"is_release=$isRelease" | Out-File -FilePath $env:GITHUB_OUTPUT'
        in version_step["run"]
    )


@pytest.mark.skipif(
    POWERSHELL_TESTS_UNAVAILABLE,
    reason="Поведенческие PowerShell-тесты выполняются только под Windows",
)
@pytest.mark.parametrize(
    ("event_name", "ref", "expected"),
    [
        ("push", "refs/tags/v0.1.0", "true"),
        ("push", "refs/heads/main", "false"),
        ("pull_request", "refs/tags/v0.1.0", "false"),
    ],
)
def test_is_release_uses_push_and_tag_together(tmp_path, event_name, ref, expected):
    version_step = _step(SPEC["jobs"]["build"], "Resolve and validate build version")
    match = re.search(
        r"(?ms)^\$isRelease = \(\n.*?^\)\.ToString\(\)\.ToLowerInvariant\(\)",
        version_step["run"],
    )

    assert match is not None
    result = _run_powershell(
        f'{match.group(0)}\nWrite-Output "is_release=$isRelease"\n',
        tmp_path,
        {"GITHUB_EVENT_NAME": event_name, "GITHUB_REF": ref},
    )

    assert result.returncode == 0, result.stderr
    assert f"is_release={expected}" in result.stdout


def test_release_notes_keep_generated_changelog_and_security_block():
    build = SPEC["jobs"]["build"]
    upload = _step(build, "Upload verification artifact")
    publish = SPEC["jobs"]["publish"]
    create = _step(publish, "Create draft GitHub Release")

    assert "release/security-notes.md" in upload["with"]["path"]
    assert publish["if"] == "needs.build.outputs.is_release == 'true'"
    assert "--generate-notes" in create["run"]
    assert "--notes-file .\\release\\security-notes.md" in create["run"]


def test_unexpected_scan_failure_has_independent_non_blocking_fallback():
    build = SPEC["jobs"]["build"]
    fallback = _step(build, "Ensure release security notes exist")

    assert fallback["if"] == "always() && steps.version.outputs.is_release == 'true'"
    assert "import release_security" not in fallback["run"]
    assert "Исходный код ShikiUpdatesBot открыт" not in fallback["run"]


@pytest.mark.skipif(
    POWERSHELL_TESTS_UNAVAILABLE,
    reason="Поведенческие PowerShell-тесты выполняются только под Windows",
)
@pytest.mark.parametrize("initial_state", ["valid", "missing", "empty", "damaged"])
def test_security_notes_fallback_behavior(tmp_path, initial_state):
    fallback = _step(SPEC["jobs"]["build"], "Ensure release security notes exist")
    notes = tmp_path / "release" / "security-notes.md"
    summary = tmp_path / "summary.md"
    initial_bytes = None

    if initial_state != "missing":
        notes.parent.mkdir(parents=True)
        if initial_state == "valid":
            initial = release_security.build_security_markdown(
                release_security.ScanReport(
                    sha256="a" * 64,
                    available=True,
                    total_engines=70,
                )
            )
        elif initial_state == "damaged":
            initial = "<!-- shikiupdatesbot-security-report:start -->\nоборванный отчёт\n"
        else:
            initial = ""
        notes.write_text(initial, encoding="utf-8", newline="\n")
        initial_bytes = notes.read_bytes()

    result = _run_powershell(
        fallback["run"],
        tmp_path,
        {"GITHUB_STEP_SUMMARY": str(summary)},
    )

    assert result.returncode == 0, result.stderr
    if initial_state == "valid":
        assert notes.read_bytes() == initial_bytes
        assert not summary.exists()
    else:
        text = notes.read_text(encoding="utf-8-sig")
        assert "<!-- shikiupdatesbot-security-report:start -->" in text
        assert "автоматический анализ недоступен" in text
        assert "<!-- shikiupdatesbot-security-report:end -->" in text
        assert "недоступен" in text
        assert "автоматический анализ недоступен" in summary.read_text(
            encoding="utf-8-sig"
        )


@pytest.mark.skipif(
    POWERSHELL_TESTS_UNAVAILABLE,
    reason="Поведенческие PowerShell-тесты выполняются только под Windows",
)
def test_security_notes_fallback_hashes_existing_executable(tmp_path):
    fallback = _step(SPEC["jobs"]["build"], "Ensure release security notes exist")
    executable = tmp_path / "dist" / "ShikiUpdatesBot.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"abc")
    summary = tmp_path / "summary.md"

    result = _run_powershell(
        fallback["run"],
        tmp_path,
        {"GITHUB_STEP_SUMMARY": str(summary)},
    )

    assert result.returncode == 0, result.stderr
    expected_sha256 = (
        "ba7816bf8f01cfea414140de5dae2223"
        "b00361a396177a9cb410ff61f20015ad"
    )
    notes = tmp_path / "release" / "security-notes.md"
    assert f"- **SHA-256:** `{expected_sha256}`" in notes.read_text(
        encoding="utf-8-sig"
    )


def test_workflow_permissions_remain_narrow():
    assert SPEC["permissions"] == {"contents": "read"}
    assert SPEC["jobs"]["build"].get("permissions") is None
    assert SPEC["jobs"]["publish"]["permissions"] == {
        "actions": "read",
        "contents": "write",
    }
