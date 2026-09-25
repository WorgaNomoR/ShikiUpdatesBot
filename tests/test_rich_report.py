# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Exact aiogram types и безопасная семантика Rich Report renderer."""

import json
from html import unescape

import pytest
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
    RichTextCode,
    RichTextItalic,
    RichTextSubscript,
    RichTextUrl,
)

from report_asset_ids import REPORT_POSTER_PLACEHOLDER_MEDIA
from report_model import (
    Bold,
    Code,
    Gallery,
    Italic,
    Link,
    Poster,
    Report,
    Row,
    Rows,
    Table,
    TableCell,
    TableGroup,
    TableRow,
    Text,
    Title,
    heading,
    line,
    rendered_html,
    section,
    unit,
)
from rich_message_schema import (
    RICH_TEXT_LIMIT,
    validate_rich_payload,
)
from rich_report import (
    RichReportRenderError,
    render_rich_report,
)


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


def test_inline_code_maps_to_native_rich_text_code():
    report = Report((unit(section(line("Команда: ", Code("/block ID")))),))

    paragraph = render_rich_report(report)[0].message.blocks[0]

    assert isinstance(paragraph, InputRichBlockParagraph)
    assert isinstance(paragraph.text[1], RichTextCode)
    assert paragraph.text[1].text == "/block ID"


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


def test_grouped_table_maps_links_colspan_and_hostile_text_without_parsing():
    report = Report((unit(section(Table(
        columns=4,
        header=TableRow((
            TableCell((Bold("Название"),)),
            TableCell((Bold("Оценка"),)),
            TableCell((Bold("Год"),)),
            TableCell((Bold("Тип"),)),
        )),
        groups=(TableGroup(
            rows=(
                TableRow((
                    TableCell((Title(
                        "Linked <title>",
                        "https://example.test/title?a=1&b=2",
                    ),)),
                    TableCell((Text("9⭐"),), align="center"),
                    TableCell((Text("2024"),), align="center"),
                    TableCell((Text("TV-сериал"),), align="center"),
                )),
                TableRow((TableCell((
                    Bold("Комментарий:\n"),
                    Text("<b>plain</b>\n• not a list"),
                ), colspan=4),)),
            ),
            fallback=(line("fallback"),),
        ),),
    ))),))

    table = render_rich_report(report)[0].message.blocks[0]

    assert isinstance(table, InputRichBlockTable)
    assert len(table.cells) == 3
    assert isinstance(table.cells[1][0].text, RichTextUrl)
    assert table.cells[1][0].text.text == "Linked <title>"
    assert table.cells[1][0].text.url == "https://example.test/title?a=1&b=2"
    assert table.cells[2][0].colspan == 4
    assert isinstance(table.cells[2][0].text[0], RichTextBold)
    assert table.cells[2][0].text[1] == "<b>plain</b>\n• not a list"


