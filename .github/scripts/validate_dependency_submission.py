# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Проверяет, что Component Detection разрешил все корневые Python manifests."""

import json
import sys
from pathlib import Path

EXPECTED_MANIFESTS = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-build.txt",
)


def _repository_relative_path(raw_path: str, repository_root: Path) -> str | None:
    """Возвращает путь из отчёта относительно корня репозитория."""
    path = Path(raw_path)
    if not path.is_absolute():
        return path.as_posix()

    try:
        return path.relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return None


def validate_dependency_submission(payload: object, repository_root: Path) -> None:
    """Отклоняет пустой, неполный или неразрешённый dependency graph."""
    if not isinstance(payload, dict):
        raise ValueError("Component Detection output must be a JSON object")

    dependency_graphs = payload.get("dependencyGraphs")
    if not isinstance(dependency_graphs, dict):
        raise ValueError("Component Detection output has no dependencyGraphs object")

    normalized_graphs = {
        relative_path: graph
        for raw_path, graph in dependency_graphs.items()
        if isinstance(raw_path, str)
        and (relative_path := _repository_relative_path(raw_path, repository_root))
        is not None
    }
    missing = sorted(set(EXPECTED_MANIFESTS) - normalized_graphs.keys())
    if missing:
        raise ValueError(f"Missing dependency graphs: {', '.join(missing)}")

    for manifest in EXPECTED_MANIFESTS:
        graph = normalized_graphs[manifest]
        if not isinstance(graph, dict):
            raise ValueError(f"Invalid dependency graph for {manifest}")

        direct_dependencies = graph.get("explicitlyReferencedComponentIds")
        if not isinstance(direct_dependencies, list) or not direct_dependencies:
            raise ValueError(f"No direct dependencies resolved for {manifest}")

        dependency_edges = graph.get("graph")
        has_edges = isinstance(dependency_edges, dict) and any(
            isinstance(children, list) and children
            for children in dependency_edges.values()
        )
        if not has_edges:
            raise ValueError(f"No transitive dependency edges resolved for {manifest}")


def main() -> int:
    """Читает отчёт Component Detection и возвращает код результата проверки."""
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output.json")
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        validate_dependency_submission(payload, Path.cwd())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Dependency submission validation failed: {exc}", file=sys.stderr)
        return 1

    print("Dependency submission validation passed for all root manifests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
