# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Реальный локальный рендер, ограничения картинки и безопасные ссылки."""

from dataclasses import replace
from fractions import Fraction
from html.parser import HTMLParser
from io import BytesIO

import pytest
from PIL import (
    Image,
    ImageDraw,
)

from taste_card import build_taste_card
from taste_card_render import (
    CAPTION_LIMIT,
    MAX_BYTES,
    MAX_HEIGHT,
    TasteRenderError,
    _footer_notes,
    caption_for,
    render_taste_card,
    smoke_card,
)


class _Caption(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.links = []

    def handle_data(self, data):
        self.text += data

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "a"}
        if tag == "a":
            self.links.append(dict(attrs)["href"])


def test_caption_is_only_title_links_without_repeating_image_heading_or_profile():
    card = smoke_card()
    caption = _Caption()
    caption.feed(caption_for(card))
    assert caption.text.startswith("Тайтлы из карточки:\n1. ")
    assert "Карточка вкуса" not in caption.text and card.source.profile not in caption.text
    assert "Небо и море / 空と海 · брошено" in caption.text


def test_caption_is_empty_when_card_has_no_examples():
    source = smoke_card().source
    source = replace(source, titles=(replace(
        source.titles[0], status="completed", score=7, community=Fraction(7),
    ),))
    assert caption_for(build_taste_card(source)) == ""


@pytest.mark.parametrize("category", ["anime", "manga", "ranobe"])
def test_real_jpeg_fonts_cyrillic_cjk_and_category_smoke(category):
    card = smoke_card(category)
    rendered = render_taste_card(card)
    with Image.open(BytesIO(rendered.image)) as image:
        image.load()
        assert image.format == "JPEG" and image.mode == "RGB"
        assert image.size == (1200, rendered.height)
    assert rendered.height <= MAX_HEIGHT
    assert len(rendered.image) <= MAX_BYTES
    assert rendered.width + rendered.height <= 10000
    assert rendered.caption == caption_for(card)
    caption = _Caption()
    caption.feed(rendered.caption)
    assert len(caption.text.encode("utf-16-le")) // 2 <= CAPTION_LIMIT
    assert caption.links == [title.url for title in card.examples]


