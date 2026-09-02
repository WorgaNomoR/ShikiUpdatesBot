# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии отправки разрешённого Python dependency graph."""

import importlib.util
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
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
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    ),
    (
        "Set up Python",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
    ),
]
EXPECTED_RUN_STEPS = [
    (
        "Resolve runtime dependencies",
        "python -m pip install --disable-pip-version-check --dry-run "
        "--ignore-installed --report dependency-report-runtime.json "
        "--requirement requirements.txt",
    ),
    (
        "Resolve development dependencies",
        "python -m pip install --disable-pip-version-check --dry-run "
        "--ignore-installed --report dependency-report-dev.json "
        "--requirement requirements-dev.txt",
    ),
    (
        "Resolve build dependencies",
        "python -m pip install --disable-pip-version-check --dry-run "
        "--ignore-installed --report dependency-report-build.json "
        "--requirement requirements-build.txt",
    ),
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


def _load_validator():
    spec = importlib.util.spec_from_file_location("dependency_validator", VALIDATOR_PATH)
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
        "GITHUB_TOKEN": "${{ github.token }}",  # nosec B105 - выражение Actions.
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


def test_dependency_submission_replaces_legacy_detector_snapshot():
    submitter = _load_submitter()

    assert submitter.DETECTOR_NAME == "Component Detection"
    assert submitter.DETECTOR_VERSION == "0.0.1"
    assert submitter.DETECTOR_URL == (
        "https://github.com/advanced-security/"
        "component-detection-dependency-submission-action"
    )
    assert submitter.CORRELATOR == "00-shikiupdatesbot-python-3.12"


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


@pytest.mark.parametrize(
    ("report", "message"),
    [
        ({}, "contains no packages"),
        (
            {
                "install": [
                    {
                        "requested": False,
                        "metadata": {
                            "name": "Indirect",
                            "version": "1.0",
                            "requires_dist": ["Child"],
                        },
                    },
                    {
                        "requested": False,
                        "metadata": {
                            "name": "Child",
                            "version": "2.0",
                            "requires_dist": [],
                        },
                    },
                ],
            },
            "contains no direct packages",
        ),
        (
            {
                "install": [
                    {
                        "requested": True,
                        "metadata": {
                            "name": "Standalone",
                            "version": "1.0",
                            "requires_dist": [],
                        },
                    },
                ],
            },
            "contains no dependency edges",
        ),
    ],
)
def test_dependency_snapshot_builder_rejects_incomplete_reports(report, message):
    submitter = _load_submitter()

    with pytest.raises(ValueError, match=message):
        submitter.build_manifest_snapshot("requirements.txt", "runtime", report)


class _ResponseStub:
    """Имитирует минимальный контекстный ответ urllib."""

    def __init__(self, status: int, body: bytes = b"{}"):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self) -> bytes:
        return self.body


def test_dependency_snapshot_submission_requires_github_environment(monkeypatch):
    submitter = _load_submitter()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    with pytest.raises(ValueError, match="GITHUB_TOKEN and GITHUB_REPOSITORY"):
        submitter.submit_snapshot({})


def test_dependency_snapshot_submission_uses_documented_api(monkeypatch):
    submitter = _load_submitter()
    captured_requests = []

    def urlopen_stub(request, timeout):
        captured_requests.append((request, timeout))
        return _ResponseStub(201, b'{"message":"accepted"}')

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    monkeypatch.setattr(submitter, "urlopen", urlopen_stub)

    response = submitter.submit_snapshot({"version": 0})

    request, timeout = captured_requests[0]
    assert request.full_url == (
        "https://api.github.com/repos/owner/repository/dependency-graph/snapshots"
    )
    assert request.get_header("Authorization") == "Bearer test-token"
    assert timeout == 60
    assert response == {"message": "accepted"}


def test_dependency_snapshot_submission_rejects_unexpected_status(monkeypatch):
    submitter = _load_submitter()
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    monkeypatch.setattr(
        submitter,
        "urlopen",
        lambda request, timeout: _ResponseStub(202),
    )

    with pytest.raises(ValueError, match="unexpected status 202"):
        submitter.submit_snapshot({})


def test_dependency_snapshot_submission_reports_http_error(monkeypatch):
    submitter = _load_submitter()
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")

    def urlopen_stub(request, timeout):
        raise HTTPError(
            request.full_url,
            422,
            "Unprocessable Entity",
            hdrs=None,
            fp=BytesIO(b'{"message":"invalid snapshot"}'),
        )

    monkeypatch.setattr(submitter, "urlopen", urlopen_stub)

    with pytest.raises(ValueError, match="422.*invalid snapshot"):
        submitter.submit_snapshot({})


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


def test_dependency_submission_validator_accepts_three_resolved_root_manifests():
    validator = _load_validator()
    manifests = {
        manifest.name: _resolved_manifest(manifest.name)
        for manifest in EXPECTED_MANIFESTS
    }

    validator.validate_dependency_submission({"manifests": manifests})


def test_dependency_submission_validator_rejects_empty_snapshot():
    validator = _load_validator()

    with pytest.raises(ValueError, match="Missing manifests"):
        validator.validate_dependency_submission({"manifests": {}})


def test_dependency_submission_validator_rejects_wrong_source_location():
    validator = _load_validator()
    manifests = {
        manifest.name: _resolved_manifest(manifest.name)
        for manifest in EXPECTED_MANIFESTS
    }
    manifests["requirements.txt"]["file"] = {
        "source_location": ".github/dependency-submission/requirements.txt",
    }

    with pytest.raises(ValueError, match="Invalid source_location for requirements.txt"):
        validator.validate_dependency_submission({"manifests": manifests})


def test_dependency_submission_validator_rejects_manifest_without_edges():
    validator = _load_validator()
    manifests = {
        manifest.name: _resolved_manifest(manifest.name)
        for manifest in EXPECTED_MANIFESTS
    }
    for dependency in manifests["requirements.txt"]["resolved"].values():
        dependency["dependencies"] = []

    with pytest.raises(
        ValueError,
        match="No transitive dependency edges resolved for requirements.txt",
    ):
        validator.validate_dependency_submission({"manifests": manifests})


def test_dependency_submission_validator_rejects_empty_version():
    validator = _load_validator()
    manifests = {
        manifest.name: _resolved_manifest(manifest.name)
        for manifest in EXPECTED_MANIFESTS
    }
    resolved = manifests["requirements.txt"]["resolved"]
    direct_url = next(iter(resolved))
    dependency = deepcopy(resolved.pop(direct_url))
    empty_version_url = direct_url.rpartition("@")[0] + "@"
    dependency["package_url"] = empty_version_url
    resolved[empty_version_url] = dependency

    with pytest.raises(ValueError, match="Unversioned dependency key"):
        validator.validate_dependency_submission({"manifests": manifests})
