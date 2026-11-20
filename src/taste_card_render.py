# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Локальное изображение и подпись из одного неизменяемого портрета."""

from dataclasses import dataclass
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
# Кремовый, красный и тёмные тона повторяют логотип, README и иллюстрацию статистики.
_BG, _INK, _MUTED = "#FBF0E0", "#101817", "#6A5C50"
_RED, _BLUE, _LINE = "#B72518", "#354D60", "#D8C8B4"
_PAPER, _SHADOW, _SAND = "#FFF9EF", "#E8DCC9", "#B39B7C"
_SLATE = "#849B9F"
_RED_TINT, _BLUE_TINT = "#F7E3D8", "#E8EBEA"


class TasteRenderError(ValueError):
    """Локальный макет или ресурсы не позволяют безопасно сформировать фото."""


@dataclass(frozen=True)
class RenderedTasteCard:
    """Замороженная пара фото/подпись до первой попытки Telegram."""

    image: bytes
    caption: str
    width: int
    height: int
    filename: str = "taste-card.jpg"


def _number(value: Fraction | int, *, signed: bool = False) -> str:
    """Округлить только отображение, сохранив простые русские числа."""
    return format(float(value), "+.1f" if signed else ".1f").replace(".", ",").replace("-", "−")


def _percent(value: Fraction) -> str:
    """Процент показывается рядом с собственной раскрытой внизу выборкой."""
    if 0 < value < Fraction(1, 100):
        return "менее 1%"
    return f"{float(value) * 100:.0f}%"


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


