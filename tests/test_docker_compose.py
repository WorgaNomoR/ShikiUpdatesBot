# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии конфигурационного контракта Docker Compose."""

import re
from pathlib import Path

import yaml

from main_menu import MAIN_MENU_ASSETS

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.yml"
DOCKERIGNORE_PATH = COMPOSE_PATH.with_name(".dockerignore")
DOCKERFILE_PATH = COMPOSE_PATH.with_name("Dockerfile")
REQUIRED_ENV_VARS = {"BOT_TOKEN", "OWNER_ID", "SHIKI_USER"}
REQUIRED_INTERPOLATION = re.compile(r"^\$\{([A-Z][A-Z0-9_]*):\?[^}]+\}$")


def _load_service() -> dict:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return compose["services"]["ShikiUpdatesBot"]


def test_compose_requires_every_application_setting():
    environment = _load_service()["environment"]
    for name in REQUIRED_ENV_VARS:
        value = environment.get(name)
        match = (
            REQUIRED_INTERPOLATION.fullmatch(value)
            if isinstance(value, str)
            else None
        )

        assert match is not None
        assert match.group(1) == name


def test_compose_loads_env_file_but_keeps_data_volume_invariant():
    service = _load_service()

    assert service["restart"] == "unless-stopped"
    assert service["env_file"] == [".env"]
    assert service["environment"]["DATA_DIR"] == "/data"
    assert "./data:/data" in service["volumes"]


def test_docker_context_keeps_required_runtime_files():
    patterns = DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    active_patterns = [
        pattern.strip()
        for pattern in patterns
        if pattern.strip() and not pattern.lstrip().startswith("#")
    ]
    relevant = [
        pattern
        for pattern in patterns
        if pattern.lstrip("!").startswith("assets/")
    ]

    assert relevant == [
        "assets/*",
        "!assets/info-preview.png",
        "!assets/report-poster-placeholder-v1.png",
        "!assets/report-poster-placeholder-v2.jpg",
        "!assets/main-menu/",
        "!assets/main-menu/*.jpg",
    ]
    assert "assets/" not in patterns
    assert active_patterns[-1] == "!examples/facts.json"


def test_docker_build_requires_runtime_assets_in_effective_context():
    instructions = DOCKERFILE_PATH.read_text(encoding="utf-8").splitlines()
    dockerfile = "\n".join(instructions)

    assert "RUN test -f /app/assets/info-preview.png" in instructions
    assert (
        "RUN test -f /app/assets/report-poster-placeholder-v1.png"
        in instructions
    )
    assert (
        "RUN test -f /app/assets/report-poster-placeholder-v2.jpg"
        in instructions
    )
    asset_loop_start = dockerfile.index("for asset in")
    asset_loop_end = dockerfile.index("done;", asset_loop_start)
    asset_loop = dockerfile[asset_loop_start:asset_loop_end]
    assert 'test -f "/app/assets/main-menu/$asset"' in asset_loop
    for filename in MAIN_MENU_ASSETS.values():
        assert filename in asset_loop
    assert (
        "find /app/assets/main-menu -maxdepth 1 -type f -name '*.jpg'"
        in dockerfile
    )
    assert f'wc -l)" -eq {len(MAIN_MENU_ASSETS)}' in dockerfile
    assert "RUN test -f /app/examples/facts.json" in instructions
