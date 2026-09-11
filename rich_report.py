# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Aiogram renderer Rich Messages поверх общей типизированной модели."""

import re
from dataclasses import dataclass

from aiogram.types import (
    InputMediaPhoto,
    InputRichBlockAnchor,
    InputRichBlockCollage,
    InputRichBlockDetails,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichMessage,
    RichBlockTableCell,
    RichTextAnchorLink,
    RichTextBold,
    RichTextItalic,
    RichTextSubscript,
    RichTextUrl,
)

from report_asset_ids import REPORT_POSTER_PLACEHOLDER_MEDIA
from report_model import (
    Bold,
    Heading,
    Inline,
    Italic,
    Line,
    Link,
    Poster,
    Report,
    ReportItem,
    Rows,
    Section,
    Table,
    TableCell,
    TableGroup,
    Text,
    Title,
    Unit,
)
from rich_message_schema import (
    RichMessageValidationError,
    is_safe_https_media_url,
    validate_rich_payload,
)

_ORDERED_MARKER = re.compile(r"  ([1-9][0-9]*)\. ")
_BULLET_MARKERS = {"• ", "  • "}


class RichReportRenderError(ValueError):
    """Типизированный отчёт нельзя безопасно представить одним rich payload."""


@dataclass(frozen=True)
class RenderedRichUnit:
    """Одна independently validated Rich Message delivery unit."""

    message: InputRichMessage
    payload: dict
    unit_index: int


def _rich_inline(part: Inline):
    if isinstance(part, Bold):
        return RichTextBold(text=part.value)
    if isinstance(part, Italic):
        return RichTextItalic(text=part.value)
    if isinstance(part, Link):
        return RichTextUrl(text=part.text, url=part.url)
    if isinstance(part, Title):
        if part.url:
            return RichTextUrl(text=part.text, url=part.url)
        return part.text
    if isinstance(part, Text):
        return part.value
    raise RichReportRenderError("unsupported_inline")


def _rich_parts(parts: tuple[Inline, ...]):
    rendered = [_rich_inline(part) for part in parts]
    if len(rendered) == 1:
        return rendered[0]
    return rendered


def _line_list_marker(value: Line) -> tuple[bool, int | None, tuple[Inline, ...]] | None:
    """Распознать только отдельный producer-owned marker, не пользовательский текст."""
    if not value.parts or not isinstance(value.parts[0], Text):
        return None
    marker = value.parts[0].value
    if marker in _BULLET_MARKERS:
        return False, None, value.parts[1:]
    match = _ORDERED_MARKER.fullmatch(marker)
    if match is not None:
        return True, int(match.group(1)), value.parts[1:]
    return None


def report_has_rich_features(report: Report) -> bool:
    """Есть ли в отчёте структура, которую rich transport реально улучшит."""
    return any(
        isinstance(item, (Heading, Rows, Table))
        or isinstance(item, Line) and _line_list_marker(item) is not None
        for logical_unit in report.units
        for logical_section in logical_unit.sections
        for item in logical_section.items
    )


def _paragraph(value: Line) -> InputRichBlockParagraph:
    return InputRichBlockParagraph(text=_rich_parts(value.parts))


def _counter_table(value: Rows) -> InputRichBlockTable | None:
    if not value.rows:
        return None
    has_suffix = any(row.suffix for row in value.rows)
    cells = []
    for row in value.rows:
        rendered_row = [
            RichBlockTableCell(
                align="left",
                valign="middle",
                text=(
                    row.table_label
                    if row.table_label is not None
                    else row.label
                ),
            ),
            RichBlockTableCell(
                align="right",
                valign="middle",
                text=row.value,
            ),
        ]
        if has_suffix:
            rendered_row.append(RichBlockTableCell(
                align="right",
                valign="middle",
                text=(row.suffix or "").strip(),
            ))
        cells.append(rendered_row)
    return InputRichBlockTable(
        cells=cells,
        is_bordered=True,
        is_striped=True,
        is_compact=True,
    )


def _rich_table_cell(value: TableCell) -> RichBlockTableCell:
    """Проверить геометрию и собрать одну ячейку общей Rich-таблицы."""
    if type(value.colspan) is not int or value.colspan < 1:
        raise RichReportRenderError("table_colspan")
    if value.align not in {"left", "center", "right"}:
        raise RichReportRenderError("table_align")
    if value.valign not in {"top", "middle", "bottom"}:
        raise RichReportRenderError("table_valign")
    return RichBlockTableCell(
        align=value.align,
        valign=value.valign,
        text=_rich_parts(value.parts) if value.parts else None,
        colspan=value.colspan if value.colspan > 1 else None,
    )


def _catalog_table(value: Table) -> InputRichBlockTable | None:
    """Отобразить произвольную таблицу, не используя ordinary-проекцию."""
    if type(value.columns) is not int or not 1 <= value.columns <= 20:
        raise RichReportRenderError("table_columns")
    logical_rows = []
    if value.header is not None:
        logical_rows.append(value.header)
    logical_rows.extend(
        row
        for group in value.groups
        for row in group.rows
    )
    if not logical_rows:
        return None

    cells = []
    for row in logical_rows:
        if not row.cells:
            raise RichReportRenderError("table_row_columns")
        rendered_cells = [_rich_table_cell(cell) for cell in row.cells]
        if sum(cell.colspan for cell in row.cells) != value.columns:
            raise RichReportRenderError("table_row_columns")
        cells.append(rendered_cells)
    return InputRichBlockTable(
        cells=cells,
        is_bordered=True,
        is_striped=True,
        is_compact=True,
    )


