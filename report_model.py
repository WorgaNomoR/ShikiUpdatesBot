# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Типизированная модель отчётов и обычный Telegram HTML renderer."""

from dataclasses import dataclass
from html import escape

TELEGRAM_TEXT_LIMIT = 4096


def telegram_text_length(value: str) -> int:
    """Консервативная длина видимого текста в Telegram UTF-16 code units."""
    return len(value.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class Text:
    """Обычный недоверенный текст без форматирования."""

    value: str


@dataclass(frozen=True)
class Bold:
    """Недоверенный текст с жирным форматированием."""

    value: str


@dataclass(frozen=True)
class Italic:
    """Недоверенный текст с курсивным форматированием."""

    value: str


@dataclass(frozen=True)
class Link:
    """Недоверенные подпись и URL одной логической ссылки."""

    text: str
    url: str


Inline = Text | Bold | Italic | Link


@dataclass(frozen=True)
class Line:
    """Одна логическая строка из типизированных inline-элементов."""

    parts: tuple[Inline, ...]


@dataclass(frozen=True)
class Row:
    """Одна логическая строка выровненного счётчика."""

    label: str
    value: str
    suffix: str = ""


@dataclass(frozen=True)
class Rows:
    """Группа строк, отображаемая обычным renderer как multiline code."""

    rows: tuple[Row, ...]


ReportItem = Line | Rows


@dataclass(frozen=True)
class _CodeLines:
    """Уже выровненные строки одного continuation-фрагмента Rows."""

    lines: tuple[str, ...]


@dataclass(frozen=True)
class Section:
    """Логический блок; его элементы разделяются одним переводом строки."""

    items: tuple[ReportItem, ...]


@dataclass(frozen=True)
class Unit:
    """Самостоятельная тема отчёта, содержащая один или несколько блоков."""

    sections: tuple[Section, ...]


@dataclass(frozen=True)
class Report:
    """Отчёт в порядке его самостоятельных delivery units."""

    units: tuple[Unit, ...]


@dataclass(frozen=True)
class RenderedChunk:
    """Самостоятельное валидное HTML-сообщение и его логическая позиция."""

    html: str
    visible_length: int
    unit_index: int


def line(*parts: Inline | str) -> Line:
    """Удобно собрать строку, превращая литералы в обычный Text."""
    return Line(tuple(Text(part) if isinstance(part, str) else part for part in parts))


def section(*items: ReportItem) -> Section:
    """Удобно собрать логический блок отчёта."""
    return Section(tuple(items))


def unit(*sections: Section) -> Unit:
    """Удобно собрать самостоятельную тему отчёта."""
    return Unit(tuple(sections))


def plain_report(text: str) -> Report:
    """Собрать одночастный безопасный отчёт из обычного текста."""
    return Report((unit(section(line(text))),))


def _inline_text(part: Inline) -> str:
    if isinstance(part, Link):
        return part.text
    return part.value


def _render_inline(part: Inline) -> str:
    text = escape(_inline_text(part), quote=True)
    if isinstance(part, Bold):
        return f"<b>{text}</b>"
    if isinstance(part, Italic):
        return f"<i>{text}</i>"
    if isinstance(part, Link):
        return f'<a href="{escape(part.url, quote=True)}">{text}</a>'
    return text


def _clone_inline(part: Inline, value: str) -> Inline:
    if isinstance(part, Link):
        return Link(value, part.url)
    return type(part)(value)


def _take_utf16_prefix(value: str, limit: int) -> tuple[str, str]:
    """Отделить максимально длинный prefix в лимите, не разрезая code point."""
    if limit <= 0:
        return "", value
    used = 0
    for index, char in enumerate(value):
        width = telegram_text_length(char)
        if used + width > limit:
            return value[:index], value[index:]
        used += width
    return value, ""


def _split_line(value: Line, limit: int) -> list[Line]:
    """Продолжить только plain text, сохраняя formatting nodes атомарными."""
    fragments: list[Line] = []
    current: list[Inline] = []
    current_length = 0

    for part in value.parts:
        remaining_text = _inline_text(part)
        if not remaining_text:
            current.append(part)
            continue
        part_length = telegram_text_length(remaining_text)
        if not isinstance(part, Text):
            if part_length > limit:
                raise ValueError("Formatting node превышает лимит Telegram")
            if current and current_length + part_length > limit:
                fragments.append(Line(tuple(current)))
                current = []
                current_length = 0
            current.append(part)
            current_length += part_length
            continue
        while remaining_text:
            available = limit - current_length
            prefix, remaining = _take_utf16_prefix(remaining_text, available)
            if prefix:
                current.append(_clone_inline(part, prefix))
                current_length += telegram_text_length(prefix)
                remaining_text = remaining
            if remaining_text:
                if current:
                    fragments.append(Line(tuple(current)))
                    current = []
                    current_length = 0
                    continue
                raise ValueError("Невозможно продолжить logical field в заданном лимите")

    if current or not fragments:
        fragments.append(Line(tuple(current)))
    return fragments


def _row_texts(value: Rows) -> tuple[str, ...]:
    if not value.rows:
        return ()
    label_width = max(len(row.label) for row in value.rows)
    value_width = max(len(row.value) for row in value.rows)
    rendered = []
    for row in value.rows:
        dots = "·" * (label_width - len(row.label) + 1)
        rendered.append(
            f"{row.label} {dots} {row.value.rjust(value_width)}{row.suffix}"
        )
    return tuple(rendered)


def _render_item(value: ReportItem | _CodeLines) -> tuple[str, int]:
    if isinstance(value, Line):
        return (
            "".join(_render_inline(part) for part in value.parts),
            sum(telegram_text_length(_inline_text(part)) for part in value.parts),
        )
    rows = value.lines if isinstance(value, _CodeLines) else _row_texts(value)
    plain = "\n".join(rows)
    return f"<code>{escape(plain, quote=True)}</code>", telegram_text_length(plain)


def _split_rows(value: Rows, limit: int) -> list[_CodeLines]:
    """Делить multiline formatting только между рядами либо их continuations."""
    logical_rows = _row_texts(value)
    fragments: list[_CodeLines] = []
    current: list[str] = []
    current_length = 0

    for row_text in logical_rows:
        row_length = telegram_text_length(row_text)
        if row_length <= limit:
            separator = 1 if current else 0
            if current_length + separator + row_length > limit:
                fragments.append(_CodeLines(tuple(current)))
                current = []
                current_length = 0
                separator = 0
            current.append(row_text)
            current_length += separator + row_length
            continue

        if current:
            fragments.append(_CodeLines(tuple(current)))
            current = []
            current_length = 0
        remaining = row_text
        while remaining:
            prefix, remaining = _take_utf16_prefix(remaining, limit)
            if not prefix:
                raise ValueError("Невозможно продолжить logical row в заданном лимите")
            fragments.append(_CodeLines((prefix,)))

    if current:
        fragments.append(_CodeLines(tuple(current)))
    return fragments


def _split_item(value: ReportItem, limit: int) -> list[ReportItem | _CodeLines]:
    html, visible = _render_item(value)
    if visible <= limit:
        return [value]
    if isinstance(value, Line):
        return _split_line(value, limit)
    return _split_rows(value, limit)


def _render_section(value: Section) -> tuple[str, int]:
    rendered = [_render_item(item) for item in value.items]
    return (
        "\n".join(html for html, _ in rendered),
        sum(length for _, length in rendered) + max(0, len(rendered) - 1),
    )


def _render_unit(value: Unit, limit: int, unit_index: int) -> list[RenderedChunk]:
    chunks: list[RenderedChunk] = []
    current_html = ""
    current_length = 0

    def flush() -> None:
        nonlocal current_html, current_length
        if current_html:
            chunks.append(RenderedChunk(current_html, current_length, unit_index))
            current_html = ""
            current_length = 0

    for logical_section in value.sections:
        if not logical_section.items:
            continue
        section_html, section_length = _render_section(logical_section)
        section_separator = 2 if current_html else 0
        if section_length <= limit - current_length - section_separator:
            current_html += ("\n\n" if current_html else "") + section_html
            current_length += section_separator + section_length
            continue
        if section_length <= limit:
            flush()
            current_html = section_html
            current_length = section_length
            continue

        flush()
        for item in logical_section.items:
            for item_fragment in _split_item(item, limit):
                item_html, item_length = _render_item(item_fragment)
                item_separator = 1 if current_html else 0
                if item_length > limit - current_length - item_separator:
                    flush()
                    item_separator = 0
                current_html += ("\n" if item_separator else "") + item_html
                current_length += item_separator + item_length
        flush()

    flush()
    return chunks


def render_report(
    report: Report,
    *,
    limit: int = TELEGRAM_TEXT_LIMIT,
) -> tuple[RenderedChunk, ...]:
    """Отобразить и логически разбить отчёт на самостоятельные HTML chunks."""
    if limit <= 0:
        raise ValueError("Лимит Telegram-сообщения должен быть положительным")
    chunks = []
    for unit_index, logical_unit in enumerate(report.units):
        chunks.extend(_render_unit(logical_unit, limit, unit_index))
    return tuple(chunks)


def rendered_html(report: Report, *, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Вернуть только HTML-тексты для тестов и текущего durable quarter state."""
    return [chunk.html for chunk in render_report(report, limit=limit)]
