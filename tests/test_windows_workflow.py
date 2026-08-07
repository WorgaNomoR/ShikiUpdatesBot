# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии Windows release workflow."""

from pathlib import Path

import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows-exe.yml"
WORKFLOW = WORKFLOW_PATH.read_text(encoding="utf-8")
SPEC = yaml.safe_load(WORKFLOW)


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_virustotal_secret_is_scoped_to_single_release_step():
    build = SPEC["jobs"]["build"]
    scan_steps = [
        step for step in build["steps"] if "VIRUSTOTAL_API_KEY" in (step.get("env") or {})
    ]

    assert "VIRUSTOTAL_API_KEY" not in (SPEC.get("env") or {})
    assert "VIRUSTOTAL_API_KEY" not in (build.get("env") or {})
    assert len(scan_steps) == 1
    assert scan_steps[0]["name"] == "Scan release executable with VirusTotal"
    assert scan_steps[0]["if"] == "steps.version.outputs.is_release == 'true'"
    assert scan_steps[0]["continue-on-error"] is True
    assert scan_steps[0]["env"] == {
        "VIRUSTOTAL_API_KEY": "${{ secrets.VIRUSTOTAL_API_KEY }}"
    }
    assert "python release_security.py" in scan_steps[0]["run"]
    assert "--notes-path .\\release\\security-notes.md" in scan_steps[0]["run"]
    assert "--summary-path $env:GITHUB_STEP_SUMMARY" in scan_steps[0]["run"]

    version_step = _step(build, "Resolve and validate build version")
    assert build["outputs"]["is_release"] == "${{ steps.version.outputs.is_release }}"
    assert '$env:GITHUB_EVENT_NAME -eq "push"' in version_step["run"]
    assert '$env:GITHUB_REF -like "refs/tags/*"' in version_step["run"]
    assert (
        '"is_release=$isRelease" | Out-File -FilePath $env:GITHUB_OUTPUT'
        in version_step["run"]
    )


def test_release_notes_keep_generated_changelog_and_security_block():
    build = SPEC["jobs"]["build"]
    upload = _step(build, "Upload verification artifact")
    publish = SPEC["jobs"]["publish"]
    create = _step(publish, "Create draft GitHub Release")

    assert "release/security-notes.md" in upload["with"]["path"]
    assert publish["if"] == "needs.build.outputs.is_release == 'true'"
    assert "--generate-notes" in create["run"]
    assert "--notes-file .\\release\\security-notes.md" in create["run"]


def test_unexpected_scan_failure_has_non_blocking_fallback():
    build = SPEC["jobs"]["build"]
    fallback = _step(build, "Ensure release security notes exist")

    assert fallback["if"] == "always() && steps.version.outputs.is_release == 'true'"
    assert "автоматический анализ недоступен" in fallback["run"]
    assert "$sha256 = if (Test-Path $exePath)" in fallback["run"]
    assert '"недоступен"' in fallback["run"]
    assert "(Test-Path $notesPath -PathType Leaf) -and" in fallback["run"]
    assert "(Get-Item $notesPath).Length -gt 0" in fallback["run"]
    assert "if (-not $notesAreValid)" in fallback["run"]
    assert "::warning::" in fallback["run"]
    assert "GITHUB_STEP_SUMMARY" in fallback["run"]
    assert "import release_security" not in fallback["run"]
    assert "Исходный код ShikiUpdatesBot открыт" not in fallback["run"]


def test_workflow_permissions_remain_narrow():
    assert SPEC["permissions"] == {"contents": "read"}
    assert SPEC["jobs"]["build"].get("permissions") is None
    assert SPEC["jobs"]["publish"]["permissions"] == {
        "actions": "read",
        "contents": "write",
    }