def caption_for(card: TasteCard) -> str:
    """HTML собирается только из собственных тегов и экранированных полей."""
    examples = card.examples
    if not examples:
        return ""
    result = "Тайтлы из карточки:"
    used = _utf16(result)
    for index, title in enumerate(examples, 1):
        suffix = " · брошено" if title.status == "dropped" else ""
        remaining = len(examples) - index + 1
        budget = min(90, (CAPTION_LIMIT - used) // remaining - _utf16(f"\n{index}. {suffix}"))
        label = _short(title.title, max(8, budget))
        line = f"\n{index}. {label}{suffix}"
        result += f'\n{index}. <a href="{escape(title.url, quote=True)}">{escape(label)}</a>{suffix}'
        used += _utf16(line)
    if used > CAPTION_LIMIT:
        raise TasteRenderError("caption_limit")
    return result


def _footer_notes(card: TasteCard) -> tuple[str, ...]:
    """Сохранить базы блоков, раскрывая только действительно неполные метаданные."""
    prefs, drop, source = card.preferences, card.dropping, card.source
    total = len(source.titles)
    selection = f"Выборка: {total} тайтлов без запланированного"
    selection += " — все оценены." if card.rated == total else f"; оценено {card.rated}."
    notes = [selection]
    if card.rated:
        scope = (
            "Сравнение и разнообразие — по оценённым."
            if card.paired == card.rated else
            f"Разнообразие — по оценённым. Сравнение с Shikimori: {card.paired}/{card.rated}."
        )
        groups = [
            f"{item.group.label} — {len(item.group.titles)}"
            for item in (prefs.higher, prefs.lower) if item
        ]
        if groups:
            scope += " Группы оценок: " + "; ".join(groups) + "."
        notes.append(scope)
    labels = ("жанры", "темы", "форматы")
    if card.rated:
        gaps = [
            f"{label} {count}/{card.rated}"
            for label, count in (*zip(labels, prefs.coverage), ("годы", len(card.diversity.years)))
            if count < card.rated
        ]
        if gaps:
            notes.append("Неполные метаданные оценённых: " + "; ".join(gaps) + ".")
    if drop.total:
        outcomes = f"Доля отказов среди завершённых и брошенных: {drop.dropped}/{drop.total}"
        if drop.pattern:
            pattern = drop.pattern
            outcomes += f"; {pattern.group.label}: {pattern.dropped}/{len(pattern.group.titles)}"
        notes.append(outcomes + ".")
        gaps = [
            f"{label} {count}/{drop.total}"
            for label, count in zip(labels, drop.coverage) if count < drop.total
        ]
        if gaps:
            notes.append("Неполные метаданные завершённых и брошенных: " + "; ".join(gaps) + ".")
    else:
        notes.append("Для доли отказов пока нет конечных исходов.")
    issues = []
    if source.unresolved:
        issues.append(f"тип не определён у {source.unresolved}")
    if source.excluded:
        issues.append(f"непригодных записей {source.excluded}")
    if issues:
        notes.append("В исходном списке: " + "; ".join(issues) + ".")
    synced = source.synced_at.strftime("%d.%m.%Y %H:%M") if source.synced_at else "дата неизвестна"
    notes.extend((
        f"Список проверен: {synced}. Создано: {source.generated_at:%d.%m.%Y %H:%M} (UTC).",
        "Рейтинги Shikimori могут быть старше списка; удалённые тайтлы не учтены.",
    ))
    return tuple(notes)


class _Canvas:
    """Ограниченный холст; подписи измеряются тем же шрифтом, которым рисуются."""

    def __init__(self, resource_root: Path):
        self.scale = 2
        self.image = Image.new("RGB", (WIDTH * 2, MAX_HEIGHT * 2), _BG)
        self.draw = ImageDraw.Draw(self.image)
        self.font_path = resource_root / "assets" / "taste-card" / FONT_NAME
        self.fonts = {}

    def font(self, size):
        if size not in self.fonts:
            self.fonts[size] = ImageFont.truetype(
                str(self.font_path), size * 2, layout_engine=ImageFont.Layout.BASIC,
            )
        return self.fonts[size]

    def measure(self, text, size):
        return self.draw.textlength(text, font=self.font(size)) / 2

    def fit(self, text, size, width):
        if self.measure(text, size) <= width:
            return text
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self.measure(text[:middle].rstrip() + "…", size) <= width:
                low = middle
            else:
                high = middle - 1
        return text[:low].rstrip() + "…"

    def wrap(self, text, size, width, lines=2):
        rest = " ".join(text.split())
        result = []
        while rest and len(result) < lines:
            if self.measure(rest, size) <= width:
                result.append(rest)
                rest = ""
                break
            if len(result) == lines - 1:
                result.append(self.fit(rest, size, width))
                rest = ""
                break
            low, high = 1, len(rest)
            while low < high:
                middle = (low + high + 1) // 2
                if self.measure(rest[:middle], size) <= width:
                    low = middle
                else:
                    high = middle - 1
            cut = rest.rfind(" ", 0, low + 1)
            cut = cut if cut > 0 else low
            result.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip()
        return result or [""]

    def text(self, x, y, text, size=30, color=_INK, *, bold=False, width=None):
        if width is not None:
            text = self.fit(text, size, width)
        self.draw.text(
            (x * 2, y * 2), text, font=self.font(size), fill=color, anchor="lt",
            stroke_width=1 if bold else 0, stroke_fill=color,
        )

    def multiline(self, x, y, text, size=30, color=_INK, *, width=1024, lines=2, bold=False):
        for line in self.wrap(text, size, width, lines):
            self.text(x, y, line, size, color, bold=bold)
            y += size + 9
        return y

    def box(self, x, y, width, height, color, radius=18, outline=None):
        self.draw.rounded_rectangle(
            (x * 2, y * 2, (x + width) * 2, (y + height) * 2),
            radius=radius * 2, fill=color, outline=outline, width=2,
        )

    def section(self, y, height, heading):
        self.box(56, y + 3, 1088, height, _SHADOW, 24)
        self.box(56, y, 1088, height, _PAPER, 24, _LINE)
        self.text(88, y + 27, heading, 37, bold=True)

    def example(self, y, title, *, prefix="Например: "):
        # Существенный статус ставится до сокращаемого названия.
        label = ("брошено · " if title.status == "dropped" else "") + title.title
        self.text(91, y, prefix + label, 27, _MUTED, width=1018)

    def delta(self, y, value):
        """Одна плашка разницы для личных сравнений и предпочтений."""
        color, background = (_BLUE, _BLUE_TINT) if value > 0 else (_RED, _RED_TINT)
        x, top, width, height = 944, y + 6, 164, 76
        self.box(x, top, width, height, background)
        label, font = _number(value, signed=True), self.font(39)
        left, upper, right, lower = self.draw.textbbox(
            (0, 0), label, font=font, anchor="lt", stroke_width=1,
        )
        # Учитываем реальные границы знака, цифр и обводки в масштабе холста.
        self.draw.text(
            ((x + width / 2) * 2 - (left + right) / 2,
             (top + height / 2) * 2 - (upper + lower) / 2),
            label, font=font, fill=color, anchor="lt", stroke_width=1, stroke_fill=color,
        )

    def comparison(self, y, card):
        rows = []
        for item in (card.above, card.below):
            if item:
                label = ("Брошено · " if item.title.status == "dropped" else "") + item.title.title
                row_height = len(self.wrap(label, 32, 805, lines=2)) * 41 + 55
                rows.append((item, label, row_height))
        height = 87 + sum(row_height for _, _, row_height in rows) if rows else 210
        self.section(y, height, "Где оценки расходятся")
        if not rows:
            message = (
                "Заметных расхождений не видно" if card.comparison_state == "neutral"
                else "Нет сохранённых пар оценок" if card.comparison_state == "unavailable"
                else "Для сравнения пока мало данных"
            )
            self.text(91, y + 99, message, 31, _MUTED)
        top = y + 87
        for item, label, row_height in rows:
            title_end = self.multiline(91, top, label, 32, width=805, lines=2, bold=True)
            # Собственные оценки ближе к названию, чем к следующему тайтлу.
            self.text(
                92, title_end + 1,
                f"Профиль {item.title.score}  /  Shikimori {_number(item.title.community)}",
                27, _MUTED,
            )
            self.delta(top, item.delta)
            top += row_height
        return y + height + 24

    def preferences(self, y, card):
        preferences = card.preferences
        rows = [item for item in (preferences.higher, preferences.lower) if item]
        height = 124 + len(rows) * 148 if rows else 235
        self.section(y, height, "Предпочтения")
        if not rows:
            message = (
                "Выраженного предпочтения не видно" if preferences.state == "neutral"
                else "Пока нет личных оценок" if preferences.state == "unavailable"
                else "Для вывода о предпочтениях пока мало данных"
            )
            self.multiline(91, y + 102, message, 31, _MUTED, lines=2)
        if rows:
            self.text(91, y + 78, f"Обычная оценка профиля — {_number(preferences.baseline)}", 26, _MUTED)
        for index, item in enumerate(rows):
            top = y + 124 + index * 148
            if index:
                self.draw.line((91 * 2, (top - 17) * 2, 1108 * 2, (top - 17) * 2), fill=_LINE, width=2)
            self.text(91, top, item.group.label, 32, bold=True, width=805)
            self.text(92, top + 41, f"Средняя оценка — {_number(item.mean)}", 27, _MUTED)
            self.delta(top, item.delta)
            self.example(top + 85, item.example)
        return y + height + 24

    def diversity(self, y, card):
        data = card.diversity
        extra = sum(bool(value) for value in (data.genre, data.theme, data.rare))
        columns = 2 if len(data.formats) > 3 else 3
        legend_extra = 40 if len(data.formats) > 3 else 0
        height = 260 + extra * 40 + legend_extra if card.rated else 180
        self.section(y, height, "Разнообразие")
        top = y + 88
        if not card.rated:
            self.text(91, top + 15, "Пока нет оценённых тайтлов", 31, _MUTED)
            return y + height + 24
        if data.years:
            span = str(data.years[0]) if data.years[0] == data.years[-1] else f"{data.years[0]}–{data.years[-1]}"
            self.text(91, top, span, 40, bold=True)
            count = len(data.decades)
            word = russian_count_word(count, "десятилетие", "десятилетия", "десятилетий")
            label = f"годы выпуска · {count} {word}"
            self.text(91 + self.measure(span, 40) + 26, top + 12, label, 24, _MUTED)
        else:
            self.text(91, top + 8, "Годы выпуска неизвестны", 31, _MUTED)
        formats = list(data.formats)
        x, colors = 91, (_BLUE, _SAND, _MUTED, _SLATE)
        for index, (_, count) in enumerate(formats):
            segment = round(1018 * count / card.rated)
            self.box(x, top + 72, segment, 25, colors[index], radius=3)
            x += segment
        for index, (label, count) in enumerate(formats):
            x = 91 + index % columns * (546 if columns == 2 else 345)
            legend_y = top + 110 + index // columns * 40
            self.box(x, legend_y + 6, 14, 14, colors[index], radius=3)
            self.text(
                x + 24, legend_y,
                f"{label} {_percent(Fraction(count, card.rated))}", 25,
                width=446 if columns == 2 else 300,
            )
        cursor = top + 154 + legend_extra
        for value in (data.genre, data.theme):
            if value:
                self.text(91, cursor, "Часто встречается: " + value[0], 27, width=1018)
                cursor += 40
        if data.rare:
            self.example(cursor, data.rare, prefix=f"Редкость ({data.rare_label}): ")
        return y + height + 24

    def dropping(self, y, card):
        data = card.dropping
        height = 295 if data.pattern else 250
        self.section(y, height, "Что чаще бросается")
        top = y + 86
        if not data.total:
            self.multiline(91, top + 14, "Пока нет завершённых и брошенных тайтлов", 31, _MUTED)
        else:
            pattern = data.pattern
            for x, label, rate, color in (
                (91, pattern.group.label if pattern else "В целом",
                 Fraction(pattern.dropped, len(pattern.group.titles)) if pattern else data.rate,
                 _RED if pattern else _MUTED),
                (637, "В целом", data.rate, _MUTED),
            )[:2 if pattern else 1]:
                self.text(x, top, label, 27, color, bold=True, width=470)
                self.text(x, top + 40, _percent(rate), 52, color, bold=True)
                self.box(x, top + 111, 470, 12, _SHADOW, radius=6)
                if rate:
                    self.box(x, top + 111, max(1, round(470 * rate)), 12, color, radius=6)
            if pattern:
                self.example(top + 150, pattern.example)
            else:
                message = (
                    "Повышенная доля по направлениям не выделяется" if data.state == "neutral"
                    else "Для вывода по направлениям пока мало данных"
                )
                self.text(91, top + 139, message, 27, _MUTED, width=1018)
        return y + height + 24

    def footer(self, y, card):
        self.draw.line((66 * 2, y * 2, 1134 * 2, y * 2), fill=_LINE, width=2)
        cursor = y + 18
        for sample in _footer_notes(card):
            # Числа и существенный контекст не обрезаются; слишком большой макет отклоняется.
            lines = self.wrap(sample, 21, 1068, lines=8)
            if any(line.endswith("…") for line in lines):
                raise TasteRenderError("footer_overflow")
            for line in lines:
                self.text(67, cursor, line, 21, _MUTED)
                cursor += 28
        return cursor + 25


def render_taste_card(card: TasteCard, *, resource_root: Path = RESOURCE_ROOT) -> RenderedTasteCard:
    """Сформировать ограниченный JPEG и подпись без сети и записи состояния."""
    if not card.available:
        raise TasteRenderError("unavailable_card")
    try:
        canvas = _Canvas(resource_root)
        canvas.text(64, 45, "Карточка вкуса", 70, bold=True)
        profile = f"{card.source.profile} / {CATEGORY_LABELS[card.source.category]}"
        end = canvas.multiline(68, 142, profile, 32, _BLUE, width=1060, lines=2, bold=True)
        y = end + 34
        for block in (canvas.comparison, canvas.preferences, canvas.diversity, canvas.dropping):
            y = block(y, card)
        height = canvas.footer(y + 8, card)
        if height > MAX_HEIGHT:
            raise TasteRenderError("image_height")
        image = canvas.image.crop((0, 0, WIDTH * 2, height * 2)).resize(
            (WIDTH, height), Image.Resampling.LANCZOS,
        )
        content = b""
        for quality in (92, 85, 78):
            output = BytesIO()
            image.save(output, format="JPEG", quality=quality, subsampling=0)
            content = output.getvalue()
            if len(content) <= MAX_BYTES:
                break
        if len(content) > MAX_BYTES:
            raise TasteRenderError("image_size")
        return RenderedTasteCard(content, caption_for(card), WIDTH, height)
    except (OSError, ValueError, OverflowError) as error:
        if isinstance(error, TasteRenderError):
            raise
        raise TasteRenderError("local_render") from error


def smoke_card(category: str = "anime") -> TasteCard:
    """Условный срез для офлайн-проверки реальных ресурсов и frozen-рендера."""
    if category not in CATEGORY_LABELS:
        raise ValueError("taste_category")
    titles = []
    media = "animes" if category == "anime" else "mangas"
    for index in range(1, 61):
        genre = "Детектив" if index <= 10 else "Романтика" if index <= 20 else "Фэнтези"
        score = 9 if index <= 10 else 5 if index <= 20 else 7
        kind = ("movie" if index <= 10 else "ova" if index > 58 else "tv") if category == "anime" else (
            "manga" if category == "manga" else "light_novel"
        )
        name = {1: "Небо и море / 空と海", 11: "Стеклянный город", 59: "Письма на север"}.get(index, f"История {index}")
        titles.append(TasteTitle(
            index, name, f"https://shikimori.io/{media}/{index}",
            "dropped" if index <= 4 or index in {21, 22} else "completed",
            kind, score, Fraction("6.6") if index <= 10 else Fraction("8.4"),
            1980 + index % 46, (genre,), ("Путешествие",),
        ))
    now = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    return build_taste_card(TasteInput("DEMO · условные данные", category, now, now, tuple(titles)))
