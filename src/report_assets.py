# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Локальные иллюстрации Rich-сообщений и проверка сохранённых ресурсов."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

from aiogram.types import (
    BufferedInputFile,
    InputRichMessage,
)

from report_asset_ids import (
    REPORT_ACHIEVEMENTS_MEDIA,
    REPORT_POSTER_PLACEHOLDER_MEDIA,
    REPORT_POSTER_PLACEHOLDER_SHA256,
    REPORT_POSTER_PLACEHOLDER_V1_MEDIA,
    REPORT_POSTER_PLACEHOLDER_V1_SHA256,
)
from runtime import RESOURCE_ROOT

_REPORT_ASSETS = {
    REPORT_ACHIEVEMENTS_MEDIA: ("main-menu/achievements-v1.jpg", None),
    REPORT_POSTER_PLACEHOLDER_V1_MEDIA: (
        "report-poster-placeholder-v1.png",
        REPORT_POSTER_PLACEHOLDER_V1_SHA256,
    ),
    REPORT_POSTER_PLACEHOLDER_MEDIA: (
        "report-poster-placeholder-v2.jpg",
        REPORT_POSTER_PLACEHOLDER_SHA256,
    ),
}


class ReportAssetError(ValueError):
    """Локальная иллюстрация недоступна или изменилась вопреки сохранённому хэшу."""


def is_report_asset_media(value: object) -> bool:
    """Распознать только явно разрешённую ссылку на встроенную иллюстрацию."""
    return isinstance(value, str) and value in _REPORT_ASSETS


def _materialized_asset(reference: str) -> BufferedInputFile:
    filename, expected_hash = _REPORT_ASSETS[reference]
    path = RESOURCE_ROOT / "assets" / filename
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ReportAssetError("asset_unavailable") from exc
    if not content:
        raise ReportAssetError("asset_unavailable")
    if expected_hash is not None and sha256(content).hexdigest() != expected_hash:
        raise ReportAssetError("asset_hash")
    return BufferedInputFile(content, filename=path.name)


def materialize_rich_message(payload: dict) -> InputRichMessage:
    """Загрузить разрешённые иллюстрации и проверить хэши сохранённых ресурсов."""
    content = deepcopy(payload)

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        media = value.get("media")
        if is_report_asset_media(media):
            value["media"] = _materialized_asset(media)
        for child in value.values():
            visit(child)

    visit(content)
    return InputRichMessage.model_validate(content)
