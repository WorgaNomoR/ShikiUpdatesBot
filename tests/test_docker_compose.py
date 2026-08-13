# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Статические гарантии конфигурационного контракта Docker Compose."""

import re
from pathlib import Path

import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.yml"
REQUIRED_ENV_VARS = {"BOT_TOKEN", "OWNER_ID", "SHIKI_USER"}
REQUIRED_INTERPOLATION = re.compile(r"^\$\{([A-Z][A-Z0-9_]*):\?[^}]+\}$")


def _load_service() -> dict:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return compose["services"]["ShikiUpdatesBot"]


def test_compose_requires_every_application_setting():
    environment = _load_service()["environment"]
    required = {
        match.group(1)
        for value in environment.values()
        if isinstance(value, str) and (match := REQUIRED_INTERPOLATION.fullmatch(value))
    }

    assert required == REQUIRED_ENV_VARS


def test_compose_loads_env_file_but_keeps_data_volume_invariant():
    service = _load_service()

    assert service["restart"] == "unless-stopped"
    assert service["env_file"] == [".env"]
    assert service["environment"]["DATA_DIR"] == "/data"
    assert "./data:/data" in service["volumes"]
