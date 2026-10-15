# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Versioned materialization локальных Rich Message assets."""

import hashlib
from pathlib import Path

import pytest
from aiogram.types import BufferedInputFile

import report_assets
from report_asset_ids import (
    REPORT_ACHIEVEMENTS_MEDIA,
    REPORT_POSTER_PLACEHOLDER_MEDIA,
    REPORT_POSTER_PLACEHOLDER_SHA256,
    REPORT_POSTER_PLACEHOLDER_V1_MEDIA,
    REPORT_POSTER_PLACEHOLDER_V1_SHA256,
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


@pytest.mark.parametrize(("reference", "filename", "expected_hash"), [
    (
        REPORT_POSTER_PLACEHOLDER_V1_MEDIA,
        "report-poster-placeholder-v1.png",
        REPORT_POSTER_PLACEHOLDER_V1_SHA256,
    ),
    (
        REPORT_POSTER_PLACEHOLDER_MEDIA,
        "report-poster-placeholder-v2.jpg",
        REPORT_POSTER_PLACEHOLDER_SHA256,
    ),
])
def test_versioned_asset_is_loaded_as_exact_buffered_input_file(
    reference,
    filename,
    expected_hash,
):
    message = materialize_rich_message(_message(reference))
    media = message.blocks[0].photo.media
    expected_bytes = (ROOT / "assets" / filename).read_bytes()

    assert isinstance(media, BufferedInputFile)
    assert media.filename == filename
    assert media.data == expected_bytes
    assert expected_hash == hashlib.sha256(
        expected_bytes,
    ).hexdigest()
    assert expected_hash in reference


def test_achievement_artwork_uses_packaged_main_menu_jpeg():
    message = materialize_rich_message(_message(REPORT_ACHIEVEMENTS_MEDIA))
    media = message.blocks[0].photo.media

    assert isinstance(media, BufferedInputFile)
    assert media.filename == "achievements-v1.jpg"
    assert media.data == (ROOT / "assets" / "main-menu" / media.filename).read_bytes()


def test_interactive_artwork_can_be_replaced_without_updating_a_hash(monkeypatch, tmp_path):
    asset = tmp_path / "assets" / "main-menu" / "achievements-v1.jpg"
    asset.parent.mkdir(parents=True)
    monkeypatch.setattr(report_assets, "RESOURCE_ROOT", tmp_path)
    payload = _message(REPORT_ACHIEVEMENTS_MEDIA)

    asset.write_bytes(b"first jpeg")
    first = materialize_rich_message(payload).blocks[0].photo.media
    asset.write_bytes(b"resized jpeg")
    second = materialize_rich_message(payload).blocks[0].photo.media

    assert first.data == b"first jpeg"
    assert second.data == b"resized jpeg"
    assert first is not second
    assert payload == _message(REPORT_ACHIEVEMENTS_MEDIA)


@pytest.mark.parametrize("empty", [False, True])
def test_missing_or_empty_achievement_artwork_fails_before_send(monkeypatch, tmp_path, empty):
    asset = tmp_path / "assets" / "main-menu" / "achievements-v1.jpg"
    asset.parent.mkdir(parents=True)
    if empty:
        asset.write_bytes(b"")
    monkeypatch.setattr(report_assets, "RESOURCE_ROOT", tmp_path)

    with pytest.raises(ReportAssetError, match="^asset_unavailable$"):
        materialize_rich_message(_message(REPORT_ACHIEVEMENTS_MEDIA))


def test_external_https_media_is_not_rewritten():
    media_url = "https://cdn.example.test/poster.jpg"

    message = materialize_rich_message(_message(media_url))

    assert message.blocks[0].photo.media == media_url


@pytest.mark.parametrize("create_corrupt_file", [False, True])
@pytest.mark.parametrize(("reference", "filename"), [
    (REPORT_POSTER_PLACEHOLDER_V1_MEDIA, "report-poster-placeholder-v1.png"),
    (REPORT_POSTER_PLACEHOLDER_MEDIA, "report-poster-placeholder-v2.jpg"),
])
def test_missing_or_changed_versioned_asset_fails_before_send(
    monkeypatch,
    tmp_path,
    create_corrupt_file,
    reference,
    filename,
):
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    if create_corrupt_file:
        (asset_dir / filename).write_bytes(b"changed")
    monkeypatch.setattr(report_assets, "RESOURCE_ROOT", tmp_path)

    expected = "asset_hash" if create_corrupt_file else "asset_unavailable"
    with pytest.raises(ReportAssetError, match=f"^{expected}$"):
        materialize_rich_message(_message(reference))
