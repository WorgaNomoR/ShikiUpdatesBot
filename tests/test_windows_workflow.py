# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии Windows release workflow."""

from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows-exe.yml"
).read_text(encoding="utf-8")


def test_virustotal_secret_is_scoped_to_tag_push_step():
    scan_step = WORKFLOW.split("- name: Scan release executable with VirusTotal", 1)[1]
    scan_step = scan_step.split("- name: Ensure release security notes exist", 1)[0]

    assert "$env:GITHUB_EVENT_NAME -eq \"push\"" in WORKFLOW
    assert "$env:GITHUB_REF -like \"refs/tags/*\"" in WORKFLOW
    assert "if: steps.version.outputs.is_release == 'true'" in scan_step
    assert "VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}" in scan_step
    assert "continue-on-error: true" in scan_step
    assert "VIRUSTOTAL_API_KEY" not in WORKFLOW.split("jobs:", 1)[0]


def test_release_notes_keep_generated_changelog_and_security_block():
    assert "release/security-notes.md" in WORKFLOW
    assert "--generate-notes" in WORKFLOW
    assert "--notes-file .\\release\\security-notes.md" in WORKFLOW
    assert "if: needs.build.outputs.is_release == 'true'" in WORKFLOW


def test_unexpected_scan_failure_has_non_blocking_fallback():
    fallback = WORKFLOW.split("- name: Ensure release security notes exist", 1)[1]
    fallback = fallback.split("- name: Assemble portable ZIP and checksum", 1)[0]

    assert "if: always() && steps.version.outputs.is_release == 'true'" in fallback
    assert "автоматический анализ недоступен" in fallback
    assert "::warning::" in fallback
    assert "GITHUB_STEP_SUMMARY" in fallback


def test_workflow_permissions_remain_narrow():
    assert "permissions:\n  contents: read" in WORKFLOW
    publish = WORKFLOW.split("  publish:", 1)[1]
    assert "permissions:\n      actions: read\n      contents: write" in publish
