# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контракты общих названий, ссылок и постеров отчётов."""

import pytest

from report_model import (
    Link,
    Poster,
    Text,
    Title,
)
from report_titles import _title_inline


def test_title_poster_normalizes_empty_saved_url_to_missing_slot():
    assert _title_inline(
        {"title": "No poster", "poster_url": ""},
        poster=True,
    ) == Title("No poster", None, Poster(None))


@pytest.mark.parametrize("record, expected", [
    ({}, Text("???")),
    ({"title": "A <B> & C", "url": None}, Text("A <B> & C")),
    ({"title": 7, "url": "/mangas/2"}, Link("7", "https://shikimori.io/mangas/2")),
    ({"title": "Full", "url": "https://shikimori.io/animes/1"}, Link("Full", "https://shikimori.io/animes/1")),
])
def test_title_inline_preserves_text_and_normalizes_links(record, expected):
    assert _title_inline(record) == expected


@pytest.mark.parametrize("poster_url, expected", [
    (None, None),
    ("   ", None),
    (7, None),
    (" https://cdn.example.test/poster.jpg ", "https://cdn.example.test/poster.jpg"),
])
def test_poster_branch_keeps_title_link_and_normalizes_saved_poster(poster_url, expected):
    record = {"title": "Title", "url": "/animes/1", "poster_url": poster_url}
    assert _title_inline(record, poster=True) == Title(
        "Title", "https://shikimori.io/animes/1", Poster(expected),
    )
