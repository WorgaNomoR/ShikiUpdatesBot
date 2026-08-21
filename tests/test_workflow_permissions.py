# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии прав доступа и безопасности CI workflows."""

import re
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
    assert all(
        job.get("permissions") in (None, {"contents": "read"})
        for job in workflow["jobs"].values()
    )


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


def test_ruff_autofix_serializes_runs_per_ref():
    workflow = _load_workflow("ruff-autofix.yml")

    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }


def test_ruff_autofix_actions_are_pinned_to_full_shas():
    workflow = _load_workflow("ruff-autofix.yml")
    action_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]

    assert action_steps
    assert all(
        re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", step["uses"])
        for step in action_steps
    )


def test_ruff_autofix_validates_fixed_tree_before_commit():
    workflow = _load_workflow("ruff-autofix.yml")
    steps = workflow["jobs"]["ruff-autofix"]["steps"]
    named_steps = {step["name"]: (index, step) for index, step in enumerate(steps)}

    fix_index, fix_step = named_steps["Apply Ruff fixes"]
    ruff_index, ruff_step = named_steps["Validate Ruff result"]
    tests_index, tests_step = named_steps["Run tests"]
    whitespace_index, whitespace_step = named_steps["Check whitespace"]
    commit_index, commit_step = named_steps["Commit fixes"]

    assert fix_step["with"]["args"] == "check --fix"
    assert ruff_step["run"] == "ruff check ."
    assert tests_step["run"] == "pytest tests/"
    assert whitespace_step["run"] == "git diff --check"
    assert fix_index < ruff_index < tests_index < whitespace_index < commit_index
    assert commit_step["if"] == "${{ success() }}"
    assert "git commit" in commit_step["run"]
    assert "git push" in commit_step["run"]


def test_ruff_autofix_skips_commit_when_tree_is_unchanged():
    workflow = _load_workflow("ruff-autofix.yml")
    commit_step = next(
        step
        for step in workflow["jobs"]["ruff-autofix"]["steps"]
        if step.get("name") == "Commit fixes"
    )

    assert 'if [ -n "$(git status --porcelain)" ]; then' in commit_step["run"]
