# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Exact aiogram types и безопасная семантика Rich Report renderer."""

import json
from html import unescape

from aiogram.types import (
    InputRichBlockAnchor,
    InputRichBlockCollage,
    InputRichBlockDetails,
    InputRichBlockList,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichMessage,
    RichTextAnchorLink,
    RichTextBold,
    RichTextItalic,
    RichTextSubscript,
    RichTextUrl,
)

from report_asset_ids import REPORT_POSTER_PLACEHOLDER_MEDIA
from report_model import (
    Bold,
    Italic,
    Link,
    Poster,
    Report,
    Row,
    Rows,
    Title,
    heading,
    line,
    rendered_html,
    section,
    unit,
)
from rich_report import render_rich_report


def _structured_report() -> Report:
    return Report((unit(
        section(heading("📊 ", Bold("<unsafe & heading '><script>"), level=1)),
        section(
            heading(
                "📦 ",
                Bold("details </details>"),
                level=3,
                collapsible=True,
            ),
            Rows((
                Row("<cell>", "7", "  50%"),
                Row("wide & value", "9"),
            )),
            line("  1. ", Link("<linked title>", 'https://example.test/?q=<x>&v="y"')),
            line("  2. ", Italic("second & item")),
        ),
    ),))


def test_renderer_uses_exact_aiogram_types_and_keeps_values_as_text():
    rendered = render_rich_report(_structured_report())[0]
    message = rendered.message

    assert isinstance(message, InputRichMessage)
    assert message.skip_entity_detection is True
    assert [type(block) for block in message.blocks] == [
        InputRichBlockAnchor,
        InputRichBlockSectionHeading,
        InputRichBlockDetails,
        InputRichBlockParagraph,
    ]
    anchor, title, details, return_link = message.blocks
    assert anchor.name == "unit-0-top"
    assert isinstance(title.text[1], RichTextBold)
    assert title.text[1].text == "<unsafe & heading '><script>"
    assert isinstance(details.summary[1], RichTextBold)
    assert details.summary[1].text == "details </details>"
    assert not any(isinstance(part, RichTextAnchorLink) for part in details.summary)
    assert details.is_open is True
    assert [type(block) for block in details.blocks] == [
        InputRichBlockTable,
        InputRichBlockList,
    ]
    table, ordered = details.blocks
    assert isinstance(return_link.text, RichTextAnchorLink)
    assert isinstance(return_link.text.text, RichTextSubscript)
    assert return_link.text.text.text == "↑ К началу"
    assert return_link.text.anchor_name == "unit-0-top"
    assert table.is_bordered is table.is_striped is table.is_compact is True
    assert [len(row) for row in table.cells] == [3, 3]
    assert table.cells[0][0].text == "<cell>"
    assert table.cells[0][1].text == "7"
    assert table.cells[0][2].text == "50%"
    assert table.cells[1][2].text == ""
    assert [item.value for item in ordered.items] == [1, 2]
    assert all(item.type == "1" for item in ordered.items)
    assert isinstance(ordered.items[0].blocks[0], InputRichBlockParagraph)
    assert isinstance(ordered.items[0].blocks[0].text, RichTextUrl)
    assert ordered.items[0].blocks[0].text.text == "<linked title>"
    assert ordered.items[0].blocks[0].text.url == 'https://example.test/?q=<x>&v="y"'
    assert isinstance(ordered.items[1].blocks[0].text, RichTextItalic)


def test_table_label_can_improve_rich_layout_without_changing_html_fallback():
    report = Report((unit(section(Rows((
        Row("★10", "2", table_label="10★"),
        Row("★9", "1", table_label="9★"),
    ))),),))

    ordinary = unescape(rendered_html(report)[0])
    table = render_rich_report(report)[0].message.blocks[0]

    assert "★10" in ordinary
    assert "★9" in ordinary
    assert [row[0].text for row in table.cells] == ["10★", "9★"]


def test_mixed_table_tolerates_missing_suffix_in_defensive_rich_boundary():
    report = Report((unit(section(Rows((
        Row("with", "1", "  50%"),
        Row("without", "2", None),
    ))),),))

    table = render_rich_report(report)[0].message.blocks[0]

    assert [row[2].text for row in table.cells] == ["50%", ""]


def test_renderer_is_deterministic_and_anchor_names_ignore_hostile_text():
    first = render_rich_report(_structured_report())[0].payload
    second = render_rich_report(_structured_report())[0].payload

    assert first == second
    serialized = json.dumps(first, ensure_ascii=False)
    assert '"name": "unit-0-top"' in serialized
    assert '"anchor_name": "unit-0-top"' in serialized
    assert "&gt;&lt;script" not in serialized


