# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистые identifiers встроенных media assets отчётов."""

REPORT_POSTER_PLACEHOLDER_V1_SHA256 = (
    "33cbe54acad16a96a561e4948da822d7eedd9667386d5973391badbd9bbe58f7"
)
REPORT_POSTER_PLACEHOLDER_V1_MEDIA = (
    "asset://report-poster-placeholder-v1/sha256/"
    f"{REPORT_POSTER_PLACEHOLDER_V1_SHA256}"
)

REPORT_POSTER_PLACEHOLDER_SHA256 = (
    "568cf4caf5c5b2c9b87ef917655fb276e2e4f734b87a41a1c69c44ed6045d5a3"
)
REPORT_POSTER_PLACEHOLDER_MEDIA = (
    "asset://report-poster-placeholder-v2/sha256/"
    f"{REPORT_POSTER_PLACEHOLDER_SHA256}"
)

REPORT_POSTER_PLACEHOLDER_MEDIA_REFERENCES = frozenset((
    REPORT_POSTER_PLACEHOLDER_V1_MEDIA,
    REPORT_POSTER_PLACEHOLDER_MEDIA,
))