def test_separate_table_groups_render_as_cards_with_external_italic_comment():
    hostile = "<b>plain</b>\n* not Markdown"
    report = Report((unit(section(Table(
        columns=3,
        groups=(
            TableGroup(
                rows=(
                    TableRow((
                        TableCell((Text("1"),)),
                        TableCell((Title(
                            "First",
                            "https://example.test/first",
                        ),)),
                        TableCell((Text("9⭐"),)),
                    )),
                    TableRow((TableCell((
                        Bold("Год: "),
                        Text("2024"),
                    ), colspan=3),)),
                ),
                fallback=(line("1. First"), line("Год: 2024")),
                after=(line("💬 ", Italic(hostile)),),
            ),
            TableGroup(
                rows=(TableRow((
                    TableCell((Text("2"),)),
                    TableCell((Title("Second", None),)),
                    TableCell((Text("—"),)),
                )),),
                fallback=(line("2. Second"),),
            ),
        ),
        separate_groups=True,
    ))),))

    ordinary = unescape(rendered_html(report)[0])
    blocks = render_rich_report(report)[0].message.blocks

    assert [type(block) for block in blocks] == [
        InputRichBlockTable,
        InputRichBlockParagraph,
        InputRichBlockTable,
    ]
    assert len(blocks[0].cells[0]) == 3
    assert blocks[0].cells[1][0].colspan == 3
    assert isinstance(blocks[1].text, list)
    assert blocks[1].text[0] == "💬 "
    assert isinstance(blocks[1].text[1], RichTextItalic)
    assert blocks[1].text[1].text == hostile
    assert ordinary.index("First") < ordinary.index(hostile) < ordinary.index("Second")


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
            heading("open", collapsible=True, is_open=True),
            line("visible"),
        ),
        section(
            heading("closed", collapsible=True, is_open=False),
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


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_explicit_gallery_uses_no_placeholder_and_keeps_bounded_layout(count):
    report = Report((unit(
        section(heading("Status", level=1)),
        section(Gallery(tuple(
            Poster(f"https://cdn.example.test/{index}.jpg")
            for index in range(count)
        ))),
    ),))

    rendered = render_rich_report(report)[0]
    media_blocks = rendered.message.blocks[1:]

    if count == 0:
        assert media_blocks == []
    elif count == 1:
        assert len(media_blocks) == 1
        assert isinstance(media_blocks[0], InputRichBlockPhoto)
        assert media_blocks[0].photo.media == "https://cdn.example.test/0.jpg"
    else:
        assert len(media_blocks) == 1
        assert isinstance(media_blocks[0], InputRichBlockCollage)
        assert [photo.photo.media for photo in media_blocks[0].blocks] == [
            f"https://cdn.example.test/{index}.jpg"
            for index in range(count)
        ]
    assert REPORT_POSTER_PLACEHOLDER_MEDIA not in json.dumps(
        rendered.payload,
        ensure_ascii=False,
    )


def test_explicit_gallery_omits_invalid_sources_instead_of_filling_slots():
    report = Report((unit(
        section(heading("Status", level=1)),
        section(Gallery((
            Poster("https://cdn.example.test/first.jpg"),
            Poster(None),
            Poster("http://cdn.example.test/insecure.jpg"),
        ))),
    ),))

    rendered = render_rich_report(report)[0]

    assert len(rendered.message.blocks) == 2
    assert isinstance(rendered.message.blocks[1], InputRichBlockPhoto)
    assert rendered.message.blocks[1].photo.media == "https://cdn.example.test/first.jpg"


def test_explicit_gallery_rejects_unbounded_media_hint():
    report = Report((unit(
        section(heading("Status", level=1)),
        section(Gallery(tuple(
            Poster(f"https://cdn.example.test/{index}.jpg")
            for index in range(4)
        ))),
    ),))

    with pytest.raises(RichReportRenderError, match="^gallery_size$"):
        render_rich_report(report)


def _catalog_report(count: int, *, padding: int = 0) -> Report:
    groups = tuple(
        TableGroup(
            rows=(TableRow((TableCell((Text(
                f"card-{index:03d}-{'x' * padding}"
            ),)),)),),
            fallback=(line(f"card-{index:03d}-{'x' * padding}"),),
        )
        for index in range(count)
    )
    return Report((unit(
        section(heading("📺 ", Bold("АНИМЕ"), level=1)),
        section(
            heading(
                "✅ ",
                Bold(f"Просмотрено · {count}"),
                collapsible=True,
            ),
            Table(columns=1, groups=groups, separate_groups=True),
        ),
    ),))


def _catalog_tokens(payloads: tuple[dict, ...]) -> list[str]:
    result = []

    def visit(value: object) -> None:
        if isinstance(value, str):
            if value.startswith("card-"):
                result.append(value)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if isinstance(value, dict):
            for item in value.values():
                visit(item)

    for payload in payloads:
        visit(payload)
    return result


def _rich_parts_text(parts: list) -> str:
    return "".join(
        part if isinstance(part, str) else part.text
        for part in parts
    )


def _multi_status_catalog_report(
    first_count: int,
    second_count: int,
    *,
    second_padding: int = 0,
) -> Report:
    def groups(prefix: str, count: int, padding: int = 0):
        return tuple(
            TableGroup(
                rows=(TableRow((TableCell((Text(
                    f"{prefix}-{index:03d}-{'x' * padding}"
                ),)),)),),
                fallback=(line(f"{prefix}-{index:03d}-{'x' * padding}"),),
            )
            for index in range(count)
        )

    return Report((unit(
        section(heading("📺 ", Bold("АНИМЕ"), level=1)),
        section(
            heading(
                "✅ ",
                Bold(f"Первый · {first_count}"),
                collapsible=True,
            ),
            Table(
                columns=1,
                groups=groups("first", first_count),
                separate_groups=True,
            ),
        ),
        section(
            heading(
                "🗑 ",
                Bold(f"Второй · {second_count}"),
                collapsible=True,
            ),
            Table(
                columns=1,
                groups=groups("second", second_count, second_padding),
                separate_groups=True,
            ),
        ),
    ),))


def test_grouped_cards_paginate_immediately_after_exact_block_boundary():
    before = render_rich_report(_catalog_report(248))
    after = render_rich_report(_catalog_report(249))

    assert len(before) == 1
    assert len(after) == 2
    assert all(validate_rich_payload(fragment.payload) for fragment in after)
    assert _catalog_tokens(tuple(fragment.payload for fragment in after)) == [
        f"card-{index:03d}-"
        for index in range(249)
    ]
    assert all(fragment.fallback_unit is not None for fragment in after)


def test_paginated_catalog_fills_tail_with_next_status_and_marks_continuation():
    rendered = render_rich_report(_multi_status_catalog_report(249, 1))

    assert len(rendered) == 2
    assert [
        _rich_parts_text(fragment.message.blocks[1].text)
        for fragment in rendered
    ] == [
        "📺 АНИМЕ",
        "📺 АНИМЕ · продолжение",
    ]
    details = [
        [
            _rich_parts_text(block.summary)
            for block in fragment.message.blocks
            if isinstance(block, InputRichBlockDetails)
        ]
        for fragment in rendered
    ]
    assert details == [
        ["✅ Первый · 249"],
        [
            "✅ Первый · 249 · продолжение",
            "🗑 Второй · 1",
        ],
    ]
    payloads = tuple(fragment.payload for fragment in rendered)
    payload_text = json.dumps(payloads, ensure_ascii=False)
    tokens = [
        *(f"first-{index:03d}-" for index in range(249)),
        "second-000-",
    ]
    assert all(payload_text.count(token) == 1 for token in tokens)
    assert [payload_text.index(token) for token in tokens] == sorted(
        payload_text.index(token) for token in tokens
    )
    assert all(fragment.fallback_unit is not None for fragment in rendered)
    fallback = "\n".join(
        html
        for fragment in rendered
        for html in rendered_html(Report((fragment.fallback_unit,)))
    )
    assert all(fallback.count(token) == 1 for token in tokens)
    assert [fallback.index(token) for token in tokens] == sorted(
        fallback.index(token) for token in tokens
    )


def test_paginated_catalog_does_not_split_neighbor_into_tail():
    rendered = render_rich_report(_multi_status_catalog_report(247, 2))

    assert len(rendered) == 2
    assert all(validate_rich_payload(fragment.payload) for fragment in rendered)
    details = [
        [
            _rich_parts_text(block.summary)
            for block in fragment.message.blocks
            if isinstance(block, InputRichBlockDetails)
        ]
        for fragment in rendered
    ]
    assert details == [
        ["✅ Первый · 247"],
        ["🗑 Второй · 2"],
    ]
    payload_text = json.dumps(
        tuple(fragment.payload for fragment in rendered),
        ensure_ascii=False,
    )
    assert payload_text.count("second-000-") == 1
    assert payload_text.count("second-001-") == 1


def test_paginated_catalog_keeps_next_status_separate_when_tail_has_no_room():
    rendered = render_rich_report(_multi_status_catalog_report(
        249,
        1,
        second_padding=32_680,
    ))

    assert len(rendered) == 3
    assert all(validate_rich_payload(fragment.payload) for fragment in rendered)
    details = [
        [
            _rich_parts_text(block.summary)
            for block in fragment.message.blocks
            if isinstance(block, InputRichBlockDetails)
        ]
        for fragment in rendered
    ]
    assert details == [
        ["✅ Первый · 249"],
        ["✅ Первый · 249 · продолжение"],
        ["🗑 Второй · 1"],
    ]


def test_oversized_comment_paginates_immediately_after_character_boundary():
    fixed_text = len("H") + len("S") + len("card") + len("↑ К началу")

    def report(comment_length: int) -> Report:
        group = TableGroup(
            rows=(TableRow((TableCell((Text("card"),)),)),),
            fallback=(line("card"),),
            after=(line(Italic("x" * comment_length)),),
        )
        return Report((unit(
            section(heading("H", level=1)),
            section(
                heading("S", collapsible=True),
                Table(columns=1, groups=(group,), separate_groups=True),
            ),
        ),))

    before = render_rich_report(report(RICH_TEXT_LIMIT - fixed_text))
    after = render_rich_report(report(RICH_TEXT_LIMIT - fixed_text + 1))

    assert len(before) == 1
    assert len(after) == 2
    assert all(validate_rich_payload(fragment.payload) for fragment in after)
    assert "".join(
        value["text"]["text"]
        for fragment in after
        for block in fragment.payload["blocks"]
        if block["type"] == "details"
        for value in block["blocks"]
        if value["type"] == "paragraph"
    ) == "x" * (RICH_TEXT_LIMIT - fixed_text + 1)


def test_grouped_cards_pack_against_simultaneous_block_and_character_pressure():
    rendered = render_rich_report(_catalog_report(260, padding=140))
    payloads = tuple(fragment.payload for fragment in rendered)

    assert len(rendered) > 1
    assert all(validate_rich_payload(payload) for payload in payloads)
    assert _catalog_tokens(payloads) == [
        f"card-{index:03d}-{'x' * 140}"
        for index in range(260)
    ]
    for fragment in rendered:
        assert fragment.message.blocks[0].name == "unit-0-top"
        assert fragment.message.blocks[-1].text.text.text == "↑ К началу"
        assert isinstance(fragment.message.blocks[1], InputRichBlockSectionHeading)
        assert any(
            isinstance(block, InputRichBlockDetails)
            for block in fragment.message.blocks
        )