def test_plain_hostile_list_prefix_does_not_create_list_structure():
    report = Report((unit(section(line("• injected as one untrusted field"))),))

    message = render_rich_report(report)[0].message

    assert len(message.blocks) == 1
    assert isinstance(message.blocks[0], InputRichBlockParagraph)
    assert message.blocks[0].text == "• injected as one untrusted field"


def test_collapsible_policy_preserves_explicit_open_and_closed_states():
    report = Report((unit(
        section(
            heading("open", collapsible=True, open=True),
            line("visible"),
        ),
        section(
            heading("closed", collapsible=True, open=False),
            line("hidden until opened"),
        ),
    ),))

    message = render_rich_report(report)[0].message
    details = [
        block
        for block in message.blocks
        if isinstance(block, InputRichBlockDetails)
    ]

    assert [block.is_open for block in details] == [True, False]
    return_links = [
        block
        for block in message.blocks
        if (
            isinstance(block, InputRichBlockParagraph)
            and isinstance(block.text, RichTextAnchorLink)
        )
    ]
    assert len(return_links) == 1
    assert message.blocks[-1] is return_links[0]


def test_rich_and_ordinary_preserve_data_tokens_in_the_same_order():
    report = _structured_report()
    ordinary = unescape("\n".join(rendered_html(report)))
    rich = json.dumps(render_rich_report(report)[0].payload, ensure_ascii=False)
    tokens = [
        "<unsafe & heading '><script>",
        "details </details>",
        "<cell>",
        "7",
        "50%",
        "wide & value",
        "9",
        "<linked title>",
        "second & item",
    ]

    assert [ordinary.index(token) for token in tokens] == sorted(
        ordinary.index(token) for token in tokens
    )
    assert [rich.index(token) for token in tokens] == sorted(
        rich.index(token) for token in tokens
    )


def _poster_top(*titles: Title) -> Report:
    return Report((unit(section(
        heading("Топ", collapsible=True),
        *(
            line(f"  {index}. ", title, " — ⭐10")
            for index, title in enumerate(titles, 1)
        ),
    )),))


def test_top_collage_keeps_rank_order_and_replaces_invalid_poster_slots():
    report = _poster_top(
        Title(
            "<first>",
            "https://shikimori.io/animes/1",
            Poster("https://cdn.example.test/first.jpg"),
        ),
        Title(
            "second & missing",
            "https://shikimori.io/animes/2",
            Poster(None),
        ),
        Title(
            "third hostile",
            "https://shikimori.io/animes/3",
            Poster('https://user:password@cdn.example.test/third.jpg'),
        ),
    )

    rendered = render_rich_report(report)[0]
    details = rendered.message.blocks[1]

    assert isinstance(details, InputRichBlockDetails)
    assert [type(block) for block in details.blocks] == [
        InputRichBlockList,
        InputRichBlockCollage,
    ]
    collage = details.blocks[1]
    assert all(isinstance(block, InputRichBlockPhoto) for block in collage.blocks)
    assert [block.photo.media for block in collage.blocks] == [
        "https://cdn.example.test/first.jpg",
        REPORT_POSTER_PLACEHOLDER_MEDIA,
        REPORT_POSTER_PLACEHOLDER_MEDIA,
    ]
    assert "<first>" in unescape(rendered_html(report)[0])
    assert "asset://" not in rendered_html(report)[0]


def test_collage_is_hidden_for_one_title_or_when_every_poster_is_unavailable():
    reports = (
        _poster_top(Title("only", None, Poster("https://cdn.example.test/only.jpg"))),
        _poster_top(
            Title("first", None, Poster(None)),
            Title("second", None, Poster("javascript:alert(1)")),
        ),
    )

    for report in reports:
        details = render_rich_report(report)[0].message.blocks[1]
        assert isinstance(details, InputRichBlockDetails)
        assert [type(block) for block in details.blocks] == [
            InputRichBlockList,
        ]


def test_partial_poster_hints_never_create_a_misaligned_collage():
    report = _poster_top(
        Title("first", None, Poster("https://cdn.example.test/first.jpg")),
        Title("second", None),
    )

    details = render_rich_report(report)[0].message.blocks[1]

    assert isinstance(details, InputRichBlockDetails)
    assert [type(block) for block in details.blocks] == [
        InputRichBlockList,
    ]
