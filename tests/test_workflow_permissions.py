# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии прав доступа и безопасности CI workflows."""

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


def test_ruff_autofix_actions_match_approved_shas():
    workflow = _load_workflow("ruff-autofix.yml")
    action_steps = [
        (step["name"], step["uses"])
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]

    assert action_steps == [
        (
            "Checkout repository",
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        ),
        (
            "Set up Python",
            "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
        ),
        (
            "Apply Ruff fixes",
            "astral-sh/ruff-action@4919ec5cf1f49eff0871dbcea0da843445b837e6",
        ),
    ]


def test_ruff_autofix_prepares_python_and_dependencies_before_fix():
    workflow = _load_workflow("ruff-autofix.yml")
    steps = workflow["jobs"]["ruff-autofix"]["steps"]
    named_steps = {step["name"]: (index, step) for index, step in enumerate(steps)}

    checkout_index, _ = named_steps["Checkout repository"]
    setup_index, setup_step = named_steps["Set up Python"]
    install_index, install_step = named_steps["Install dependencies"]
    fix_index, _ = named_steps["Apply Ruff fixes"]
    install_commands = [
        line.strip() for line in install_step["run"].splitlines() if line.strip()
    ]

    assert setup_step["with"]["python-version"] == "3.12"
    assert install_commands == [
        "python -m pip install --upgrade pip",
        "python -m pip install -r requirements.txt",
        "python -m pip install -r requirements-dev.txt",
    ]
    assert checkout_index < setup_index < install_index < fix_index


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
