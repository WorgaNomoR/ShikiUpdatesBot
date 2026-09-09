# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистая проверка сериализованных Bot API Rich Message payloads."""

import re
from urllib.parse import urlsplit

from report_asset_ids import REPORT_POSTER_PLACEHOLDER_MEDIA

RICH_TEXT_LIMIT = 32768
RICH_BLOCK_LIMIT = 500
RICH_NESTING_LIMIT = 16
RICH_MEDIA_LIMIT = 50
RICH_TABLE_COLUMN_LIMIT = 20

_ANCHOR_NAME = re.compile(r"unit-[0-9]+-top")


class RichMessageValidationError(ValueError):
    """Rich payload не соответствует локальному безопасному подмножеству."""


def is_safe_https_media_url(value: object) -> bool:
    """Проверить цельный HTTPS URL без credentials и control characters."""
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and bool(hostname)
        and parsed.username is None
        and parsed.password is None
    )


class _Counter:
    """Накопить официальные лимиты без привязки к aiogram."""

    def __init__(self) -> None:
        self.characters = 0
        self.blocks = 0
        self.depth = 0
        self.media = 0

    def _record_depth(self, depth: int) -> None:
        """Зафиксировать глубину до обхода дочернего содержимого."""
        self.depth = max(self.depth, depth)
        if depth > RICH_NESTING_LIMIT:
            raise RichMessageValidationError("nesting")

    def media_reference(self, value: object) -> None:
        """Проверить внешний HTTPS URL или известный локальный asset id."""
        if value == REPORT_POSTER_PLACEHOLDER_MEDIA:
            return
        if not is_safe_https_media_url(value):
            raise RichMessageValidationError("media_url")

    def text(self, value: object, depth: int) -> None:
        self._record_depth(depth)
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise RichMessageValidationError("text_encoding") from None
            self.characters += len(value)
            return
        if isinstance(value, list):
            for part in value:
                self.text(part, depth + 1)
            return
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise RichMessageValidationError("rich_text_type")
        kind = value["type"]
        if kind in {"bold", "italic", "subscript"}:
            if set(value) != {"type", "text"}:
                raise RichMessageValidationError("rich_text_fields")
            self.text(value["text"], depth + 1)
            return
        if kind == "url":
            if (
                set(value) != {"type", "text", "url"}
                or not is_safe_https_media_url(value["url"])
            ):
                raise RichMessageValidationError("rich_url")
            self.text(value["text"], depth + 1)
            return
        if kind == "anchor_link":
            if (
                set(value) != {"type", "text", "anchor_name"}
                or not isinstance(value["anchor_name"], str)
                or _ANCHOR_NAME.fullmatch(value["anchor_name"]) is None
            ):
                raise RichMessageValidationError("anchor_link")
            self.text(value["text"], depth + 1)
            return
        raise RichMessageValidationError("rich_text_kind")

    def block(self, value: object, depth: int) -> None:
        self._record_depth(depth)
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise RichMessageValidationError("block_type")
        self.blocks += 1
        kind = value["type"]
        if kind == "paragraph":
            if set(value) != {"type", "text"}:
                raise RichMessageValidationError("paragraph_fields")
            self.text(value["text"], depth)
            return
        if kind == "heading":
            if (
                set(value) != {"type", "text", "size"}
                or type(value["size"]) is not int
                or not 1 <= value["size"] <= 6
            ):
                raise RichMessageValidationError("heading_fields")
            self.text(value["text"], depth)
            return
        if kind == "anchor":
            if (
                set(value) != {"type", "name"}
                or not isinstance(value["name"], str)
                or _ANCHOR_NAME.fullmatch(value["name"]) is None
            ):
                raise RichMessageValidationError("anchor_name")
            return
        if kind == "list":
            if set(value) != {"type", "items"} or not isinstance(value["items"], list):
                raise RichMessageValidationError("list_fields")
            for item in value["items"]:
                self.list_item(item, depth + 1)
            return
        if kind == "table":
            expected = {"type", "cells", "is_bordered", "is_striped", "is_compact"}
            if set(value) != expected or not isinstance(value["cells"], list):
                raise RichMessageValidationError("table_fields")
            if any(value[field] is not True for field in expected - {"type", "cells"}):
                raise RichMessageValidationError("table_options")
            for row in value["cells"]:
                self.table_row(row, depth + 1)
            return
        if kind == "details":
            if (
                set(value) != {"type", "summary", "blocks", "is_open"}
                or type(value["is_open"]) is not bool
                or not isinstance(value["blocks"], list)
            ):
                raise RichMessageValidationError("details_fields")
            self.text(value["summary"], depth)
            for block in value["blocks"]:
                self.block(block, depth + 1)
            return
        if kind == "collage":
            if (
                set(value) != {"type", "blocks"}
                or not isinstance(value["blocks"], list)
                or not 2 <= len(value["blocks"]) <= 3
                or any(
                    not isinstance(block, dict) or block.get("type") != "photo"
                    for block in value["blocks"]
                )
            ):
                raise RichMessageValidationError("collage_fields")
            for block in value["blocks"]:
                self.block(block, depth + 1)
            return
        if kind == "photo":
            if set(value) != {"type", "photo"} or not isinstance(value["photo"], dict):
                raise RichMessageValidationError("photo_fields")
            photo = value["photo"]
            if set(photo) != {"type", "media"} or photo.get("type") != "photo":
                raise RichMessageValidationError("photo_media")
            self.media_reference(photo.get("media"))
            self.media += 1
            return
        raise RichMessageValidationError("block_kind")

    def list_item(self, value: object, depth: int) -> None:
        self._record_depth(depth)
        if not isinstance(value, dict) or not isinstance(value.get("blocks"), list):
            raise RichMessageValidationError("list_item")
        keys = set(value)
        if keys == {"blocks"}:
            pass
        elif (
            keys == {"blocks", "value", "type"}
            and type(value["value"]) is int
            and value["type"] == "1"
        ):
            pass
        else:
            raise RichMessageValidationError("list_item_fields")
        self.blocks += 1
        for block in value["blocks"]:
            self.block(block, depth + 1)

    def table_row(self, value: object, depth: int) -> None:
        self._record_depth(depth)
        if not isinstance(value, list):
            raise RichMessageValidationError("table_row")
        columns = 0
        self.blocks += 1
        for cell in value:
            if not isinstance(cell, dict):
                raise RichMessageValidationError("table_cell")
            allowed = {"align", "valign", "text", "colspan"}
            if not set(cell) <= allowed or "align" not in cell or "valign" not in cell:
                raise RichMessageValidationError("table_cell_fields")
            if cell["align"] not in {"left", "center", "right"}:
                raise RichMessageValidationError("table_cell_align")
            if cell["valign"] not in {"top", "middle", "bottom"}:
                raise RichMessageValidationError("table_cell_valign")
            colspan = cell.get("colspan", 1)
            if type(colspan) is not int or colspan < 1:
                raise RichMessageValidationError("table_cell_colspan")
            columns += colspan
            if "text" in cell:
                self.text(cell["text"], depth)
        if columns > RICH_TABLE_COLUMN_LIMIT:
            raise RichMessageValidationError("table_columns")


def validate_rich_payload(payload: object) -> dict:
    """Проверить точный сериализованный payload и вернуть исходный dict."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"blocks", "skip_entity_detection"}
        or payload.get("skip_entity_detection") is not True
        or not isinstance(payload.get("blocks"), list)
        or not payload["blocks"]
    ):
        raise RichMessageValidationError("message_fields")
    counter = _Counter()
    for block in payload["blocks"]:
        counter.block(block, 1)
    if counter.characters > RICH_TEXT_LIMIT:
        raise RichMessageValidationError("characters")
    if counter.blocks > RICH_BLOCK_LIMIT:
        raise RichMessageValidationError("blocks")
    if counter.depth > RICH_NESTING_LIMIT:
        raise RichMessageValidationError("nesting")
    if counter.media > RICH_MEDIA_LIMIT:
        raise RichMessageValidationError("media")
    return payload
