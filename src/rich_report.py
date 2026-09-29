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

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RenderedRichUnit:
    """Одна independently validated Rich Message delivery unit."""

    message: InputRichMessage
    payload: dict
    unit_index: int
    fragment_index: int = 0
    fallback_unit: Unit | None = None


def _rich_inline(part: Inline):
    if isinstance(part, Bold):
        return RichTextBold(text=part.value)
    if isinstance(part, Italic):
        return RichTextItalic(text=part.value)
    if isinstance(part, Code):
        return RichTextCode(text=part.value)
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
        isinstance(item, (Gallery, Heading, Rows, Table))
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


def _gallery_blocks(value: Gallery) -> list:
    """Отобразить до трёх пригодных обложек без placeholder-подстановки."""
    if len(value.posters) > 3:
        raise RichReportRenderError("gallery_size")
    sources = []
    for poster in value.posters:
        if not isinstance(poster, Poster):
            raise RichReportRenderError("render")
        source = _safe_poster_url(poster.url)
        if source is not None:
            sources.append(source)
    photos = [
        InputRichBlockPhoto(photo=InputMediaPhoto(
            media=source,
            parse_mode=None,
            show_caption_above_media=None,
        ))
        for source in sources
    ]
    if len(photos) == 1:
        return photos
    if len(photos) in {2, 3}:
        return [InputRichBlockCollage(blocks=photos)]
    return []


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
        if isinstance(item, Gallery):
            blocks.extend(_gallery_blocks(item))
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


def _render_unit(
    value: Unit,
    unit_index: int,
    fragment_index: int = 0,
) -> RenderedRichUnit | None:
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
    return RenderedRichUnit(
        message,
        payload,
        unit_index,
        fragment_index,
        value,
    )


def _limit_overflow(exc: RichReportRenderError) -> bool:
    """Разрешить pagination только для двух официальных лимитов сообщения."""
    return exc.reason in {"blocks", "characters"}


def _fragment_table(value: Table, groups: tuple[TableGroup, ...]) -> Table:
    """Сохранить геометрию таблицы при выборе цельных логических групп."""
    return Table(
        columns=value.columns,
        groups=groups,
        header=value.header,
        separate_groups=value.separate_groups,
    )


def _continuation_heading(value: Heading) -> Heading:
    """Явно отличить transport-продолжение от нового логического раздела."""
    return Heading(
        (*value.parts, Text(" · продолжение")),
        level=value.level,
        collapsible=value.collapsible,
        open=value.open,
    )


def _fragment_section(
    value: Section,
    table: Table,
    groups: tuple[TableGroup, ...],
    *,
    continuation: bool = False,
) -> Section:
    """Заменить единственную таблицу каталога выбранными карточками."""
    heading = value.items[0]
    if not isinstance(heading, Heading):
        raise RichReportRenderError("render")
    if continuation:
        heading = _continuation_heading(heading)
    return Section((heading, _fragment_table(table, groups)))


def _inline_value(part: Inline) -> str:
    """Получить исходный текст inline-узла без transport-разметки."""
    if isinstance(part, (Link, Title)):
        return part.text
    return part.value


def _clone_text_inline(part: Inline, text: str) -> Inline:
    """Продолжить только допускающий разбиение текстовый стиль."""
    if isinstance(part, (Text, Bold, Italic, Code)):
        return type(part)(text)
    raise RichReportRenderError("render")


def _line_character_count(value: Line) -> int:
    return sum(len(_inline_value(part)) for part in value.parts)


def _take_line_prefix(value: Line, count: int) -> tuple[Line, Line]:
    """Отделить Unicode-prefix, не разрезая ссылки и Title nodes."""
    prefix: list[Inline] = []
    remainder: list[Inline] = []
    available = count
    taking = True
    for part in value.parts:
        text = _inline_value(part)
        if not taking:
            remainder.append(part)
            continue
        if isinstance(part, (Link, Title)):
            if len(text) <= available:
                prefix.append(part)
                available -= len(text)
            else:
                remainder.append(part)
                taking = False
            continue
        taken = min(len(text), available)
        if taken:
            prefix.append(_clone_text_inline(part, text[:taken]))
            available -= taken
        if taken < len(text):
            remainder.append(_clone_text_inline(part, text[taken:]))
            taking = False
    return Line(tuple(prefix)), Line(tuple(remainder))


def _group_with_after(value: TableGroup, after: tuple[Line, ...]) -> TableGroup:
    return TableGroup(value.rows, value.fallback, after)


def _after_only_group(after: tuple[Line, ...] = ()) -> TableGroup:
    return TableGroup((), (), after)


