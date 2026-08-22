# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии отправки разрешённого Python dependency graph."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "dependency-submission.yml"
SUBMITTER_PATH = ROOT / ".github" / "scripts" / "submit_dependency_snapshot.py"
VALIDATOR_PATH = ROOT / ".github" / "scripts" / "validate_dependency_submission.py"

EXPECTED_PATHS = [
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-build.txt",
    ".github/scripts/submit_dependency_snapshot.py",
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
]
EXPECTED_RUN_STEPS = [
    (
        "Build resolved Python dependency snapshot",
        "python .github/scripts/submit_dependency_snapshot.py "
        "build dependency-snapshot.json",
    ),
    (
        "Validate resolved Python dependency snapshot",
        "python .github/scripts/validate_dependency_submission.py "
        "dependency-snapshot.json",
    ),
    (
        "Submit resolved Python dependency snapshot",
        "python .github/scripts/submit_dependency_snapshot.py "
        "submit dependency-snapshot.json",
    ),
]


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _load_submitter():
    spec = importlib.util.spec_from_file_location("dependency_submitter", SUBMITTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_dependency_submission_steps_match_approved_contract():
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


def test_dependency_submission_uses_python_312_and_scopes_token_to_upload():
    workflow = _load_workflow()
    steps = workflow["jobs"]["submit-python-dependencies"]["steps"]
    named_steps = {step["name"]: step for step in steps}

    assert named_steps["Checkout repository"]["with"] == {
        "persist-credentials": False,
    }
    assert named_steps["Set up Python"]["with"] == {
        "python-version": "3.12",
    }
    assert named_steps["Submit resolved Python dependency snapshot"]["env"] == {
        "GITHUB_TOKEN": "${{ github.token }}",
    }
    assert all(
        "env" not in step
        for step in steps
        if step["name"] != "Submit resolved Python dependency snapshot"
    )


def test_dependency_submission_has_exactly_three_canonical_root_manifests():
    adapter = ROOT / ".github" / "dependency-submission" / "requirements.txt"
    root_manifests = sorted(ROOT.glob("requirements*.txt"))

    assert root_manifests == sorted(EXPECTED_MANIFESTS)
    assert not adapter.exists()


def _pip_report() -> dict:
    return {
        "install": [
            {
                "requested": True,
                "metadata": {
                    "name": "Demo_Package",
                    "version": "1.2.3",
                    "requires_dist": [
                        "Child.Package>=2",
                        "Ignored; python_version < '2'",
                    ],
                },
            },
            {
                "requested": False,
                "metadata": {
                    "name": "Child.Package",
                    "version": "2.0",
                    "requires_dist": [],
                },
            },
        ],
    }


def test_dependency_snapshot_builder_preserves_identity_versions_and_edges():
    submitter = _load_submitter()

    manifest = submitter.build_manifest_snapshot(
        "requirements-dev.txt",
        "development",
        _pip_report(),
    )

    direct_url = "pkg:pypi/demo-package@1.2.3"
    child_url = "pkg:pypi/child-package@2.0"
    assert manifest == {
        "name": "requirements-dev.txt",
        "file": {"source_location": "requirements-dev.txt"},
        "resolved": {
            direct_url: {
                "package_url": direct_url,
                "relationship": "direct",
                "scope": "development",
                "dependencies": [child_url],
            },
            child_url: {
                "package_url": child_url,
                "relationship": "indirect",
                "scope": "development",
                "dependencies": [],
            },
        },
    }


def _resolved_manifest(manifest: str) -> dict:
    direct_url = f"pkg:pypi/{manifest.removesuffix('.txt')}@1.0"
    child_url = f"pkg:pypi/{manifest.removesuffix('.txt')}-child@2.0"
    return {
        "name": manifest,
        "file": {"source_location": manifest},
        "resolved": {
            direct_url: {
                "package_url": direct_url,
                "relationship": "direct",
                "scope": "runtime",
                "dependencies": [child_url],
            },
            child_url: {
                "package_url": child_url,
                "relationship": "indirect",
                "scope": "runtime",
                "dependencies": [],
            },
        },
    }


def _run_validator(tmp_path: Path, manifests: object) -> subprocess.CompletedProcess:
    output_path = tmp_path / "dependency-snapshot.json"
    output_path.write_text(
        json.dumps({"manifests": manifests}),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(VALIDATOR_PATH), str(output_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_dependency_submission_validator_accepts_three_resolved_root_manifests(tmp_path):
    manifests = {
        manifest.name: _resolved_manifest(manifest.name)
        for manifest in EXPECTED_MANIFESTS
    }

    result = _run_validator(tmp_path, manifests)

    assert result.returncode == 0
    assert "validation passed" in result.stdout


def test_dependency_submission_validator_rejects_empty_snapshot(tmp_path):
    result = _run_validator(tmp_path, {})

    assert result.returncode == 1
    assert "Missing manifests" in result.stderr


def test_dependency_submission_validator_rejects_wrong_source_location(tmp_path):
    manifests = {
        manifest.name: _resolved_manifest(manifest.name)
        for manifest in EXPECTED_MANIFESTS
    }
    manifests["requirements.txt"]["file"] = {
        "source_location": ".github/dependency-submission/requirements.txt",
    }

    result = _run_validator(tmp_path, manifests)

    assert result.returncode == 1
    assert "Invalid source_location for requirements.txt" in result.stderr


def test_dependency_submission_validator_rejects_manifest_without_edges(tmp_path):
    manifests = {
        manifest.name: _resolved_manifest(manifest.name)
        for manifest in EXPECTED_MANIFESTS
    }
    for dependency in manifests["requirements.txt"]["resolved"].values():
        dependency["dependencies"] = []

    result = _run_validator(tmp_path, manifests)

    assert result.returncode == 1
    assert "No transitive dependency edges resolved for requirements.txt" in result.stderr
