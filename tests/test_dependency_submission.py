# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии отправки разрешённого Python dependency graph."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "dependency-submission.yml"
VALIDATOR_PATH = ROOT / ".github" / "scripts" / "validate_dependency_submission.py"

EXPECTED_PATHS = [
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-build.txt",
    ".github/scripts/validate_dependency_submission.py",
    ".github/workflows/dependency-submission.yml",
]
EXPECTED_MANIFESTS = [
    ROOT / "requirements.txt",
    ROOT / "requirements-dev.txt",
    ROOT / "requirements-build.txt",
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
EXPECTED_RUN_STEPS = [
    (
        "Validate submitted Python dependency graph",
        "python .github/scripts/validate_dependency_submission.py output.json",
    ),
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
    action_steps = [
        (step["name"], step["uses"])
        for step in steps
        if "uses" in step
    ]
    run_steps = [(step["name"], step["run"]) for step in steps if "run" in step]

    assert action_steps == EXPECTED_ACTIONS
    assert run_steps == EXPECTED_RUN_STEPS
    assert all(("uses" in step) != ("run" in step) for step in steps)


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
        "filePath": ".",
        "detectorsCategories": "Python",
        "correlator": "shikiupdatesbot-python-3.12",
    }


def test_dependency_submission_root_scan_has_exactly_three_canonical_manifests():
    adapter = ROOT / ".github" / "dependency-submission" / "requirements.txt"
    root_manifests = sorted(ROOT.glob("requirements*.txt"))

    assert root_manifests == sorted(EXPECTED_MANIFESTS)
    assert not adapter.exists()


def _run_validator(tmp_path: Path, dependency_graphs: object) -> subprocess.CompletedProcess:
    output_path = tmp_path / "output.json"
    output_path.write_text(
        json.dumps({"dependencyGraphs": dependency_graphs}),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(VALIDATOR_PATH), str(output_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _resolved_graph() -> dict:
    return {
        "explicitlyReferencedComponentIds": ["direct"],
        "graph": {
            "direct": ["transitive"],
            "transitive": None,
        },
    }


def test_dependency_submission_validator_accepts_three_resolved_root_manifests(tmp_path):
    dependency_graphs = {
        str(tmp_path / manifest.name): _resolved_graph()
        for manifest in EXPECTED_MANIFESTS
    }

    result = _run_validator(tmp_path, dependency_graphs)

    assert result.returncode == 0
    assert "validation passed" in result.stdout


def test_dependency_submission_validator_rejects_empty_snapshot(tmp_path):
    result = _run_validator(tmp_path, {})

    assert result.returncode == 1
    assert "Missing dependency graphs" in result.stderr


def test_dependency_submission_validator_rejects_manifest_without_edges(tmp_path):
    dependency_graphs = {
        str(tmp_path / manifest.name): _resolved_graph()
        for manifest in EXPECTED_MANIFESTS
    }
    dependency_graphs[str(tmp_path / "requirements.txt")]["graph"] = {
        "direct": None,
    }

    result = _run_validator(tmp_path, dependency_graphs)

    assert result.returncode == 1
    assert "No transitive dependency edges resolved for requirements.txt" in result.stderr