def _paginate_rich_unit(value: Unit, unit_index: int) -> list[RenderedRichUnit]:
    """Упаковать list-карточки по фактическим serialized Rich budgets."""
    if len(value.sections) < 2 or not value.sections[0].items:
        raise RichReportRenderError("render")
    header = value.sections[0]
    media_heading = header.items[0]
    if not isinstance(media_heading, Heading):
        raise RichReportRenderError("render")

    table_sections: list[tuple[Section, Table]] = []
    for logical_section in value.sections[1:]:
        if (
            len(logical_section.items) != 2
            or not isinstance(logical_section.items[0], Heading)
            or not isinstance(logical_section.items[1], Table)
            or not logical_section.items[1].separate_groups
            or not logical_section.items[1].groups
        ):
            raise RichReportRenderError("render")
        table_sections.append((logical_section, logical_section.items[1]))

    compact_header = Section((_continuation_heading(media_heading),))
    rendered: list[RenderedRichUnit] = []
    working_groups = [list(table.groups) for _, table in table_sections]
    FragmentPart = tuple[int, tuple[TableGroup, ...], bool]

    def candidate(
        parts: tuple[FragmentPart, ...],
        *,
        message_continuation: bool | None = None,
    ) -> Unit:
        if message_continuation is None:
            message_continuation = bool(rendered)
        fragment_sections = tuple(
            _fragment_section(
                table_sections[section_index][0],
                table_sections[section_index][1],
                groups,
                continuation=status_continuation,
            )
            for section_index, groups, status_continuation in parts
        )
        fragment_header = compact_header if message_continuation else header
        return Unit((fragment_header, *fragment_sections))

    def fits(
        parts: tuple[FragmentPart, ...],
        *,
        message_continuation: bool | None = None,
    ) -> bool:
        try:
            _render_unit(
                candidate(
                    parts,
                    message_continuation=message_continuation,
                ),
                unit_index,
            )
        except RichReportRenderError as exc:
            if _limit_overflow(exc):
                return False
            raise
        return True

    def emit(parts: tuple[FragmentPart, ...]) -> None:
        fragment = candidate(parts)
        rich = _render_unit(fragment, unit_index, len(rendered))
        if rich is None:
            raise RichReportRenderError("render")
        rendered.append(rich)

    def largest_prefix(
        section_index: int,
        base: TableGroup,
        remaining: Line,
        *,
        status_continuation: bool,
        message_continuation: bool,
    ) -> tuple[Line, Line]:
        low = 1
        high = _line_character_count(remaining)
        best: tuple[Line, Line] | None = None
        while low <= high:
            middle = (low + high) // 2
            prefix, tail = _take_line_prefix(remaining, middle)
            if not prefix.parts:
                low = middle + 1
                continue
            trial = _group_with_after(base, (*base.after, prefix))
            if fits(
                ((section_index, (trial,), status_continuation),),
                message_continuation=message_continuation,
            ):
                best = prefix, tail
                low = middle + 1
            else:
                high = middle - 1
        if best is None:
            raise RichReportRenderError("characters")
        return best

    def split_group(
        section_index: int,
        group_index: int,
        group: TableGroup,
    ) -> list[TableGroup]:
        card = _group_with_after(group, ())
        status_continuation = group_index > 0
        message_continuation = bool(rendered)
        first_part = ((section_index, (card,), status_continuation),)
        if not fits(
            first_part,
            message_continuation=message_continuation,
        ):
            # Повторный render сохраняет точный blocks/characters reason.
            _render_unit(
                candidate(
                    first_part,
                    message_continuation=message_continuation,
                ),
                unit_index,
            )
            raise RichReportRenderError("render")
        pieces = [card]
        for after_line in group.after:
            remaining = after_line
            while remaining.parts:
                current = pieces[-1]
                piece_continuation = status_continuation or len(pieces) > 1
                piece_message_continuation = (
                    message_continuation or len(pieces) > 1
                )
                complete = _group_with_after(
                    current,
                    (*current.after, remaining),
                )
                if fits(
                    ((section_index, (complete,), piece_continuation),),
                    message_continuation=piece_message_continuation,
                ):
                    pieces[-1] = complete
                    break
                try:
                    prefix, tail = largest_prefix(
                        section_index,
                        current,
                        remaining,
                        status_continuation=piece_continuation,
                        message_continuation=piece_message_continuation,
                    )
                except RichReportRenderError as exc:
                    if exc.reason != "characters" or not (
                        current.rows or current.fallback or current.after
                    ):
                        raise
                    pieces.append(_after_only_group())
                    continue
                pieces[-1] = _group_with_after(
                    current,
                    (*current.after, prefix),
                )
                remaining = tail
                if remaining.parts:
                    pieces.append(_after_only_group())
        return pieces

    section_index = 0
    group_index = 0
    while section_index < len(table_sections):
        fragment_parts: list[FragmentPart] = []
        while section_index < len(table_sections):
            groups = working_groups[section_index]
            if group_index >= len(groups):
                section_index += 1
                group_index = 0
                continue

            low = group_index + 1
            high = len(groups)
            best_end = group_index
            while low <= high:
                middle = (low + high) // 2
                proposed = tuple(groups[group_index:middle])
                candidate_parts = (*fragment_parts, (
                    section_index,
                    proposed,
                    group_index > 0,
                ))
                if fits(tuple(candidate_parts)):
                    best_end = middle
                    low = middle + 1
                else:
                    high = middle - 1

            if best_end > group_index:
                if fragment_parts and best_end < len(groups):
                    # Соседний статус добавляется в хвост только целиком.
                    break
                fragment_parts.append((
                    section_index,
                    tuple(groups[group_index:best_end]),
                    group_index > 0,
                ))
                group_index = best_end
                if group_index == len(groups):
                    section_index += 1
                    group_index = 0
                    continue
                break

            if fragment_parts:
                break

            pieces = split_group(
                section_index,
                group_index,
                groups[group_index],
            )
            groups[group_index:group_index + 1] = pieces

        if not fragment_parts:
            raise RichReportRenderError("render")
        emit(tuple(fragment_parts))

    if not rendered:
        raise RichReportRenderError("render")
    return rendered


def render_rich_report(report: Report) -> tuple[RenderedRichUnit, ...]:
    """Отобразить Report.Unit как один или несколько validated Rich payloads."""
    rendered = []
    for unit_index, logical_unit in enumerate(report.units):
        try:
            rich_unit = _render_unit(logical_unit, unit_index)
        except RichReportRenderError as exc:
            if not _limit_overflow(exc):
                raise
            try:
                rendered.extend(_paginate_rich_unit(logical_unit, unit_index))
            except RichReportRenderError as pagination_exc:
                if pagination_exc.reason == "render":
                    raise exc from pagination_exc
                raise
        else:
            if rich_unit is not None:
                rendered.append(rich_unit)
    return tuple(rendered)
