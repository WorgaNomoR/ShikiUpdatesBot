# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии политики обновления зависимостей."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPENDABOT_PATH = ROOT / ".github" / "dependabot.yml"
DOCKERFILE_PATH = ROOT / "Dockerfile"

EXPECTED_LIMITS = {
    "pip": 3,
    "docker": 1,
    "github-actions": 2,
}
EXPECTED_PIP_GROUPS = {
    "runtime-minor-and-patch": {
        "applies-to": "version-updates",
        "patterns": ["aiogram", "aiohttp", "python-dotenv", "pytrovich"],
        "update-types": ["minor", "patch"],
    },
    "test-minor-and-patch": {
        "applies-to": "version-updates",
        "patterns": ["pytest", "pytest-*", "pyyaml"],
        "update-types": ["minor", "patch"],
    },
    "build-minor-and-patch": {
        "applies-to": "version-updates",
        "patterns": ["pyinstaller", "pyinstaller-hooks-contrib"],
        "update-types": ["minor", "patch"],
    },
}


def _load_dependabot() -> dict:
    return yaml.safe_load(DEPENDABOT_PATH.read_text(encoding="utf-8"))


def _updates_by_ecosystem() -> dict[str, dict]:
    config = _load_dependabot()
    return {entry["package-ecosystem"]: entry for entry in config["updates"]}


def test_dependabot_covers_only_the_approved_root_ecosystems():
    config = _load_dependabot()
    updates = _updates_by_ecosystem()

    assert config["version"] == 2
    assert set(config) == {"version", "updates"}
    assert set(updates) == set(EXPECTED_LIMITS)
    assert len(config["updates"]) == len(EXPECTED_LIMITS)
    assert all(entry["directory"] == "/" for entry in updates.values())
    assert all("multi-ecosystem-group" not in entry for entry in updates.values())


def test_dependabot_uses_monthly_bounded_version_updates():
    updates = _updates_by_ecosystem()

    for ecosystem, expected_limit in EXPECTED_LIMITS.items():
        entry = updates[ecosystem]
        assert entry["schedule"] == {"interval": "monthly"}
        assert entry["open-pull-requests-limit"] == expected_limit
        assert "target-branch" not in entry


def test_pip_minor_and_patch_updates_are_grouped_by_role():
    pip_updates = _updates_by_ecosystem()["pip"]

    assert pip_updates["groups"] == EXPECTED_PIP_GROUPS


def test_docker_updates_cannot_change_the_python_minor():
    docker_updates = _updates_by_ecosystem()["docker"]
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert dockerfile.splitlines()[0] == "FROM python:3.12-slim"
    assert docker_updates["ignore"] == [
        {
            "dependency-name": "python",
            "update-types": [
                "version-update:semver-minor",
                "version-update:semver-major",
            ],
        }
    ]


def test_action_updates_stay_in_their_ecosystem_and_leave_majors_separate():
    action_updates = _updates_by_ecosystem()["github-actions"]

    assert action_updates["groups"] == {
        "actions-minor-and-patch": {
            "applies-to": "version-updates",
            "patterns": ["*"],
            "update-types": ["minor", "patch"],
        }
    }
