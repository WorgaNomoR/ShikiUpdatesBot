# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Локальная карточка статистики и её подпись."""

from collections.abc import Iterable
from dataclasses import (
    dataclass,
    replace,
)
from datetime import (
    datetime,
    timezone,
)
from fractions import Fraction
from html import escape
from io import BytesIO
from pathlib import Path

from PIL import (
    Image,
    ImageDraw,
    ImageFont,
    ImageOps,
)

from runtime import RESOURCE_ROOT
from taste_card import (
    CATEGORY_LABELS,
    TasteCard,
    TasteInput,
    TasteTitle,
    build_taste_card,
)
from utils import russian_count_word

WIDTH = 1200
MAX_HEIGHT = 2400
MAX_BYTES = 5 * 1024 * 1024
CAPTION_LIMIT = 950
FONT_NAME = "NotoSansCJKjp-Regular.otf"
PROJECT_ADDRESS = "github.com/WorgaNomoR/ShikiUpdatesBot"
PROJECT_URL = "https://" + PROJECT_ADDRESS

_BG, _PAPER, _INK, _MUTED = "#FBF0E0", "#FFFFFF", "#101817", "#6A5C50"
_LINE, _TRACK = "#E1D5C6", "#ECE8E2"
_BLUE, _DARK_BLUE, _RED = "#41647B", "#234659", "#A3382C"
_SAND, _SLATE = "#AA8D67", "#779197"
_FORMAT_COLORS = (_BLUE, _SAND, _SLATE, "#565968")
_STATUS_COLORS = (_BLUE, _SLATE, "#57576F", "#C6AA83", "#736762", "#AD493D")
_LEFT, _RIGHT, _TEXT_LEFT, _TEXT_RIGHT = 52, 1148, 78, 1122
_AXIS_LABELS = {"genre": "Жанр", "theme": "Тема", "format": "Формат"}
_AXIS_HEADINGS = {
    "genre": "Средние оценки по жанрам",
    "theme": "Средние оценки по темам",
    "format": "Средние оценки по форматам",
}


class TasteRenderError(ValueError):
    """Локальный макет или ресурсы не позволяют сформировать фото."""


@dataclass(frozen=True)
class RenderedTasteCard:
    """Замороженная пара фото и подписи до первой попытки Telegram."""

    image: bytes
    caption: str
    width: int
    height: int
    filename: str = "profile-statistics.jpg"


@dataclass(frozen=True)
class _Layout:
    rare: bool = True
    two_line_titles: bool = True
    drop_example: bool = True
    years: bool = True


def _number(value: Fraction | int, *, signed: bool = False, compact: bool = False) -> str:
    label = format(float(value), "+.1f" if signed else ".1f").replace(".", ",").replace("-", "−")
    return label.removesuffix(",0") if compact else label


def _percent(value: Fraction) -> str:
    if 0 < value < Fraction(1, 1000):
        return "<0,1%"
    return _number(value * 100, compact=True) + "%"


