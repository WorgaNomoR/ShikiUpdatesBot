# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Versioned локальные media assets для замороженных Rich Message plans."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

from aiogram.types import (
    BufferedInputFile,
    InputRichMessage,
)

from report_asset_ids import (
    REPORT_POSTER_PLACEHOLDER_MEDIA,
    REPORT_POSTER_PLACEHOLDER_SHA256,
)
from runtime import RESOURCE_ROOT

_REPORT_ASSETS = {
    REPORT_POSTER_PLACEHOLDER_MEDIA: (
        "report-poster-placeholder-v1.png",
        REPORT_POSTER_PLACEHOLDER_SHA256,
    ),
}


class ReportAssetError(ValueError):
    """Замороженный локальный asset отсутствует или изменился."""


def is_report_asset_media(value: object) -> bool:
    """Распознать только известный versioned media identifier."""
    return isinstance(value, str) and value in _REPORT_ASSETS


def _materialized_asset(reference: str) -> BufferedInputFile:
    filename, expected_hash = _REPORT_ASSETS[reference]
    path = RESOURCE_ROOT / "assets" / filename
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ReportAssetError("asset_unavailable") from exc
    if sha256(content).hexdigest() != expected_hash:
        raise ReportAssetError("asset_hash")
    return BufferedInputFile(content, filename=filename)


def materialize_rich_message(payload: dict) -> InputRichMessage:
    """Заменить frozen asset identifiers точными проверенными байтами."""
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
