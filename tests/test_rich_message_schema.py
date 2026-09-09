# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Официальные границы безопасного Rich Message payload subset."""

import pytest

from report_asset_ids import REPORT_POSTER_PLACEHOLDER_MEDIA
from rich_message_schema import (
    RICH_BLOCK_LIMIT,
    RICH_MEDIA_LIMIT,
    RICH_NESTING_LIMIT,
    RICH_TABLE_COLUMN_LIMIT,
    RICH_TEXT_LIMIT,
    RichMessageValidationError,
    validate_rich_payload,
)


def _message(*blocks):
    return {"blocks": list(blocks), "skip_entity_detection": True}


def _paragraph(text):
    return {"type": "paragraph", "text": text}


def _photo(media="https://cdn.example.test/poster.jpg"):
    return {
        "type": "photo",
        "photo": {"type": "photo", "media": media},
    }


@pytest.mark.parametrize("extra", [0, 1])
def test_rich_character_boundary_counts_unicode_code_points(extra):
    text = "😀" * (RICH_TEXT_LIMIT + extra)
    payload = _message(_paragraph(text))
    if extra:
        with pytest.raises(RichMessageValidationError, match="^characters$"):
            validate_rich_payload(payload)
    else:
        assert validate_rich_payload(payload) is payload


def test_rich_text_must_be_valid_utf8():
    with pytest.raises(RichMessageValidationError, match="^text_encoding$"):
        validate_rich_payload(_message(_paragraph("\ud800")))


@pytest.mark.parametrize(
    "within,over",
    [
        (
            _message(*(
                {"type": "anchor", "name": f"unit-{index}-top"}
                for index in range(RICH_BLOCK_LIMIT)
            )),
            _message(*(
                {"type": "anchor", "name": f"unit-{index}-top"}
                for index in range(RICH_BLOCK_LIMIT + 1)
            )),
        ),
        (
            _message({
                "type": "list",
                "items": [{"blocks": []} for _ in range(RICH_BLOCK_LIMIT - 1)],
            }),
            _message({
                "type": "list",
                "items": [{"blocks": []} for _ in range(RICH_BLOCK_LIMIT)],
            }),
        ),
        (
            _message({
                "type": "table",
                "cells": [[] for _ in range(RICH_BLOCK_LIMIT - 1)],
                "is_bordered": True,
                "is_striped": True,
                "is_compact": True,
            }),
            _message({
                "type": "table",
                "cells": [[] for _ in range(RICH_BLOCK_LIMIT)],
                "is_bordered": True,
                "is_striped": True,
                "is_compact": True,
            }),
        ),
        (
            _message(*(
                {
                    "type": "details",
                    "summary": "",
                    "blocks": [],
                    "is_open": True,
                }
                for _ in range(RICH_BLOCK_LIMIT)
            )),
            _message(*(
                {
                    "type": "details",
                    "summary": "",
                    "blocks": [],
                    "is_open": True,
                }
                for _ in range(RICH_BLOCK_LIMIT + 1)
            )),
        ),
    ],
    ids=["blocks", "list-items", "table-rows", "details"],
)
def test_rich_block_boundary_counts_every_documented_unit(within, over):
    validate_rich_payload(within)
    with pytest.raises(RichMessageValidationError, match="^blocks$"):
        validate_rich_payload(over)


def _nested_details(details_count: int) -> dict:
    block = _paragraph("deep")
    for _ in range(details_count):
        block = {
            "type": "details",
            "summary": "summary",
            "blocks": [block],
            "is_open": True,
        }
    return block


def _nested_text_lists(list_count: int) -> list:
    value: str | list = "deep"
    for _ in range(list_count):
        value = [value]
    return value


def test_rich_nesting_boundary_includes_nested_blocks():
    validate_rich_payload(_message(_nested_details(RICH_NESTING_LIMIT - 1)))
    with pytest.raises(RichMessageValidationError, match="^nesting$"):
        validate_rich_payload(_message(_nested_details(RICH_NESTING_LIMIT)))


@pytest.mark.parametrize("block", [
    _nested_details(2000),
    _paragraph(_nested_text_lists(2000)),
])
def test_excessive_nesting_fails_before_python_recursion_limit(block):
    with pytest.raises(RichMessageValidationError, match="^nesting$"):
        validate_rich_payload(_message(block))


def _table(columns: int) -> dict:
    return {
        "type": "table",
        "cells": [[
            {"align": "left", "valign": "middle", "text": str(index)}
            for index in range(columns)
        ]],
        "is_bordered": True,
        "is_striped": True,
        "is_compact": True,
    }


