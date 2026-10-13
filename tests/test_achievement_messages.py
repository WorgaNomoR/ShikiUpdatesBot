# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""HTML-safe fallback и полные Telegram-sized поздравления."""

from html import unescape
from html.parser import HTMLParser

from achievement_messages import render_achievement_messages
from catchup_digest import html_length


def test_unknown_and_known_labels_are_safe():
    chunks = render_achievement_messages(
        {'<b>😀 & "': 2, "evangelion": 1}, {},
        baseline=False, base_url="https://shikimori.io", profile="A & B",
        labels={("evangelion", 1): ("Евангелион", "franchise")},
    )
    text = "".join(chunks)
    assert "&lt;b&gt;" in text and "&amp;" in text
    assert "/A%20%26%20B/achievements" in text
    assert "/achievements/franchise/evangelion" in text
    assert "Евангелион" in text
    assert all(html_length(chunk) <= 4096 for chunk in chunks)


def test_source_labels_cover_any_family_and_only_matching_level():
    chunks = render_achievement_messages(
        {"new_family": 3, "animelist": 5}, {"animelist": 4},
        baseline=False, base_url="https://shikimori.io", profile="WNR",
        labels={
            ("new_family", 3): ("Новое <достижение> & победа", "new_category"),
            ("animelist", 4): ("Название прежнего уровня", "common"),
        },
    )
    text = "".join(chunks)
    assert "Новое &lt;достижение&gt; &amp; победа" in text
    assert "/achievements/new_category/new_family" in text
    assert "Название прежнего уровня" not in text
    assert "Достижение «animelist»" in text
    assert "уровень 4 → <b>5</b>" in text


def test_large_award_batch_splits_without_losing_labels():
    awards = {f"{i:04d} 😀<&>" * 20: 2 for i in range(150)}
    chunks = render_achievement_messages(awards, {}, baseline=False, base_url="https://shikimori.io", profile="WNR")
    assert len(chunks) > 1
    assert all(0 < html_length(chunk) <= 4096 for chunk in chunks)
    parser = HTMLParser()
    pieces = []
    parser.handle_data = pieces.append
    for chunk in chunks:
        parser.feed(chunk)
    visible = "".join(pieces)
    assert all(unescape(label) in visible for label in awards)
