# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Проверяет snapshot корневых Python manifests перед отправкой в GitHub."""

import json
import sys
from pathlib import Path

EXPECTED_MANIFESTS = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-build.txt",
)


def validate_dependency_submission(payload: object) -> None:
    """Отклоняет пустой, неполный или неразрешённый dependency graph."""
    if not isinstance(payload, dict):
        raise ValueError("Dependency snapshot must be a JSON object")

    manifests = payload.get("manifests")
    if not isinstance(manifests, dict):
        raise ValueError("Dependency snapshot has no manifests object")

    missing = sorted(set(EXPECTED_MANIFESTS) - manifests.keys())
    if missing:
        raise ValueError(f"Missing manifests: {', '.join(missing)}")
    unexpected = sorted(manifests.keys() - set(EXPECTED_MANIFESTS))
    if unexpected:
        raise ValueError(f"Unexpected manifests: {', '.join(unexpected)}")

    for manifest in EXPECTED_MANIFESTS:
        manifest_snapshot = manifests[manifest]
        if not isinstance(manifest_snapshot, dict):
            raise ValueError(f"Invalid manifest snapshot for {manifest}")
        if manifest_snapshot.get("name") != manifest:
            raise ValueError(f"Invalid manifest name for {manifest}")
        if manifest_snapshot.get("file") != {"source_location": manifest}:
            raise ValueError(f"Invalid source_location for {manifest}")

        resolved = manifest_snapshot.get("resolved")
        if not isinstance(resolved, dict) or not resolved:
            raise ValueError(f"No resolved dependencies for {manifest}")
        if not any(
            isinstance(dependency, dict)
            and dependency.get("relationship") == "direct"
            for dependency in resolved.values()
        ):
            raise ValueError(f"No direct dependencies resolved for {manifest}")

        has_edges = any(
            isinstance(dependency, dict)
            and isinstance(dependency.get("dependencies"), list)
            and dependency["dependencies"]
            for dependency in resolved.values()
        )
        if not has_edges:
            raise ValueError(f"No transitive dependency edges resolved for {manifest}")

        for key, dependency in resolved.items():
            if not isinstance(key, str) or not key.rpartition("@")[2]:
                raise ValueError(f"Unversioned dependency key in {manifest}")
            if not isinstance(dependency, dict) or dependency.get("package_url") != key:
                raise ValueError(f"Invalid dependency entry in {manifest}")
            children = dependency.get("dependencies")
            if not isinstance(children, list) or any(child not in resolved for child in children):
                raise ValueError(f"Invalid dependency edge in {manifest}")


def main() -> int:
    """Читает snapshot и возвращает код результата проверки."""
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dependency-snapshot.json")
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        validate_dependency_submission(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Dependency submission validation failed: {exc}", file=sys.stderr)
        return 1

    print("Dependency submission validation passed for all root manifests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
