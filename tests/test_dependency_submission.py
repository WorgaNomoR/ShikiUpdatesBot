# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии отправки разрешённого Python dependency graph."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "dependency-submission.yml"
ADAPTER_PATH = ROOT / ".github" / "dependency-submission" / "requirements.txt"

EXPECTED_PATHS = [
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-build.txt",
    ".github/dependency-submission/requirements.txt",
    ".github/workflows/dependency-submission.yml",
]
EXPECTED_ACTIONS = [
    (
        "Checkout repository",
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
    ),
    (
        "Set up Python",
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
    ),
    (
        "Submit resolved Python dependency graph",
        "advanced-security/component-detection-dependency-submission-action@"
        "31f25a8de68ae5ce2ca274bc28546a78683c15ce",
    ),
]
EXPECTED_ADAPTER_REFERENCES = [
    "-r ../../requirements.txt",
    "-r ../../requirements-dev.txt",
    "-r ../../requirements-build.txt",
]


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_dependency_submission_runs_only_for_default_branch_inputs():
    workflow = _load_workflow()

    assert workflow["on"] == {
        "workflow_dispatch": None,
        "push": {
            "branches": ["main"],
            "paths": EXPECTED_PATHS,
        },
    }
    assert "pull_request" not in workflow["on"]
    assert "schedule" not in workflow["on"]


def test_dependency_submission_has_one_bounded_write_job():
    workflow = _load_workflow()
    job = workflow["jobs"]["submit-python-dependencies"]

    assert workflow["permissions"] == {"contents": "write"}
    assert job.get("permissions") is None
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 10
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }


def test_dependency_submission_actions_match_approved_shas():
    workflow = _load_workflow()
    steps = workflow["jobs"]["submit-python-dependencies"]["steps"]
    action_steps = [(step["name"], step["uses"]) for step in steps]

    assert action_steps == EXPECTED_ACTIONS
    assert all("run" not in step for step in steps)


def test_dependency_submission_uses_python_312_without_persisted_credentials():
    workflow = _load_workflow()
    steps = workflow["jobs"]["submit-python-dependencies"]["steps"]
    named_steps = {step["name"]: step for step in steps}

    assert named_steps["Checkout repository"]["with"] == {
        "persist-credentials": False,
    }
    assert named_steps["Set up Python"]["with"] == {
        "python-version": "3.12",
    }
    assert named_steps["Submit resolved Python dependency graph"]["with"] == {
        "filePath": ".github/dependency-submission",
        "correlator": "shikiupdatesbot-python-3.12",
    }


def test_dependency_submission_adapter_references_each_root_manifest_once():
    references = [
        line.strip()
        for line in ADAPTER_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert references == EXPECTED_ADAPTER_REFERENCES
    assert all((ADAPTER_PATH.parent / line.removeprefix("-r ")).is_file() for line in references)
