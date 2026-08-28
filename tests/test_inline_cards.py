# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Иерархия, безопасность и Telegram-лимиты inline-карточек."""

import re

import pytest
from aiogram.enums import ParseMode
from aiogram.types import (
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
)

import inline_cards


def _anime_item(**overrides):
    item = {
        "id": "33",
        "url": "https://shikimori.io/animes/33-kenpuu-denki-berserk",
        "name": "Kenpuu Denki Berserk",
        "russian": "Берсерк & <Гатс>",
        "japanese": "剣風伝奇<ベルセルク>",
        "kind": "tv",
        "score": 8.61,
        "episodes": 25,
        "duration": 23,
        "airedOn": {"year": 1997},
        "studios": [{"name": "OLM & Co"}],
        "genres": [
            {"russian": "Сэйнэн", "name": "Seinen", "kind": "demographic"},
            {"russian": "Экшен", "name": "Action", "kind": "genre"},
            {"russian": "", "name": "Gore", "kind": "theme"},
        ],
        "poster": {
            "originalUrl": "https://shikimori.io/uploads/poster/animes/33/poster.jpg",
            "mainUrl": "https://shikimori.io/uploads/poster/animes/33/main.webp",
        },
        "description": "[b]Гатс[/b] <i>идёт</i> против A&B [Гриффит].",
    }
    item.update(overrides)
    return item


def test_anime_caption_normalizes_url_escapes_api_text_and_groups_taxonomy():
    caption = inline_cards.build_card_caption("anime", _anime_item())

    assert caption.startswith("🎬 ")
    assert caption.count("https://shikimori.io") == 1
    assert "Берсерк &amp; &lt;Гатс&gt;" in caption
    assert "<i>剣風伝奇&lt;ベルセルク&gt;</i>" in caption
    assert "<b>TV-сериал · 1997</b>" in caption
    assert "⭐ 8.61 · 25 эп. · 23 мин." in caption
    assert "<blockquote><b>TV-сериал · 1997</b>\n⭐ 8.61" in caption
    assert "<br>" not in caption
    assert "🎞 OLM &amp; Co" in caption
    assert "👥 <b>Демография:</b> Сэйнэн" in caption
    assert "🎭 <b>Жанры:</b> Экшен" in caption
    assert "🏷 <b>Темы:</b> Gore" in caption
    assert "[b]" not in caption and "<i>идёт</i>" not in caption
    assert "Гатс идёт против A&amp;B [Гриффит]." in caption
    assert "<tg-spoiler>" in caption


def test_photo_result_uses_poster_and_stable_domain_specific_id():
    anime = inline_cards.build_inline_result("anime", _anime_item())
    manga = inline_cards.build_inline_result("manga", _anime_item())

    assert isinstance(anime, InlineQueryResultPhoto)
    assert anime.id == "anime:33"
    assert manga.id == "manga:33"
    assert anime.photo_url.endswith("poster.jpg")
    assert anime.thumbnail_url.endswith("main.webp")
    assert anime.parse_mode == ParseMode.HTML


def test_missing_or_invalid_poster_falls_back_to_article_with_same_caption():
    missing = inline_cards.build_inline_result("manga", _anime_item(poster=None))
    invalid = inline_cards.build_inline_result(
        "ranobe",
        _anime_item(poster={"originalUrl": "javascript:alert(1)"}),
    )

    assert isinstance(missing, InlineQueryResultArticle)
    assert isinstance(invalid, InlineQueryResultArticle)
    assert missing.input_message_content.message_text.startswith("📚 ")
    assert invalid.input_message_content.message_text.startswith("📖 ")
    assert missing.input_message_content.parse_mode == ParseMode.HTML


def test_manga_and_ranobe_facts_use_chapters_volumes_and_publishers():
    item = _anime_item(
        kind="light_novel",
        episodes=None,
        duration=None,
        chapters=42,
        volumes=7,
        studios=None,
        publishers=[{"name": "Kadokawa"}],
    )

    caption = inline_cards.build_card_caption("ranobe", item)

    assert caption.startswith("📖 ")
    assert "<b>Ранобэ · 1997</b>" in caption
    assert "42 гл. · 7 т." in caption
    assert "🏢 Kadokawa" in caption
    assert "эп." not in caption and "мин." not in caption


def test_missing_optional_fields_remove_whole_sections_without_placeholders():
    item = {
        "id": "1",
        "url": "/mangas/1",
        "name": "Only title",
        "poster": None,
    }

    caption = inline_cards.build_card_caption("manga", item)

    assert caption == (
        '📚 <b><a href="https://shikimori.io/mangas/1">Only title</a></b>'
    )
    assert "None" not in caption
    assert "Описание" not in caption


def test_dense_caption_truncates_description_then_whole_taxonomy_items():
    long_items = [
        {
            "russian": f"Очень длинный жанр номер {index} " + "слово " * 20,
            "name": "fallback",
            "kind": "genre",
        }
        for index in range(12)
    ]
    item = _anime_item(
        description=" ".join(
            f"описание{index}&деталь"
            for index in range(400)
        ),
        genres=long_items,
    )

    caption = inline_cards.build_card_caption("anime", item)

    assert inline_cards.parsed_caption_length(caption) <= 1024
    assert "… ещё " in caption
    assert caption.count("<blockquote>") == caption.count("</blockquote>")
    assert caption.count("<tg-spoiler>") == caption.count("</tg-spoiler>")
    assert "&amp;" in caption
    assert re.search(r"&amp(?!;)", caption) is None


def test_cleaner_removes_known_markup_but_keeps_plain_bracketed_names():
    assert inline_cards.clean_shikimori_description(
        "[url=https://example.test][character=1]Гатс[/character][/url] [ガッツ]"
    ) == "Гатс [ガッツ]"


def test_missing_result_id_is_rejected_instead_of_creating_collisions():
    with pytest.raises(ValueError, match="id"):
        inline_cards.build_inline_result("anime", _anime_item(id=None))