def test_rich_table_column_boundary_is_exact():
    validate_rich_payload(_message(_table(RICH_TABLE_COLUMN_LIMIT)))
    with pytest.raises(RichMessageValidationError, match="^table_columns$"):
        validate_rich_payload(_message(_table(RICH_TABLE_COLUMN_LIMIT + 1)))


def test_rich_media_boundary_counts_nested_and_top_level_photos():
    validate_rich_payload(_message(*(
        _photo(f"https://cdn.example.test/{index}.jpg")
        for index in range(RICH_MEDIA_LIMIT)
    )))
    with pytest.raises(RichMessageValidationError, match="^media$"):
        validate_rich_payload(_message(*(
            _photo(f"https://cdn.example.test/{index}.jpg")
            for index in range(RICH_MEDIA_LIMIT + 1)
        )))
    def collage():
        return {
            "type": "collage",
            "blocks": [
                _photo(f"https://cdn.example.test/nested-{index}.jpg")
                for index in range(2)
            ],
        }

    validate_rich_payload(_message(
        collage(),
        *(
            _photo(f"https://cdn.example.test/top-{index}.jpg")
            for index in range(RICH_MEDIA_LIMIT - 2)
        ),
    ))
    with pytest.raises(RichMessageValidationError, match="^media$"):
        validate_rich_payload(_message(
            collage(),
            *(
                _photo(f"https://cdn.example.test/over-{index}.jpg")
                for index in range(RICH_MEDIA_LIMIT - 1)
            ),
        ))


def test_collage_accepts_two_or_three_photos_and_counts_nested_blocks():
    for count in (2, 3):
        validate_rich_payload(_message({
            "type": "collage",
            "blocks": [_photo() for _ in range(count)],
        }))
    for count in (1, 4):
        with pytest.raises(RichMessageValidationError, match="^collage_fields$"):
            validate_rich_payload(_message({
                "type": "collage",
                "blocks": [_photo() for _ in range(count)],
            }))


@pytest.mark.parametrize(
    "media",
    [
        "http://cdn.example.test/poster.jpg",
        "javascript:alert(1)",
        "https://user:password@cdn.example.test/poster.jpg",
        "https://cdn.example.test/poster.jpg\nignored",
        "asset://unknown",
    ],
)
def test_hostile_or_unknown_media_references_are_rejected(media):
    with pytest.raises(RichMessageValidationError, match="^media_url$"):
        validate_rich_payload(_message(_photo(media)))


@pytest.mark.parametrize("url", [
    "http://example.test/title",
    "javascript:alert(1)",
    "https://user:password@example.test/title",
    "https://example.test/title\nignored",
])
def test_rich_text_urls_use_the_safe_https_policy(url):
    with pytest.raises(RichMessageValidationError, match="^rich_url$"):
        validate_rich_payload(_message(_paragraph({
            "type": "url",
            "text": "title",
            "url": url,
        })))


def test_exact_versioned_local_asset_reference_is_allowed():
    payload = _message(_photo(REPORT_POSTER_PLACEHOLDER_MEDIA))

    assert validate_rich_payload(payload) is payload


def test_hostile_anchor_names_are_rejected_in_targets_and_links():
    with pytest.raises(RichMessageValidationError, match="^anchor_name$"):
        validate_rich_payload(_message({
            "type": "anchor",
            "name": '"><script>alert(1)</script>',
        }))
    with pytest.raises(RichMessageValidationError, match="^anchor_link$"):
        validate_rich_payload(_message(_paragraph({
            "type": "anchor_link",
            "text": "jump",
            "anchor_name": "../../hostile",
        })))


def test_subscript_anchor_link_is_counted_as_nested_rich_text():
    payload = _message(_paragraph({
        "type": "anchor_link",
        "text": {
            "type": "subscript",
            "text": "↑ К началу",
        },
        "anchor_name": "unit-0-top",
    }))

    assert validate_rich_payload(payload) is payload


def test_subscript_participates_in_exact_rich_text_nesting_boundary():
    def nested_text(wrapper_count: int) -> dict | str:
        value: dict | str = "deep"
        for _ in range(wrapper_count):
            value = {"type": "subscript", "text": value}
        return value

    validate_rich_payload(_message(_paragraph(nested_text(RICH_NESTING_LIMIT - 1))))
    with pytest.raises(RichMessageValidationError, match="^nesting$"):
        validate_rich_payload(_message(_paragraph(nested_text(RICH_NESTING_LIMIT))))
