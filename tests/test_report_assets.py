# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Versioned materialization локальных Rich Message assets."""

import hashlib
from pathlib import Path

import pytest
from aiogram.types import BufferedInputFile

import report_assets
from report_asset_ids import (
    REPORT_POSTER_PLACEHOLDER_MEDIA,
    REPORT_POSTER_PLACEHOLDER_SHA256,
)
from report_assets import (
    ReportAssetError,
    materialize_rich_message,
)

ROOT = Path(__file__).resolve().parents[1]


def _message(media: str) -> dict:
    return {
        "blocks": [{
            "type": "photo",
            "photo": {"type": "photo", "media": media},
        }],
        "skip_entity_detection": True,
    }


def test_versioned_asset_is_loaded_as_exact_buffered_input_file():
    message = materialize_rich_message(_message(REPORT_POSTER_PLACEHOLDER_MEDIA))
    media = message.blocks[0].photo.media
    expected_bytes = (
        ROOT / "assets" / "report-poster-placeholder-v1.png"
    ).read_bytes()

    assert isinstance(media, BufferedInputFile)
    assert media.filename == "report-poster-placeholder-v1.png"
    assert media.data == expected_bytes
    assert REPORT_POSTER_PLACEHOLDER_SHA256 == hashlib.sha256(
        expected_bytes,
    ).hexdigest()
    assert REPORT_POSTER_PLACEHOLDER_SHA256 in REPORT_POSTER_PLACEHOLDER_MEDIA


def test_external_https_media_is_not_rewritten():
    media_url = "https://cdn.example.test/poster.jpg"

    message = materialize_rich_message(_message(media_url))

    assert message.blocks[0].photo.media == media_url


@pytest.mark.parametrize("create_corrupt_file", [False, True])
def test_missing_or_changed_versioned_asset_fails_before_send(
    monkeypatch,
    tmp_path,
    create_corrupt_file,
):
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    if create_corrupt_file:
        (asset_dir / "report-poster-placeholder-v1.png").write_bytes(b"changed")
    monkeypatch.setattr(report_assets, "RESOURCE_ROOT", tmp_path)

    expected = "asset_hash" if create_corrupt_file else "asset_unavailable"
    with pytest.raises(ReportAssetError, match=f"^{expected}$"):
        materialize_rich_message(_message(REPORT_POSTER_PLACEHOLDER_MEDIA))
