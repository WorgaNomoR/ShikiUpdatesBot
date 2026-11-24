# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Реальный рендер, измеренные границы текста и атрибуция карточки."""

from dataclasses import replace
from fractions import Fraction
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
from shutil import copyfile

import pytest
from PIL import (
    Image,
    ImageDraw,
)

from runtime import RESOURCE_ROOT
from taste_card import build_taste_card
from taste_card_render import (
    CAPTION_LIMIT,
    MAX_BYTES,
    MAX_HEIGHT,
    PROJECT_URL,
    TasteRenderError,
    _Canvas,
    _footer_notes,
    render_taste_card,
    smoke_card,
)


class _Caption(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.links = []
        self.feed(markup)

    def handle_data(self, data):
        self.text += data

    def handle_starttag(self, tag, attrs):
        assert tag == "a"
        self.links.append(dict(attrs)["href"])


@pytest.fixture
def drawing(monkeypatch):
    captured = {}
    original_text = ImageDraw.ImageDraw.text
    original_panel = _Canvas.panel

    def record_text(draw, xy, text, *args, **kwargs):
        if draw._image.width > 1:
            captured.setdefault("text", []).append((text, draw.textbbox(
                xy, text, font=kwargs["font"], anchor=kwargs["anchor"], stroke_width=kwargs["stroke_width"],
            )))
        return original_text(draw, xy, text, *args, **kwargs)

    def record_panel(canvas, y, end, *args, **kwargs):
        if canvas.image.width > 1:
            captured.setdefault("panels", []).append((2 * y, 2 * end))
        return original_panel(canvas, y, end, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
    monkeypatch.setattr(_Canvas, "panel", record_panel)
    return captured


@pytest.mark.parametrize("category", ["anime", "manga", "ranobe"])
def test_real_jpeg_fonts_caption_and_dense_category_smoke(category):
    card = smoke_card(category)
    rendered = render_taste_card(card)
    with Image.open(BytesIO(rendered.image)) as image:
        image.load()
        assert image.format == "JPEG" and image.mode == "RGB"
        assert image.size == (1200, rendered.height)
    assert rendered.height <= MAX_HEIGHT and len(rendered.image) <= MAX_BYTES
    assert rendered.width + rendered.height <= 10000
    caption = _Caption(rendered.caption)
    assert len(caption.text.encode("utf-16-le")) // 2 <= CAPTION_LIMIT
    assert caption.text.startswith("Тайтлы из карточки:\n1. ")
    assert card.source.profile not in caption.text
    assert caption.links == [card.above.title.url, card.below.title.url, card.dropping.pattern.example.url, PROJECT_URL]
    assert "眠らない街" in caption.text and "· брошено" in caption.text


def test_planned_only_card_keeps_overview_and_project_link(drawing):
    source = smoke_card().source
    source = replace(source, titles=(replace(
        source.titles[0], status="planned", score=10, kind="tv", community=None,
    ),))
    rendered = render_taste_card(build_taste_card(source))
    assert rendered.height < 1300
    caption = _Caption(rendered.caption)
    assert caption.links == [PROJECT_URL]
    words = {text for text, _ in drawing["text"]}
    assert {"Все тайтлы · сериалы", "В планах 1", "Пока нет личных оценок", "—"} <= words
    assert "Доля брошенных" not in words


def test_extreme_titles_and_labels_fit_measured_bounds_and_safe_caption(drawing):
    source = smoke_card().source
    long_title = "<b>НЕ РАЗМЕТКА</b> &lt; 😀 日本語 " + "Оченьдлинноеназвание" * 120
    long_genre = "Триллер" + "曲" * 120
    source = replace(source, profile="<Профиль> & " + "Я" * 110, titles=tuple(replace(
        item, title=long_title,
        genres=tuple(long_genre if tag == "Триллер" else tag for tag in item.genres),
    ) for item in source.titles))
    rendered = render_taste_card(build_taste_card(source))
    caption = _Caption(rendered.caption)
    assert rendered.height <= MAX_HEIGHT
    assert len(caption.text.encode("utf-16-le")) // 2 <= CAPTION_LIMIT
    assert "<b>НЕ РАЗМЕТКА</b> &lt;" in caption.text
    assert all(
        left >= 0 and top >= 0 and right <= rendered.width * 2 and bottom <= rendered.height * 2
        for _, (left, top, right, bottom) in drawing["text"]
    )
    assert any(text.endswith("…") for text, _ in drawing["text"])


def test_comparison_legend_centres_head_clearance_and_long_title_padding(drawing):
    render_taste_card(smoke_card())
    entries = drawing["text"]
    heading = next(box for text, box in entries if text == "Расхождения оценок по тайтлам")
    profile = next(box for text, box in entries if text == "Профиль")
    shikimori = next(box for text, box in entries if text == "Shikimori")
    assert abs(sum(profile[1::2]) - sum(shikimori[1::2])) <= 2
    assert heading[2] < profile[0] and profile[2] < shikimori[0]
    row = next(box for text, box in entries if "Небо над городом" in text)
    assert row[1] - max(heading[3], profile[3], shikimori[3]) >= 26 * 2
    continuation = next(box for text, box in entries if "街の空" in text)
    next_title = next(box for text, box in entries if "Тетрадь странствий" in text)
    assert continuation[1] > row[3]
    assert next_title[1] - continuation[3] >= 10 * 2


def test_dropping_explanation_and_example_have_real_padding(drawing):
    render_taste_card(smoke_card())
    entries = drawing["text"]
    heading = next(box for text, box in entries if text == "Что доводилось бросать чаще")
    panel_end = next(end for start, end in drawing["panels"] if start <= heading[1] < end)
    explanation = next(box for text, box in entries if text == "Среди завершённых и брошенных тайтлов")
    first = next(box for text, box in entries if text == "10 из 20")
    example = next(box for text, box in entries if text.startswith("Пример · Хроники"))
    assert first[1] >= explanation[3] + 32 * 2
    assert panel_end - example[3] >= 19 * 2


def test_degradation_keeps_both_comparisons_and_links_only_shown_examples(drawing, monkeypatch):
    card = smoke_card()
    monkeypatch.setattr("taste_card_render.MAX_HEIGHT", 2330)
    rendered = render_taste_card(card)
    links = _Caption(rendered.caption).links
    assert rendered.height <= 2330
    assert links[:2] == [card.above.title.url, card.below.title.url]
    assert (card.dropping.pattern.example.url in links) == any(
        text.startswith("Пример ·") for text, _ in drawing["text"]
    )
    assert links[-1] == PROJECT_URL


def test_unreadable_font_is_a_typed_local_error(monkeypatch):
    def unreadable_font(*args, **kwargs):
        raise OSError("font")

    monkeypatch.setattr("PIL.ImageFont.truetype", unreadable_font)
    with pytest.raises(TasteRenderError, match="local_render"):
        render_taste_card(smoke_card())


def test_missing_packaged_font_in_requested_resource_root_is_a_typed_local_error(tmp_path):
    assets = tmp_path / "assets" / "taste-card"
    assets.mkdir(parents=True)
    for name in ("repository-qr.png", "logo.png"):
        copyfile(RESOURCE_ROOT / "assets" / "taste-card" / name, assets / name)
    with pytest.raises(TasteRenderError, match="local_render"):
        render_taste_card(smoke_card(), resource_root=tmp_path)


def test_transparent_or_invalid_avatar_uses_logo_and_partial_alpha_is_composited():
    card = smoke_card()

    def png(color):
        output = BytesIO()
        Image.new("RGBA", (160, 160), color).save(output, format="PNG")
        return output.getvalue()

    baseline = render_taste_card(card).image
    assert render_taste_card(card, avatar=png((0, 0, 0, 0))).image == baseline
    assert render_taste_card(card, avatar=b"bad image").image == baseline
    translucent = render_taste_card(card, avatar=png((100, 20, 40, 128))).image
    with Image.open(BytesIO(translucent)) as image:
        rgb = image.getpixel((1063, 120))
    assert all(abs(channel - expected) < 5 for channel, expected in zip(rgb, (177, 137, 147)))


def test_qr_keeps_verified_matrix_and_clear_cream_quiet_zone(drawing):
    path = RESOURCE_ROOT / "assets" / "taste-card" / "repository-qr.png"
    with Image.open(path) as image:
        alpha = image.convert("RGBA").getchannel("A")
    # Матрица QR, декодированная в PROJECT_URL при проверке оригинала и JPEG.
    assert sha256(alpha.tobytes()).hexdigest() == "985ecdf1331defa2313e5efd75519d5e327f0f42f0b537ca2c7034be77024db6"
    rendered = render_taste_card(smoke_card())
    with Image.open(BytesIO(rendered.image)) as image:
        x, y = 1148 - 222, rendered.height - 16 - 246
        crop = image.crop((x, y, x + 246, y + 246)).convert("L")
        dark = crop.point(lambda n: 255 if n < 100 else 0)
        assert dark.getbbox() == alpha.getbbox() == (24, 24, 222, 222)
        # Ближайший пиксель при уменьшении в 6 раз проверяет центр каждого модуля.
        modules = (41, 41)
        assert dark.resize(modules, Image.Resampling.NEAREST).tobytes() == (
            alpha.resize(modules, Image.Resampling.NEAREST).tobytes()
        )
        assert image.getpixel((x + 10, y + 40)) == image.getpixel((x - 10, y + 40))
    assert {"Made by ShikiUpdatesBot", PROJECT_URL.removeprefix("https://")} <= {text for text, _ in drawing["text"]}


def test_unrated_outcomes_keep_actual_drop_pattern(drawing):
    source = smoke_card().source
    source = replace(source, titles=tuple(replace(title, score=None, community=None) for title in source.titles))
    rendered = render_taste_card(build_taste_card(source))
    assert rendered.height < MAX_HEIGHT
    words = {text for text, _ in drawing["text"]}
    assert {"Пока нет личных оценок", "10 из 20", "20 из 120", "50%"} <= words


def test_four_format_legend_names_and_shares_stay_inside_panel(drawing):
    source = smoke_card().source
    kinds = ("tv",) * 104 + ("movie",) * 32 + ("ova",) * 16 + ("ona",) * 8
    titles = tuple(replace(title, kind=kind) for title, kind in zip(source.titles, kinds, strict=True))
    render_taste_card(build_taste_card(replace(source, titles=titles)))
    expected = {"Сериалы": "65%", "Фильмы": "20%", "OVA": "10%", "ONA": "5%"}
    rows = {text: box for text, box in drawing["text"] if text in expected}
    assert rows.keys() == expected.keys()
    for name, label_box in rows.items():
        [(share, share_box)] = [
            (text, box) for text, box in drawing["text"]
            if text.endswith("%") and box[1] == label_box[1] and label_box[2] < box[0] < 580 * 2
        ]
        assert share == expected[name]
        assert share_box[2] < 580 * 2 and share_box[3] < (386 + 276) * 2


def test_neutral_ratings_and_outcomes_fit_compact_sections(drawing):
    source = smoke_card().source
    source = replace(source, titles=tuple(replace(title, score=7, status="completed", community=Fraction(7))
                                         for title in source.titles))
    rendered = render_taste_card(build_taste_card(source))
    assert rendered.height < 2080
    words = " ".join(text for text, _ in drawing["text"])
    assert "Заметных расхождений нет" in words
    assert "Заметной разницы между группами нет" in words
    assert "Направление не выделяется" in words


def test_footer_reports_partial_coverage_without_using_the_global_cache_date():
    source = smoke_card().source
    source = replace(source, synced_at=None, excluded=1, titles=tuple(replace(
        title, genres=() if 11 <= title.id <= 20 else title.genres,
        themes=() if title.id <= 15 else title.themes,
        year=None if 11 <= title.id <= 16 else title.year,
    ) for title in source.titles))
    notes = "\n".join(_footer_notes(build_taste_card(source)))
    assert "Оценено 112. Рейтинг Shikimori — у 94 из них." in notes
    assert "жанры 102/112 · темы 97/112 · годы 106/112" in notes
    assert "жанры 110/120 · темы 105/120" in notes
    assert "непригодных записей 1" in notes and "Дата списка неизвестна" in notes
    assert "тип не определён у 0" not in notes and "UTC" in notes
