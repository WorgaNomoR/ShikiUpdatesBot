# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Авторитетные тесты типизированной модели и ordinary HTML chunk planner."""

from xml.etree import ElementTree

import pytest

from report_model import (
    Bold,
    Italic,
    Link,
    Report,
    Row,
    Rows,
    line,
    render_report,
    section,
    unit,
)


def _visible_text(markup: str) -> str:
    root = ElementTree.fromstring(f"<root>{markup}</root>")
    return "".join(root.itertext())


def _assert_independent_html(report: Report, limit: int) -> list[str]:
    chunks = render_report(report, limit=limit)
    assert chunks
    for chunk in chunks:
        ElementTree.fromstring(f"<root>{chunk.html}</root>")
        assert chunk.visible_length <= limit
    return [chunk.html for chunk in chunks]


def test_renderer_escapes_untrusted_text_labels_and_url_only_at_boundary():
    report = Report((unit(section(line(
        "plain <&> ",
        Bold("bold <&>"),
        " ",
        Italic("italic <&>"),
        " ",
        Link("link <&>", 'https://example.test/?a=1&b="quoted"'),
    ))),))

    markup = _assert_independent_html(report, 4096)[0]

    assert "plain &lt;&amp;&gt;" in markup
    assert "<b>bold &lt;&amp;&gt;</b>" in markup
    assert "<i>italic &lt;&amp;&gt;</i>" in markup
    assert 'href="https://example.test/?a=1&amp;b=&quot;quoted&quot;"' in markup
    assert _visible_text(markup) == "plain <&> bold <&> italic <&> link <&>"


def test_chunk_boundary_keeps_rows_and_link_formatting_atomic():
    report = Report((unit(
        section(
            line(Bold("HEADER")),
            Rows((
                Row("row-one", "11"),
                Row("row-two", "22"),
                Row("row-three", "33"),
            )),
        ),
        section(line("item: ", Link("linked-title-with-continuation", "https://example.test/a&b"))),
    ),))

    chunks = _assert_independent_html(report, 32)
    combined = "".join(_visible_text(chunk) for chunk in chunks)

    for token in ("row-one", "row-two", "row-three", "linked-title-with-continuation"):
        assert combined.count(token) == 1
    for token in ("row-one", "row-two", "row-three"):
        assert sum(token in _visible_text(chunk) for chunk in chunks) == 1
    assert sum(
        "linked-title-with-continuation" in _visible_text(chunk)
        for chunk in chunks
    ) == 1
    assert all(chunk.count("<code>") == chunk.count("</code>") for chunk in chunks)
    assert all(chunk.count("<a ") == chunk.count("</a>") for chunk in chunks)


def test_oversized_formatting_node_is_not_split_inside_link():
    report = Report((unit(section(line(
        Link("linked-title-too-long", "https://example.test/item"),
    ))),))

    with pytest.raises(ValueError, match="Formatting node"):
        render_report(report, limit=10)


def test_emoji_and_html_metacharacters_respect_post_entity_limit():
    value = "x" * 4093 + "😀&<"
    report = Report((unit(section(line(value))),))

    chunks = render_report(report)

    assert len(chunks) == 2
    assert [chunk.visible_length for chunk in chunks] == [4096, 1]
    combined_markup = "".join(chunk.html for chunk in chunks)
    assert "&amp;" in combined_markup
    assert "&lt;" in combined_markup
    assert "".join(_visible_text(chunk.html) for chunk in chunks) == value


def test_single_plain_text_field_longer_than_limit_is_lossless_continuation():
    value = "очень-длинное-поле<&>" * 300
    report = Report((unit(section(line(value))),))

    chunks = render_report(report, limit=127)

    assert len(chunks) > 1
    assert all(chunk.visible_length <= 127 for chunk in chunks)
    assert "".join(_visible_text(chunk.html) for chunk in chunks) == value


def test_ordinary_logical_items_are_kept_whole_and_present_exactly_once():
    tokens = [f"logical-item-{index:03d}" for index in range(40)]
    report = Report((unit(section(*(line(token) for token in tokens))),))

    chunks = _assert_independent_html(report, 80)
    visible_chunks = [_visible_text(chunk) for chunk in chunks]

    for token in tokens:
        assert sum(token in chunk for chunk in visible_chunks) == 1
        assert sum(chunk.count(token) for chunk in visible_chunks) == 1