def _catalog_table_blocks(value: Table) -> list:
    """Разделить карточки, чтобы внешние строки не попадали внутрь таблицы."""
    if not value.separate_groups:
        if any(group.after for group in value.groups):
            raise RichReportRenderError("table_after_requires_separate_groups")
        table = _catalog_table(value)
        return [table] if table is not None else []

    blocks = []
    for group in value.groups:
        table = _catalog_table(Table(
            columns=value.columns,
            groups=(TableGroup(group.rows, group.fallback),),
            header=value.header,
        ))
        if table is not None:
            blocks.append(table)
        blocks.extend(_paragraph(after) for after in group.after)
    return blocks


def _safe_poster_url(value: object) -> str | None:
    """Допустить только цельный HTTPS URL без credentials и control chars."""
    return value if isinstance(value, str) and is_safe_https_media_url(value) else None


def _poster_from_parts(parts: tuple[Inline, ...]) -> Poster | None:
    """Извлечь единственный явно отмеченный poster slot одной строки."""
    posters = [
        part.poster
        for part in parts
        if isinstance(part, Title) and part.poster is not None
    ]
    return posters[0] if len(posters) == 1 else None


def _poster_collage(posters: list[Poster]) -> InputRichBlockCollage | None:
    """Собрать top-2/3 collage; полностью пустой набор не показывать."""
    if not 2 <= len(posters) <= 3:
        return None
    sources = []
    actual_posters = 0
    for poster in posters:
        source = _safe_poster_url(poster.url)
        if source is None:
            source = REPORT_POSTER_PLACEHOLDER_MEDIA
        else:
            actual_posters += 1
        sources.append(source)
    if actual_posters == 0:
        return None
    return InputRichBlockCollage(blocks=[
        InputRichBlockPhoto(photo=InputMediaPhoto(
            media=source,
            parse_mode=None,
            show_caption_above_media=None,
        ))
        for source in sources
    ])


def _render_items(items: tuple[ReportItem, ...]) -> list:
    blocks = []
    index = 0
    while index < len(items):
        item = items[index]
        if isinstance(item, Heading):
            blocks.append(InputRichBlockSectionHeading(
                text=_rich_parts(item.parts),
                size=item.level,
            ))
            index += 1
            continue
        if isinstance(item, Rows):
            table = _counter_table(item)
            if table is not None:
                blocks.append(table)
            index += 1
            continue
        if isinstance(item, Table):
            blocks.extend(_catalog_table_blocks(item))
            index += 1
            continue
        if not isinstance(item, Line):
            raise RichReportRenderError("unsupported_item")
        marker = _line_list_marker(item)
        if marker is None:
            blocks.append(_paragraph(item))
            index += 1
            continue

        ordered = marker[0]
        list_items = []
        posters = []
        gallery_eligible = True
        while index < len(items) and isinstance(items[index], Line):
            current_marker = _line_list_marker(items[index])
            if current_marker is None or current_marker[0] != ordered:
                break
            _, value, parts = current_marker
            poster = _poster_from_parts(parts)
            if poster is None:
                gallery_eligible = False
            else:
                posters.append(poster)
            paragraph = InputRichBlockParagraph(text=_rich_parts(parts))
            kwargs = {"blocks": [paragraph]}
            if ordered:
                kwargs.update(value=value, type="1")
            list_items.append(InputRichBlockListItem(**kwargs))
            index += 1
        blocks.append(InputRichBlockList(items=list_items))
        collage = _poster_collage(posters) if ordered and gallery_eligible else None
        if collage is not None:
            blocks.append(collage)
    return blocks


def _render_section(value: Section) -> list:
    if not value.items:
        return []
    first = value.items[0]
    if not isinstance(first, Heading) or not first.collapsible or len(value.items) == 1:
        return _render_items(value.items)
    content = _render_items(value.items[1:])
    if not content:
        return _render_items((first,))
    summary = _rich_parts(first.parts)
    return [InputRichBlockDetails(
        summary=summary,
        blocks=content,
        is_open=first.open,
    )]


def _render_unit(value: Unit, unit_index: int) -> RenderedRichUnit | None:
    top_anchor = f"unit-{unit_index}-top"
    blocks = []
    has_details = False
    for logical_section in value.sections:
        rendered = _render_section(logical_section)
        has_details = has_details or any(
            isinstance(block, InputRichBlockDetails) for block in rendered
        )
        blocks.extend(rendered)
    if not blocks:
        return None
    if has_details:
        blocks.append(InputRichBlockParagraph(text=RichTextAnchorLink(
            text=RichTextSubscript(text="↑ К началу"),
            anchor_name=top_anchor,
        )))
        blocks.insert(0, InputRichBlockAnchor(name=top_anchor))
    message = InputRichMessage(blocks=blocks, skip_entity_detection=True)
    payload = message.model_dump(mode="json", exclude_none=True)
    try:
        validate_rich_payload(payload)
    except RichMessageValidationError as exc:
        raise RichReportRenderError(str(exc)) from exc
    return RenderedRichUnit(message, payload, unit_index)


def render_rich_report(report: Report) -> tuple[RenderedRichUnit, ...]:
    """Отобразить каждую непустую Report.Unit как validated Rich Message."""
    rendered = []
    for unit_index, logical_unit in enumerate(report.units):
        rich_unit = _render_unit(logical_unit, unit_index)
        if rich_unit is not None:
            rendered.append(rich_unit)
    return tuple(rendered)