def _utf16(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _short(value: str, budget: int) -> str:
    """Сократить подпись ссылки, не разрывая Unicode-символ."""
    if _utf16(value) <= budget:
        return value
    result = []
    used = 0
    for char in value:
        size = _utf16(char)
        if used + size > budget - 1:
            break
        result.append(char)
        used += size
    return "".join(result).rstrip() + "…"


def caption_for(examples: Iterable[TasteTitle]) -> str:
    """Включить проект и только действительно показанные примеры."""
    unique = {}
    for title in examples:
        unique.setdefault(title.id, title)
    link = f'<a href="{PROJECT_URL}">ShikiUpdatesBot на GitHub</a>'
    if not unique:
        return link
    lines = ["Тайтлы из карточки:"]
    visible = [*lines, "", "ShikiUpdatesBot на GitHub"]
    for index, title in enumerate(tuple(unique.values())[:6], 1):
        suffix = " · брошено" if title.status == "dropped" else ""
        label = _short(title.title, 90)
        lines.append(f'{index}. <a href="{escape(title.url, quote=True)}">{escape(label)}</a>{suffix}')
        visible.append(f"{index}. {label}{suffix}")
    if _utf16("\n".join(visible)) > CAPTION_LIMIT:
        raise TasteRenderError("caption_limit")
    return "\n".join((*lines, "", link))


def _footer_notes(card: TasteCard) -> tuple[str, ...]:
    source = card.source
    notes = []
    if card.rated:
        sample = f"Оценено {card.rated}."
        if card.paired < card.rated:
            sample += f" Рейтинг Shikimori — у {card.paired} из них."
        notes.append(sample)
        if card.paired:
            notes.append("Рейтинги Shikimori могут быть старше списка.")
        missing = [
            f"{label} {count}/{card.rated}"
            for label, count in zip(
                ("жанры", "темы", "годы"), (*card.preferences.coverage[:2], len(card.diversity.years)),
            )
            if count < card.rated
        ]
        if missing:
            notes.append("Данные об оценённых: " + " · ".join(missing) + ".")
    drop = card.dropping
    if drop.pattern:
        missing = [
            f"{label} {count}/{drop.total}"
            for label, count in zip(("жанры", "темы"), drop.coverage)
            if count < drop.total
        ]
        if missing:
            notes.append("Данные о завершённых и брошенных: " + " · ".join(missing) + ".")
    gaps = []
    if source.unresolved:
        gaps.append(f"тип не определён у {source.unresolved}")
    if source.excluded:
        gaps.append(f"непригодных записей {source.excluded}")
    if gaps:
        notes.append("В исходном списке: " + " · ".join(gaps) + ".")
    notes.append("Только тайтлы из текущего списка.")
    generated = source.generated_at
    synced = source.synced_at
    if synced is None:
        notes.append(f"Дата списка неизвестна · создано {generated:%d.%m.%Y %H:%M} UTC.")
    elif synced.date() == generated.date():
        notes.append(f"{generated:%d.%m.%Y} UTC · список проверен в {synced:%H:%M}, создано в {generated:%H:%M}.")
    else:
        notes.append(f"Список проверен {synced:%d.%m.%Y %H:%M} · создано {generated:%d.%m.%Y %H:%M} UTC.")
    return tuple(notes)


class _Canvas:
    """Рисование в масштабе 2× с одинаковым измерением и выводом глифов."""

    def __init__(self, font_path: Path):
        self.image = Image.new("RGB", (1, 1))
        self.draw = ImageDraw.Draw(self.image)
        self.font_path = font_path
        self.fonts = {}
        self.examples = []

    def allocate(self, height: int) -> None:
        """Выделить только холст уже измеренной высоты."""
        self.image = Image.new("RGB", (WIDTH * 2, height * 2), _BG)
        self.draw = ImageDraw.Draw(self.image)
        self.examples.clear()

    def font(self, size):
        if size not in self.fonts:
            self.fonts[size] = ImageFont.truetype(
                str(self.font_path), size * 2, layout_engine=ImageFont.Layout.BASIC,
            )
        return self.fonts[size]

    def bounds(self, text, size, bold=False):
        return self.draw.textbbox((0, 0), text, font=self.font(size), anchor="lt", stroke_width=int(bold))

    def width(self, text, size, bold=False):
        left, _, right, _ = self.bounds(text, size, bold)
        return (right - left) / 2

    def height(self, text, size, bold=False):
        _, top, _, bottom = self.bounds(text, size, bold)
        return (bottom - top) / 2

    def _prefix(self, text, size, width, *, bold=False, suffix=""):
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self.width(text[:middle].rstrip() + suffix, size, bold) <= width:
                low = middle
            else:
                high = middle - 1
        return low

    def wrap(self, text, size, width, lines, *, bold=False):
        result = []
        for index in range(lines):
            if self.width(text, size, bold) <= width:
                result.append(text)
                break
            last = index == lines - 1
            suffix = "…" if last else ""
            cut = self._prefix(text, size, width, bold=bold, suffix=suffix)
            if not last:
                space = text.rfind(" ", 0, cut + 1)
                if space > cut * 0.65:
                    cut = space
            result.append(text[:cut].rstrip() + suffix)
            text = text[cut:].lstrip()
        return result

    def text(self, x, y, text, size=34, color=_INK, *, align="left", bold=False, width=None, middle=False):
        if width is not None and self.width(text, size, bold) > width:
            text = self.wrap(text, size, width, 1, bold=bold)[0]
        left, top, right, bottom = self.bounds(text, size, bold)
        height = (bottom - top) / 2
        x_offset = left if align == "left" else right if align == "right" else (left + right) / 2
        if middle:
            y -= height / 2
        self.draw.text(
            (round(x * 2 - x_offset), round(y * 2 - top)), text, font=self.font(size), fill=color,
            anchor="lt", stroke_width=int(bold), stroke_fill=color,
        )
        return y + height

    def box(self, left, top, right, bottom, color, radius=0, outline=None):
        if right <= left or bottom <= top:
            return
        self.draw.rounded_rectangle(
            (left * 2, top * 2, right * 2 - 1, bottom * 2 - 1),
            radius=radius * 2, fill=color, outline=outline, width=2,
        )

    def dot(self, x, y, radius, fill, outline=None):
        self.draw.ellipse(
            ((x - radius) * 2, (y - radius) * 2, (x + radius) * 2, (y + radius) * 2),
            fill=fill, outline=outline, width=4,
        )

    def panel(self, y, end, left=_LEFT, right=_RIGHT):
        self.box(left, y, right, end, _PAPER, 22, _LINE)

    def bar(self, left, right, y, ratio, color, height=14):
        self.box(left, y, right, y + height, _TRACK, height // 2)
        fill = round((right - left) * ratio)
        if ratio:
            fill = max(1, fill)
            self.box(left, y, left + fill, y + height, color, min(height // 2, fill // 2))

    def header(self, card, statuses):
        source = card.source
        self.text(52, 31, f"{CATEGORY_LABELS[source.category]} · За всё время", 37, _DARK_BLUE, bold=True)
        self.text(52, 88, "Статистика профиля", 76, bold=True, width=889)
        self.text(52, 184, f"{source.profile} в Shikimori", 35, _MUTED, width=889)
        self.panel(240, 368)
        baseline = card.preferences.baseline
        for i, (label, value) in enumerate((
            ("В списке", len(source.titles)), (statuses[0], card.overview.statuses[0]),
            ("Брошено", card.dropping.dropped), ("Средний балл", _number(baseline) if baseline else "—"),
        )):
            left = _TEXT_LEFT + i * 270
            if i:
                self.box(left - 17, 262, left - 16, 345, _LINE)
            self.text(left, 255, str(value), 57, _RED if i == 2 and value else _DARK_BLUE, bold=True, width=248)
            self.text(left, 319, label, 32, width=250)
        return 386

    def charts(self, y, card):
        formats = card.overview.formats
        end = y + (150 if len(formats) == 1 and not card.rated else 276)
        self.panel(y, end, _LEFT, 580)
        self.panel(y, end, 602, _RIGHT)
        self.text(78, y + 22, "Форматы", 42, bold=True)
        self.text(628, y + 22, "Личные оценки", 42, bold=True)
        total = len(card.source.titles)
        if len(formats) == 1:
            self.text(78, y + 92, "Все тайтлы · " + formats[0][0].lower(), 35, _MUTED, width=477)
        else:
            start = 0
            for index, ((label, count), color) in enumerate(zip(formats, _FORMAT_COLORS)):
                finish = start + count
                self.draw.pieslice(
                    (156, (y + 87) * 2, 516, (y + 267) * 2),
                    start=360 * start / total - 90, end=360 * finish / total - 90, fill=color,
                )
                start = finish
                top = y + 91 + index * 44
                self.box(279, top + 6, 299, top + 25, color, 4)
                self.text(310, top, label, 33, width=148)
                self.text(552, top, _percent(Fraction(count, total)), 31, _DARK_BLUE, align="right", width=94)
            self.dot(168, y + 177, 57, _PAPER)
        if not card.rated:
            self.text(628, y + 92, "Пока нет личных оценок", 35, _MUTED, width=494)
            return end + 18

        top, bottom, left, step = y + 102, y + 219, 694, 42
        maximum = max(card.histogram)
        ceiling = (maximum + 4) // 5 * 5
        for count in (0, ceiling // 2, ceiling):
            level = bottom - round((bottom - top) * count / ceiling)
            self.box(left, level, left + step * 10, level + 1, _LINE)
            if count:
                self.text(left - 5, level, str(count), 21, _MUTED, align="right", middle=True, width=64)
        for index, count in enumerate(card.histogram):
            x = left + step * index
            if count:
                height = max(1, round((bottom - top) * count / ceiling))
                self.box(x + 6, bottom - height, x + 36, bottom, _RED if index < 4 else _BLUE, 3)
            self.text(x + step / 2, y + 238, str(index + 1), 29, _MUTED, align="center")
        return end + 18

    def statuses(self, y, card, labels):
        rows = [(count, label, color) for count, label, color in zip(
            card.overview.statuses, labels, _STATUS_COLORS,
        ) if count]
        end = y + 110 + 48 * ((len(rows) + 2) // 3)
        self.panel(y, end)
        self.text(_TEXT_LEFT, y + 21, "Что в списке", 42, bold=True)
        seen = 0
        for i, (count, label, color) in enumerate(rows):
            left = _TEXT_LEFT + round(1044 * seen / len(card.source.titles))
            seen += count
            right = _TEXT_LEFT + round(1044 * seen / len(card.source.titles))
            self.box(left, y + 77, right, y + 94, color)
            x, top = _TEXT_LEFT + 351 * (i % 3), y + 112 + 48 * (i // 3)
            self.box(x, top + 6, x + 17, top + 23, color, 3)
            self.text(x + 27, top, f"{label} {count}", 32, width=314)
        return end + 18

    def _single(self, y, title, detail):
        heading_size = 39
        detail_width = 625
        heading_width = _TEXT_RIGHT - _TEXT_LEFT - detail_width - 18
        title = self.wrap(title, heading_size, heading_width, 2, bold=True)
        detail = self.wrap(detail, 32, detail_width, 2)
        end = y + max(len(title) * 44, len(detail) * 38) + 46
        self.panel(y, end)
        for index, line in enumerate(title):
            self.text(_TEXT_LEFT, y + 22 + 44 * index, line, heading_size, bold=True)
        for index, line in enumerate(detail):
            self.text(_TEXT_RIGHT, y + 27 + 38 * index, line, 32, _MUTED, align="right")
        return end + 18

    def genres(self, y, card):
        data = card.overview
        if not data.genres:
            return self._single(y, "Жанры", "Пока нет данных о жанрах")
        end = y + 88 + 41 * len(data.genres)
        self.panel(y, end)
        self.text(_TEXT_LEFT, y + 19, "Частые жанры", 42, bold=True)
        total = len(card.source.titles)
        overlap = "У тайтла может быть несколько жанров"
        if data.known_genres < total:
            self.text(_TEXT_RIGHT, y + 16, f"Жанры известны у {data.known_genres} из {total} тайтлов",
                      23, _MUTED, align="right", width=601)
            self.text(_TEXT_RIGHT, y + 44, overlap, 23, _MUTED, align="right", width=601)
        else:
            self.text(_TEXT_RIGHT, y + 27, overlap, 29, _MUTED, align="right", width=601)
        for index, (label, count) in enumerate(data.genres):
            top = y + 74 + index * 41
            ratio = Fraction(count, data.known_genres)
            self.text(_TEXT_LEFT, top, label, 34, bold=True, width=296)
            self.bar(397, 804, top + 12, ratio, _BLUE)
            unit = russian_count_word(count, "тайтл", "тайтла", "тайтлов")
            self.text(997, top + 3, f"{count} {unit}", 28, _MUTED, align="right", width=183)
            self.text(_TEXT_RIGHT, top, _percent(ratio), 33, _DARK_BLUE, bold=True, align="right", width=119)
        return end + 18

    def preferences(self, y, card):
        data = card.preferences
        rows = tuple(row for row in (data.higher, data.lower) if row is not None)
        if not rows:
            message = ("Заметной разницы между группами нет" if data.state == "neutral"
                       else "Пока мало оценок для сравнения")
            return self._single(y, "Предпочтения", message) if card.rated else y
        axes = {row.group.axis for row in rows}
        heading = _AXIS_HEADINGS[next(iter(axes))] if len(axes) == 1 else "Предпочтения профиля"
        end = y + 91 + 70 * len(rows)
        self.panel(y, end)
        self.text(_TEXT_LEFT, y + 21, heading, 40, bold=True, width=638)
        self.text(_TEXT_RIGHT, y + 28, "Средний балл профиля " + _number(data.baseline),
                  28, _MUTED, align="right", width=404)
        left, right = 398, 789
        reference = left + round((right - left) * (data.baseline - 1) / 9)
        for index, item in enumerate(rows):
            top = y + 80 + index * 70
            color = _DARK_BLUE if item.delta > 0 else _RED
            self.text(_TEXT_LEFT, top, item.group.label, 39, bold=True, width=296)
            count = len(item.group.titles)
            label = f"{count} {russian_count_word(count, 'оценка', 'оценки', 'оценок')}"
            if len(axes) > 1:
                label = f"{_AXIS_LABELS[item.group.axis]} · {label}"
            self.text(_TEXT_LEFT, top + 44, label, 24, _MUTED, width=296)
            mark = left + round((right - left) * (item.mean - 1) / 9)
            self.box(left, top + 21, right, top + 28, _TRACK, 4)
            self.box(min(mark, reference), top + 20, max(mark, reference), top + 29, color, 4)
            self.box(reference, top + 9, reference + 1, top + 41, _MUTED)
            self.dot(mark, top + 25, 7, color)
            self.text(868, top + 2, _number(item.mean), 48, color, bold=True, align="center", width=126)
            direction = "выше" if item.delta > 0 else "ниже"
            self.text(_TEXT_RIGHT, top + 11, f"на {_number(abs(item.delta))} {direction}",
                      31, color, align="right", width=190)
        return end + 18

    def comparison(self, y, card, lines):
        rows = tuple(row for row in (card.above, card.below) if row is not None)
        if not rows:
            messages = {
                "neutral": "Заметных расхождений нет",
                "unavailable": "Пока нет пар оценок",
                "insufficient": "Пока мало пар оценок",
            }
            return self._single(y, "Расхождения оценок", messages[card.comparison_state]) if card.rated else y

        heading = "Расхождения оценок по тайтлам"
        legend_half_height = max(9, *(self.height(label, 25) / 2 for label in ("Профиль", "Shikimori")))
        head_bottom = max(y + 21 + self.height(heading, 40, True), y + 43 + legend_half_height)
        title_rows = []
        for row in rows:
            label = ("Брошено · " if row.title.status == "dropped" else "") + row.title.title
            wrapped = self.wrap(label, 29, 470, lines)
            height = sum(self.height(line, 29) + 4 for line in wrapped)
            title_rows.append((row, wrapped, max(53, height)))
        top = round(head_bottom + 28)
        end = top + round(sum(height + 16 for _, _, height in title_rows))
        self.panel(y, end)
        self.text(_TEXT_LEFT, y + 21, heading, 40, bold=True, width=707)
        marker = "#4D5053"
        center = y + 43
        for x, label, fill, outline in (
            (812, "Профиль", marker, None), (967, "Shikimori", _PAPER, marker),
        ):
            self.dot(x, center, 9, fill, outline)
            self.text(x + 17, center, label, 25, _MUTED, middle=True)
        for index, (row, wrapped, height) in enumerate(title_rows):
            cursor = top
            for line in wrapped:
                cursor = self.text(_TEXT_LEFT, cursor, line, 29) + 4
            center = top + 25
            color = _DARK_BLUE if row.delta > 0 else _RED
            left, right = 591, 1006
            profile = left + round((right - left) * row.title.score / 10)
            shiki = left + round((right - left) * row.title.community / 10)
            self.box(left, center - 3, right, center + 3, _TRACK, 3)
            self.box(min(profile, shiki), center - 4, max(profile, shiki), center + 4, color, 4)
            self.dot(profile, center, 24, color)
            self.dot(shiki, center, 24, _PAPER, _MUTED)
            self.text(profile, center, str(row.title.score), 27, _PAPER, align="center", middle=True, bold=True)
            self.text(shiki, center, _number(row.title.community, compact=True), 25, _INK,
                      align="center", middle=True, bold=True)
            self.text(_TEXT_RIGHT, center, _number(row.delta, signed=True, compact=True), 35, color,
                      align="right", middle=True, bold=True, width=83)
            if index + 1 < len(title_rows):
                self.box(_TEXT_LEFT, top + height + 5, _TEXT_RIGHT, top + height + 6, _TRACK)
            top += height + 16
            self.examples.append(row.title)
        return end + 18

    def dropping(self, y, card, example):
        data = card.dropping
        if not data.total:
            return y
        if data.pattern is None:
            state = "Направление не выделяется" if data.state == "neutral" else "Для сравнения групп мало данных"
            return self._single(
                y, "Доля брошенных",
                f"{data.dropped} из {data.total} завершённых и брошенных · {_percent(data.rate)}. {state}.",
            )

        subtitle = "Среди завершённых и брошенных тайтлов"
        data_y = y + 68 + self.height(subtitle, 28) + 33
        label = "Пример · " + data.pattern.example.title
        shortened = self.wrap(label, 29, 1044, 1)[0]
        example_y = data_y + 111
        end = round(example_y + (self.height(shortened, 29) if example else 0) + 21)
        self.panel(y, end)
        self.text(_TEXT_LEFT, y + 21, "Что доводилось бросать чаще", 40, bold=True)
        self.text(_TEXT_LEFT, y + 68, subtitle, 28, _MUTED)
        for index, (group, dropped, total, color) in enumerate((
            (data.pattern.group.label, data.pattern.dropped, len(data.pattern.group.titles), _RED),
            ("В целом", data.dropped, data.total, _BLUE),
        )):
            top = data_y + index * 53
            ratio = Fraction(dropped, total)
            self.text(_TEXT_LEFT, top + 2, group, 38, bold=index == 0, width=283)
            self.bar(379, 795, top + 13, ratio, color, 18)
            self.text(969, top + 4, f"{dropped} из {total}", 31, _MUTED, align="right", width=161)
            self.text(_TEXT_RIGHT, top, _percent(ratio), 41, color, bold=True, align="right", width=143)
        if example:
            self.text(_TEXT_LEFT, example_y, shortened, 29, _MUTED)
            self.examples.append(data.pattern.example)
        return end + 18

    def diversity(self, y, card, options):
        data = card.diversity
        if options.years and data.years:
            first, last = data.years[0], data.years[-1]
            self.panel(y, y + 66)
            self.text(_TEXT_LEFT, y + 20, "Годы выпуска оценённых тайтлов", 35, bold=True, width=730)
            self.text(_TEXT_RIGHT, y + 16, str(first) if first == last else f"{first}–{last}",
                      41, _DARK_BLUE, align="right", width=283)
            y += 84
        if options.rare and data.rare and all(title.id != data.rare.id for title in self.examples):
            label = ("брошено · " if data.rare.status == "dropped" else "") + data.rare.title
            y = self._single(y, f"Редкость · {data.rare_label}", label)
            self.examples.append(data.rare)
        return y

    def footer(self, y, card):
        self.box(_LEFT, y, 893, y + 1, _LINE)
        cursor = y + 13
        for note in _footer_notes(card):
            for line in self.wrap(note, 21, 841, 2):
                cursor = self.text(_LEFT, cursor, line, 21, _MUTED) + 5
        cursor = self.text(_LEFT, cursor + 3, "Made by ShikiUpdatesBot", 27, _DARK_BLUE, bold=True) + 10
        return self.text(_LEFT, cursor, PROJECT_ADDRESS, 29)

    def layout(self, card, options, qr_height):
        self.examples.clear()
        labels = (("Просмотрено", "Смотрю", "Пересматриваю") if card.source.category == "anime"
                  else ("Прочитано", "Читаю", "Перечитываю"))
        labels += ("В планах", "Отложено", "Брошено")
        y = self.header(card, labels)
        y = self.charts(y, card)
        y = self.statuses(y, card, labels)
        y = self.genres(y, card)
        y = self.preferences(y, card)
        y = self.comparison(y, card, 2 if options.two_line_titles else 1)
        y = self.dropping(y, card, options.drop_example)
        y = self.diversity(y, card, options)
        bottom = self.footer(y, card)
        return round(max(y + qr_height + 8, bottom) + 16), y + 8

    def avatar(self, avatar, logo_path):
        size, x, y = 170, 978, 35
        self.box(x - 3, y - 3, x + size + 3, y + size + 3, _PAPER, 22, _LINE)
        art = None
        if avatar:
            try:
                with Image.open(BytesIO(avatar), formats=("PNG",)) as source:
                    if source.size == (160, 160):
                        art = source.convert("RGBA")
            except (OSError, ValueError):
                pass
        tile = Image.new("RGB", (size * 2, size * 2), _PAPER)
        if art is not None and art.getchannel("A").getbbox():
            art = ImageOps.fit(art, tile.size, Image.Resampling.BICUBIC)
        else:
            with Image.open(logo_path) as source:
                art = ImageOps.contain(source.convert("RGBA"), (size * 2 - 46, size * 2 - 46))
        tile.paste(art, ((tile.width - art.width) // 2, (tile.height - art.height) // 2), art.getchannel("A"))
        mask = Image.new("L", tile.size)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, tile.width - 1, tile.height - 1), radius=40, fill=255)
        self.image.paste(tile, (x * 2, y * 2), mask)


def render_taste_card(
    card: TasteCard, *, avatar: bytes | None = None, resource_root: Path = RESOURCE_ROOT,
) -> RenderedTasteCard:
    """Измерить макет, уместить его в лимит и собрать одну пару JPEG и HTML."""
    if not card.available:
        raise TasteRenderError("unavailable_card")
    assets = resource_root / "assets" / "taste-card"
    try:
        with Image.open(assets / "repository-qr.png") as asset:
            qr = asset.convert("RGBA")
        ink = qr.getchannel("A").getbbox()
        if qr.size != (246, 246) or ink != (24, 24, 222, 222):
            raise TasteRenderError("qr_geometry")
        canvas = _Canvas(assets / FONT_NAME)
        options = _Layout()
        height, qr_y = canvas.layout(card, options, qr.height)
        # Второстепенные примеры, переносы и диапазон лет уступают место основным числам.
        for field in ("rare", "two_line_titles", "drop_example", "years"):
            if height <= MAX_HEIGHT:
                break
            options = replace(options, **{field: False})
            height, qr_y = canvas.layout(card, options, qr.height)
        if height > MAX_HEIGHT:
            raise TasteRenderError("image_height")
        canvas.allocate(height)
        canvas.layout(card, options, qr.height)
        canvas.avatar(avatar, assets / "logo.png")
        image = canvas.image.resize((WIDTH, height), Image.Resampling.LANCZOS)
        image.paste(qr, (_RIGHT - ink[2], qr_y), qr)

        for quality in (92, 85, 78):
            output = BytesIO()
            image.save(output, format="JPEG", quality=quality, subsampling=0)
            content = output.getvalue()
            if len(content) <= MAX_BYTES:
                return RenderedTasteCard(content, caption_for(canvas.examples), WIDTH, height)
        raise TasteRenderError("image_size")
    except (OSError, ValueError, OverflowError) as error:
        if isinstance(error, TasteRenderError):
            raise
        raise TasteRenderError("local_render") from error


def smoke_card(category: str = "anime") -> TasteCard:
    """Условный плотный срез для проверки реальных ресурсов и frozen-рендера."""
    if category not in CATEGORY_LABELS:
        raise ValueError("taste_category")
    titles = []
    media = "animes" if category == "anime" else "mangas"
    formats = {
        "anime": ("tv", "movie", "ova", "ona"),
        "manga": ("manga", "manhwa", "manhua"),
        "ranobe": ("light_novel", "novel"),
    }[category]
    for index in range(1, 161):
        tags = ["Приключения"]
        if index <= 24:
            tags.append("Детектив")
        if 25 <= index <= 40:
            tags.append("Комедия")
        if 41 <= index <= 60:
            tags.append("Триллер")
        if 61 <= index <= 72:
            tags.append("Фэнтези")
        if index <= 100:
            status = "dropped" if index in {1, 25, *range(41, 51)} else "completed"
        elif index <= 120:
            status = "dropped" if index <= 108 else "completed"
        else:
            status = ("watching", "rewatching", "planned", "on_hold")[index % 4]
        score = 9 if index <= 24 else 5 if index <= 40 else 8
        if index <= 2:
            score = 10
        elif index == 25:
            score = 4
        name = {
            1: "Небо над городом / 眠らない街の空と海の物語、最初から最後まで",
            25: "Тетрадь странствий по тихим улицам несуществующего города",
            41: "Хроники города, который разучился спать и давно потерял своё название",
        }.get(index, f"История {index}")
        kind = formats[(index - 1) % len(formats)] if index <= 60 else formats[0]
        community = Fraction(5) if index == 1 else Fraction(8) if index == 25 else Fraction(score)
        titles.append(TasteTitle(
            index, name, f"https://shikimori.io/{media}/{index}", status, kind,
            score if index <= 112 else None, community if index <= 94 else None,
            1986 + index % 40, tuple(tags) if index <= 140 else (), ("Путешествие",),
        ))
    now = datetime(2026, 11, 23, 9, 30, tzinfo=timezone.utc)
    return build_taste_card(TasteInput("DEMO (условные данные)", category, now, now.replace(hour=8), tuple(titles)))
