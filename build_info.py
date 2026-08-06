# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Идентичность запуска и сборки, встраиваемая PyInstaller spec-файлом.

При запуске из исходников используются общие метаданные проекта; PyInstaller
подменяет их данными конкретной CI-сборки.
"""

from __future__ import annotations

import re

from project_meta import PROJECT_REPOSITORY, PROJECT_VERSION

try:
    from _build_info import (  # type: ignore[import-not-found]
        APP_API_URL,
        APP_REPOSITORY,
        APP_SERVER_URL,
        APP_VERSION,
    )
except ImportError:
    APP_VERSION = PROJECT_VERSION
    APP_REPOSITORY = PROJECT_REPOSITORY
    APP_SERVER_URL = "https://github.com"
    APP_API_URL = "https://api.github.com"

_SEMVER_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
HAS_RELEASE_INFO = bool(_SEMVER_RE.fullmatch(APP_VERSION) and "/" in APP_REPOSITORY)
REPOSITORY_URL = (
    f"{APP_SERVER_URL.rstrip('/')}/{APP_REPOSITORY}"
    if APP_REPOSITORY
    else ""
)
RELEASES_URL = (
    f"{REPOSITORY_URL}/releases/latest"
    if REPOSITORY_URL
    else ""
)
LATEST_RELEASE_API = (
    f"{APP_API_URL.rstrip('/')}/repos/{APP_REPOSITORY}/releases/latest"
    if APP_REPOSITORY
    else ""
)


def semver_tuple(value: str) -> tuple[int, int, int] | None:
    """Строгий парсер vMAJOR.MINOR.PATCH для уведомлений об обновлениях."""
    match = _SEMVER_RE.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())
