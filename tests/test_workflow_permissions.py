# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии прав доступа read-only CI workflows."""

from pathlib import Path

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
READ_ONLY_WORKFLOWS = ("lint.yml", "tests.yml", "docker.yml")


def _load_workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("workflow_name", READ_ONLY_WORKFLOWS)
def test_read_only_workflow_permissions_are_explicit(workflow_name):
    workflow = _load_workflow(workflow_name)

    assert workflow["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("workflow_name", READ_ONLY_WORKFLOWS)
def test_read_only_workflow_checkouts_do_not_persist_credentials(workflow_name):
    workflow = _load_workflow(workflow_name)
    checkout_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]

    assert checkout_steps
    assert all(
        step.get("with", {}).get("persist-credentials") is False
        for step in checkout_steps
    )


def test_ruff_autofix_keeps_explicit_write_permission():
    workflow = _load_workflow("ruff-autofix.yml")

    assert workflow["permissions"] == {"contents": "write"}