def test_long_hostile_names_fit_bounded_image_and_caption_without_losing_links(monkeypatch):
    source = smoke_card().source
    titles = []
    for title in source.titles:
        changes = {}
        if title.id == 2:
            changes.update(score=10, community=Fraction(1))
        if title.id == 12:
            changes.update(score=1, community=Fraction(10))
        if 21 <= title.id <= 30:
            changes.update(themes=("Ужасы",), status="dropped" if title.id <= 25 else "completed")
        titles.append(replace(
            title, **changes,
            title="<b>НЕ РАЗМЕТКА</b> & 😀 日本語 " + "Оченьдлинноеназвание" * 120,
        ))
    source = replace(source, profile="<Профиль> & " + "Я" * 110, titles=tuple(titles))
    card = build_taste_card(source)
    assert len(card.examples) == 6
    drawn = []
    original = ImageDraw.ImageDraw.text

    def record(drawing, xy, value, *args, **kwargs):
        drawn.append((value, drawing.textbbox(
            xy, value, font=kwargs["font"], anchor=kwargs.get("anchor"),
            stroke_width=kwargs.get("stroke_width", 0),
        )))
        return original(drawing, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    rendered = render_taste_card(card)
    caption = _Caption()
    caption.feed(rendered.caption)
    assert rendered.height <= MAX_HEIGHT
    assert len(caption.text.encode("utf-16-le")) // 2 <= CAPTION_LIMIT
    assert "<b>НЕ РАЗМЕТКА</b>" in caption.text
    assert caption.links == [title.url for title in card.examples]
    assert len(caption.links) <= 6
    assert sum("брошено" in value.lower() for value, _ in drawn) >= 3
    assert all(
        left >= 0 and top >= 0 and right <= rendered.width * 2 and bottom <= rendered.height * 2
        for _, (left, top, right, bottom) in drawn
    )


@pytest.mark.parametrize("long_title", [False, True])
def test_comparison_ratings_are_closer_to_own_title_than_next_title(monkeypatch, long_title):
    source = smoke_card().source
    suffix = " и путешествие через далёкие города" * 6 if long_title else ""
    source = replace(source, titles=tuple(
        replace(title, title=title.title + suffix) if title.id in {1, 11} else title
        for title in source.titles
    ))
    drawn = []
    original = ImageDraw.ImageDraw.text

    def record(drawing, xy, value, *args, **kwargs):
        drawn.append((value, drawing.textbbox(
            xy, value, font=kwargs["font"], anchor=kwargs.get("anchor"),
            stroke_width=kwargs.get("stroke_width", 0),
        )))
        return original(drawing, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    render_taste_card(build_taste_card(source))
    rating_index = next(index for index, (value, _) in enumerate(drawn) if value.startswith("Профиль "))
    title_bottom = drawn[rating_index - 1][1][3]
    rating_top, rating_bottom = drawn[rating_index][1][1::2]
    next_title_top = next(box[1] for value, box in drawn if value.startswith("Стеклянный город"))
    own_gap = rating_top - title_bottom
    between_groups = next_title_top - rating_bottom
    assert 0 <= own_gap < between_groups
    assert between_groups >= 24 * 2


def test_missing_packaged_font_is_a_typed_local_error(tmp_path):
    with pytest.raises(TasteRenderError, match="local_render"):
        render_taste_card(smoke_card(), resource_root=tmp_path)


def test_partial_unrated_list_retains_actual_drop_fact(monkeypatch):
    source = smoke_card().source
    source = replace(source, titles=tuple(replace(title, score=None, community=None) for title in source.titles))
    card = build_taste_card(source)
    assert card.available and card.rated == 0 and card.dropping.total == 60
    drawn = []
    original = ImageDraw.ImageDraw.text

    def record(drawing, xy, value, *args, **kwargs):
        drawn.append(value)
        return original(drawing, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    rendered = render_taste_card(card)
    assert rendered.height < MAX_HEIGHT and "<a " in rendered.caption
    assert "10%" in drawn
    assert any("6/60" in value for value in drawn)


def test_footer_full_coverage_shares_selection_without_repeated_complete_fractions():
    notes = "\n".join(_footer_notes(smoke_card()))
    assert "60 тайтлов без запланированного — все оценены" in notes
    assert "Сравнение и разнообразие — по оценённым" in notes
    assert "Группы оценок: Детектив — 10; Романтика — 10" in notes
    assert "среди завершённых и брошенных: 6/60; Детектив: 4/10" in notes
    assert "60/60" not in notes and "Неполные метаданные" not in notes
    assert notes.count("UTC") == 1
    assert "Рейтинги Shikimori могут быть старше списка" in notes
    assert "удалённые тайтлы не учтены" in notes


@pytest.mark.parametrize("format_count, expected", [
    (2, {"Сериалы", "Фильмы"}),
    (3, {"Сериалы", "Фильмы", "OVA"}),
    (4, {"Сериалы", "Фильмы", "OVA", "ONA"}),
])
def test_format_legend_names_every_present_format(
    monkeypatch, format_count, expected,
):
    source = smoke_card().source
    kinds = ("tv",) * 30 + ("movie",) * 15 + ("ova",) * 10 + ("ona",) * 5
    allowed = {"tv", "movie", "ova", "ona"} if format_count == 4 else (
        {"tv", "movie", "ova"} if format_count == 3 else {"tv", "movie"}
    )
    source = replace(source, titles=tuple(replace(
        title, kind=kinds[index] if kinds[index] in allowed else "tv",
    ) for index, title in enumerate(source.titles)))
    drawn = []
    measured = []
    original = ImageDraw.ImageDraw.text

    def record(drawing, xy, value, *args, **kwargs):
        drawn.append(value)
        measured.append((value, drawing.textbbox(
            xy, value, font=kwargs["font"], anchor=kwargs.get("anchor"),
            stroke_width=kwargs.get("stroke_width", 0),
        )))
        return original(drawing, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    render_taste_card(build_taste_card(source))
    names = {"Сериалы", "Фильмы", "OVA", "ONA", "Другое", "TV"}
    legend = {value.split()[0] for value in drawn if "%" in value and value.split()[0] in names}
    assert legend == expected
    if format_count == 4:
        assert "OVA 17%" in drawn and "ONA 8%" in drawn
        boxes = {value.split()[0]: box for value, box in measured if "%" in value and value.split()[0] in names}
        assert boxes["Сериалы"][1] == boxes["Фильмы"][1]
        assert boxes["OVA"][1] == boxes["ONA"][1]
        assert boxes["OVA"][1] > max(boxes[name][3] for name in ("Сериалы", "Фильмы"))
        facts_top = min(box[1] for value, box in measured if value.startswith("Часто встречается:"))
        assert max(box[3] for box in boxes.values()) < facts_top


def test_footer_partial_coverage_keeps_rated_and_outcome_denominators_separate():
    source = smoke_card().source
    source = replace(source, titles=tuple(replace(
        title,
        score=None if title.id <= 10 else title.score,
        community=None if 11 <= title.id <= 15 else title.community,
        genres=() if 11 <= title.id <= 20 else title.genres,
        themes=() if title.id <= 15 else title.themes,
        year=None if 11 <= title.id <= 16 else title.year,
    ) for title in source.titles))
    notes = "\n".join(_footer_notes(build_taste_card(source)))
    assert "оценено 50" in notes and "Сравнение с Shikimori: 45/50" in notes
    assert "Неполные метаданные оценённых: жанры 40/50; темы 45/50; годы 44/50" in notes
    assert "Неполные метаданные завершённых и брошенных: жанры 50/60; темы 45/60" in notes
    assert "форматы" not in notes and "тип не определён у 0" not in notes


@pytest.mark.parametrize("unrated", [False, True])
def test_footer_empty_selection_has_no_zero_denominator_or_false_coverage(unrated):
    source = smoke_card().source
    source = replace(source, synced_at=None, titles=tuple(replace(
        title, score=None if unrated else title.score,
        status="completed" if unrated else "watching", genres=(), themes=(), year=None,
    ) for title in source.titles))
    notes = "\n".join(_footer_notes(build_taste_card(source)))
    assert "0/0" not in notes and "дата неизвестна" in notes
    assert ("Неполные метаданные оценённых:" in notes) is not unrated
    assert ("Неполные метаданные завершённых и брошенных:" in notes) is unrated
    if not unrated:
        assert "пока нет конечных исходов" in notes
