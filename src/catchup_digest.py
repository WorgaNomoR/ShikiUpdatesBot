# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистое lossless HTML-представление завершённой порции истории."""

from datetime import datetime
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from report_model import (
    TELEGRAM_TEXT_LIMIT,
    telegram_text_length,
)

_TAGS = {"b", "i", "u", "s", "code", "a"}


class _HTML(HTMLParser):
    """Сохраняем стили каждого code point; entities декодируются ровно один раз."""

    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.runs = []
        self.feed(value)
        self.close()
        if self.stack:
            raise ValueError("digest_html_unclosed")

    def handle_starttag(self, tag, attrs):
        if tag not in _TAGS or (tag != "a" and attrs):
            raise ValueError("digest_html_tag")
        if tag == "a":
            if len(attrs) != 1 or attrs[0][0] != "href":
                raise ValueError("digest_html_link")
            url = attrs[0][1]
            parsed = urlsplit(url or "")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("digest_html_url")
            opening = f'<a href="{escape(url, quote=True)}">'
        else:
            opening = f"<{tag}>"
        self.stack.append((tag, opening))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError("digest_html_nesting")
        self.stack.pop()

    def handle_data(self, data):
        self.runs.append((data, tuple(self.stack)))

    def handle_comment(self, data):
        raise ValueError("digest_html_comment")

    def handle_decl(self, decl):
        raise ValueError("digest_html_declaration")

    def handle_pi(self, data):
        raise ValueError("digest_html_instruction")


def html_length(value: str) -> int:
    """Общая runtime/import проверка безопасного HTML и видимого UTF-16 размера."""
    value.encode("utf-8")
    return sum(telegram_text_length(text) for text, _ in _HTML(value).runs)


def split_html(value: str, *, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Продолжить даже огромную ссылку/emoji без потери текста и открытых тегов."""
    chunks = []
    pieces = []
    used = 0
    for text, style in _HTML(value).runs:
        current = ""
        for char in text:
            width = telegram_text_length(char)
            if used + width > limit:
                if current:
                    pieces.append(_styled(current, style))
                    current = ""
                chunks.append("".join(pieces))
                pieces = []
                used = 0
            if width > limit:
                raise ValueError("digest_html_limit")
            current += char
            used += width
        if current:
            pieces.append(_styled(current, style))
    if pieces:
        chunks.append("".join(pieces))
    if not chunks:
        raise ValueError("digest_html_empty")
    return chunks


def _styled(text, style):
    return (
        "".join(opening for _, opening in style) + escape(text, quote=True)
        + "".join(f"</{tag}>" for tag, _ in reversed(style))
    )


def _source_period(events):
    times = []
    for event in events:
        if event["time_quality"] != "aware" or not event["event_at"]:
            return ""
        source = datetime.fromisoformat(event["event_at"])
        if source > datetime.fromisoformat(event["observed_at"]):
            return ""
        times.append(source)
    first, last = min(times), max(times)
    start, end = first.strftime("%d.%m.%Y %H:%M"), last.strftime("%d.%m.%Y %H:%M")
    return f"\n{start}" + (f" — {end}" if start != end else "") + " UTC"


def render_digest(events: list[dict], *, ordinary, heading: str) -> list[dict]:
    """Одна сводка принятой порции, включая unknown в исходном порядке."""
    if not events:
        return []
    parts = []
    header = heading + _source_period(events) + "\n\n"
    header_size = html_length(header)
    if header_size >= TELEGRAM_TEXT_LIMIT:
        raise ValueError("digest_heading_limit")
    text, refs, size = header, [], header_size
    for event in events:
        # Один выбор штатного шаблона; recovery использует сохранённый payload.
        row = ordinary(event)
        ref = [event["seq"], event["history_id"]]
        for fragment in split_html(row, limit=TELEGRAM_TEXT_LIMIT - header_size):
            length = html_length(fragment)
            separator = "\n\n" if refs else ""
            if size + len(separator) + length > TELEGRAM_TEXT_LIMIT:
                parts.append({"kind": "digest", "events": refs, "text": text})
                text, refs, size = header, [], header_size
                separator = ""
            text += separator + fragment
            size += len(separator) + length
            if not refs or refs[-1] != ref:
                refs.append(ref)
    if refs:
        parts.append({"kind": "digest", "events": refs, "text": text})
    return parts
