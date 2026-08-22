# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Строит и отправляет GitHub snapshot для корневых requirements-файлов."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import (
    Request,
    urlopen,
)

try:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
except ModuleNotFoundError:  # pragma: no cover - setup-python предоставляет pip
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement

MANIFEST_SCOPES = {
    "requirements.txt": "runtime",
    "requirements-dev.txt": "development",
    "requirements-build.txt": "development",
}
DETECTOR_NAME = "shikiupdatesbot-pip-report"
DETECTOR_VERSION = "1"
DETECTOR_URL = "https://github.com/WorgaNomoR/ShikiUpdatesBot"
API_VERSION = "2026-03-10"


def _normalize_package_name(name: str) -> str:
    """Нормализует имя пакета по правилам Python Package Index."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _package_url(name: str, version: str) -> str:
    """Возвращает канонический Package URL для PyPI-пакета."""
    normalized_name = _normalize_package_name(name)
    return f"pkg:pypi/{quote(normalized_name, safe='-')}@{quote(version, safe='.+!-_')}"


def _generate_pip_report(manifest_path: Path) -> dict:
    """Получает разрешённый граф manifest через штатный отчёт pip."""
    with tempfile.TemporaryDirectory() as temporary_directory:
        report_path = Path(temporary_directory) / "pip-report.json"
        subprocess.run(  # nosec B603 - shell не используется, manifest задан кодом.
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--dry-run",
                "--ignore-installed",
                "--report",
                str(report_path),
                "--requirement",
                str(manifest_path),
            ],
            check=True,
        )
        return json.loads(report_path.read_text(encoding="utf-8"))


def _marker_applies(requirement: Requirement) -> bool:
    """Проверяет marker зависимости для текущего Python-окружения."""
    if requirement.marker is None:
        return True

    environment = default_environment()
    environment["extra"] = ""
    return requirement.marker.evaluate(environment)


def build_manifest_snapshot(
    manifest_name: str,
    scope: str,
    pip_report: object,
) -> dict:
    """Преобразует отчёт pip в один manifest Dependency Submission API."""
    if not isinstance(pip_report, dict):
        raise ValueError(f"pip report for {manifest_name} must be a JSON object")

    install_items = pip_report.get("install")
    if not isinstance(install_items, list) or not install_items:
        raise ValueError(f"pip report for {manifest_name} contains no packages")

    packages: dict[str, dict] = {}
    for item in install_items:
        if not isinstance(item, dict):
            raise ValueError(f"pip report for {manifest_name} contains an invalid package")

        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"pip report for {manifest_name} contains invalid metadata")

        name = metadata.get("name")
        version = metadata.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise ValueError(f"pip report for {manifest_name} contains an unversioned package")

        normalized_name = _normalize_package_name(name)
        if normalized_name in packages:
            raise ValueError(f"pip report for {manifest_name} repeats {normalized_name}")

        packages[normalized_name] = {
            "item": item,
            "package_url": _package_url(normalized_name, version),
        }

    resolved: dict[str, dict] = {}
    for normalized_name, package in packages.items():
        item = package["item"]
        metadata = item["metadata"]
        raw_requirements = metadata.get("requires_dist") or []
        if not isinstance(raw_requirements, list):
            raise ValueError(f"pip report for {manifest_name} contains invalid requirements")

        dependencies = []
        for raw_requirement in raw_requirements:
            if not isinstance(raw_requirement, str):
                raise ValueError(f"pip report for {manifest_name} contains an invalid requirement")
            requirement = Requirement(raw_requirement)
            dependency = packages.get(_normalize_package_name(requirement.name))
            if dependency is not None and _marker_applies(requirement):
                dependencies.append(dependency["package_url"])

        package_url = package["package_url"]
        resolved[package_url] = {
            "package_url": package_url,
            "relationship": "direct" if item.get("requested") is True else "indirect",
            "scope": scope,
            "dependencies": sorted(set(dependencies)),
        }

    direct_dependencies = [
        dependency
        for dependency in resolved.values()
        if dependency["relationship"] == "direct"
    ]
    if not direct_dependencies:
        raise ValueError(f"pip report for {manifest_name} contains no direct packages")
    if not any(dependency["dependencies"] for dependency in resolved.values()):
        raise ValueError(f"pip report for {manifest_name} contains no dependency edges")

    return {
        "name": manifest_name,
        "file": {"source_location": manifest_name},
        "resolved": resolved,
    }


def build_snapshot(repository_root: Path) -> dict:
    """Разрешает все канонические manifests и строит единый snapshot."""
    manifests = {}
    for manifest_name, scope in MANIFEST_SCOPES.items():
        manifest_path = repository_root / manifest_name
        pip_report = _generate_pip_report(manifest_path)
        manifests[manifest_name] = build_manifest_snapshot(
            manifest_name,
            scope,
            pip_report,
        )

    return {
        "version": 0,
        "sha": os.environ.get("GITHUB_SHA", "local-dry-run"),
        "ref": os.environ.get("GITHUB_REF", "refs/heads/local-dry-run"),
        "job": {
            "correlator": "shikiupdatesbot-python-3.12",
            "id": os.environ.get("GITHUB_RUN_ID", "local-dry-run"),
        },
        "detector": {
            "name": DETECTOR_NAME,
            "version": DETECTOR_VERSION,
            "url": DETECTOR_URL,
        },
        "scanned": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "manifests": manifests,
    }


def submit_snapshot(snapshot: object) -> dict:
    """Отправляет проверенный snapshot в GitHub Dependency Submission API."""
    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not token or not repository:
        raise ValueError("GITHUB_TOKEN and GITHUB_REPOSITORY are required for submission")

    request = Request(
        f"{api_url}/repos/{repository}/dependency-graph/snapshots",
        data=json.dumps(snapshot).encode("utf-8"),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": API_VERSION,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            if response.status != 201:
                raise ValueError(f"GitHub returned unexpected status {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"GitHub rejected dependency snapshot: {exc.code} {message}") from exc


def _parse_args() -> argparse.Namespace:
    """Разбирает режим работы и путь к snapshot."""
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("build", "submit"))
    parser.add_argument("snapshot", type=Path)
    return parser.parse_args()


def main() -> int:
    """Строит локальный файл либо отправляет уже проверенный snapshot."""
    args = _parse_args()
    try:
        if args.mode == "build":
            snapshot = build_snapshot(Path.cwd())
            args.snapshot.write_text(
                json.dumps(snapshot, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            package_count = sum(
                len(manifest["resolved"])
                for manifest in snapshot["manifests"].values()
            )
            print(
                f"Built dependency snapshot for {len(snapshot['manifests'])} "
                f"manifests and {package_count} resolved packages"
            )
        else:
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
            response = submit_snapshot(snapshot)
            print(response.get("message", "Dependency snapshot submitted"))
    except (
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        print(f"Dependency snapshot failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
