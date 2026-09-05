# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Авторитетные тесты типизированной модели и ordinary HTML chunk planner."""

import re
from html import unescape
from html.parser import HTMLParser

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

_UNESCAPED_AMPERSAND = re.compile(
    r"&(?!amp;|gt;|lt;|quot;|#(?:\d+|[xX][0-9A-Fa-f]+);)",
)


class _StrictTelegramHtmlParser(HTMLParser):
    """Строго проверить ограниченное HTML-подмножество renderer."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text()
        if tag == "a":
            if len(attrs) != 1 or attrs[0][0] != "href" or attrs[0][1] is None:
                pytest.fail(f"Некорректные атрибуты ссылки: {raw}")
            match = re.fullmatch(r'<a href="(.*)">', raw, flags=re.DOTALL)
            if match is None:
                pytest.fail(f"Некорректная разметка ссылки: {raw}")
            escaped_href = match.group(1)
            if (
                any(symbol in escaped_href for symbol in '<>"')
                or _UNESCAPED_AMPERSAND.search(escaped_href)
            ):
                pytest.fail(f"Неэкранированный URL ссылки: {raw}")
        elif tag in {"b", "code", "i"}:
            if attrs or raw != f"<{tag}>":
                pytest.fail(f"Некорректный formatting tag: {raw}")
        else:
            pytest.fail(f"Неподдерживаемый Telegram HTML tag: {tag}")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            pytest.fail(f"Несогласованный закрывающий tag: {tag}")

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        pytest.fail(f"Самозакрывающийся tag не поддерживается: {tag}")

    def handle_data(self, data: str) -> None:
        if "<" in data or "&" in data:
            pytest.fail(f"Неэкранированный HTML text: {data!r}")
        self.text.append(data)

    def handle_entityref(self, name: str) -> None:
        if name not in {"amp", "gt", "lt", "quot"}:
            pytest.fail(f"Неподдерживаемая HTML entity: &{name};")
        self.text.append(unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        try:
            base = 16 if name.startswith(("x", "X")) else 10
            value = int(name[1:] if base == 16 else name, base)
            if not 0 <= value <= 0x10FFFF or 0xD800 <= value <= 0xDFFF:
                raise ValueError
        except ValueError:
            pytest.fail(f"Некорректная HTML character reference: &#{name};")
        self.text.append(chr(value))

    def handle_comment(self, data: str) -> None:
        pytest.fail("HTML-комментарии не поддерживаются")

    def handle_decl(self, decl: str) -> None:
        pytest.fail("HTML declarations не поддерживаются")

    def handle_pi(self, data: str) -> None:
        pytest.fail("HTML processing instructions не поддерживаются")


def _parse_html(markup: str) -> _StrictTelegramHtmlParser:
    parser = _StrictTelegramHtmlParser()
    parser.feed(markup)
    parser.close()
    assert parser.stack == []
    return parser


def _visible_text(markup: str) -> str:
    return "".join(_parse_html(markup).text)


def _assert_independent_html(report: Report, limit: int) -> list[str]:
    chunks = render_report(report, limit=limit)
    assert chunks
    for chunk in chunks:
        _parse_html(chunk.html)
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
