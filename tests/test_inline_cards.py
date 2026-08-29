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
        "status": "ongoing",
        "origin": "Манга",
        "rating": "R-17",
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
    assert "<blockquote><b>TV-сериал · 1997</b>\n⭐ Оценка: 8.61" in caption
    assert "⏱ 25 эп. · 23 мин." in caption
    assert "📖 Первоисточник: Манга" in caption
    assert "🔖 Возрастной рейтинг: R-17" in caption
    assert "🟢 Онгоинг" in caption
    assert "<br>" not in caption
    assert "🎞 Студия: OLM &amp; Co" in caption
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


def test_result_keyboard_opens_current_inline_shikimori_and_read_only_info():
    result = inline_cards.build_inline_result(
        "anime",
        _anime_item(),
        bot_username="@WorgaTestBot",
    )

    buttons = result.reply_markup.inline_keyboard[0]
    assert [button.text for button in buttons] == [
        "🔎 Новый поиск",
        "На Shikimori",
        "ℹ️ О боте",
    ]
    assert buttons[0].switch_inline_query_current_chat == ""
    assert buttons[1].url == (
        "https://shikimori.io/animes/33-kenpuu-denki-berserk"
    )
    assert buttons[2].url == "https://t.me/WorgaTestBot?start=info"


def test_invalid_bot_username_omits_only_info_button():
    result = inline_cards.build_inline_result(
        "anime",
        _anime_item(),
        bot_username="bad username",
    )

    buttons = result.reply_markup.inline_keyboard[0]
    assert [button.text for button in buttons] == [
        "🔎 Новый поиск",
        "На Shikimori",
    ]


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


def test_first_page_appends_explicit_project_promo_without_replacing_photos():
    items = [
        _anime_item(id=str(item_id), russian=f"Fate {item_id}")
        for item_id in (1, 2, 3)
    ]
    rendered = [
        inline_cards.build_inline_result(
            "anime",
            item,
            bot_username="WorgaTestBot",
        )
        for item in items
    ]

    results = inline_cards.finalize_inline_results(rendered, page=1)

    assert [type(result) for result in results] == [
        InlineQueryResultPhoto,
        InlineQueryResultPhoto,
        InlineQueryResultPhoto,
        InlineQueryResultArticle,
    ]
    assert [result.id for result in results] == [
        "anime:1",
        "anime:2",
        "anime:3",
        "project:share",
    ]
    promo = results[-1]
    assert promo.title == "📣 Поделиться ShikiUpdatesBot"
    assert promo.description == (
        "Отправит в чат сообщение о боте и ссылку на GitHub"
    )
    assert "ShikiUpdatesBot: активность и поиск на Shikimori" in (
        promo.input_message_content.message_text
    )
    assert promo.input_message_content.message_text.count(
        "https://github.com/WorgaNomoR/ShikiUpdatesBot"
    ) == 2
    assert promo.reply_markup.inline_keyboard[0][0].text == "GitHub и установка"
    assert promo.reply_markup.inline_keyboard[0][0].url == (
        "https://github.com/WorgaNomoR/ShikiUpdatesBot"
    )


@pytest.mark.parametrize(
    ("rendered", "page"),
    [
        ([], 1),
        ([inline_cards.build_inline_result("anime", _anime_item())], 2),
    ],
)
def test_empty_or_continuation_page_does_not_append_project_promo(
    rendered,
    page,
):
    assert inline_cards.finalize_inline_results(rendered, page=page) == rendered


def test_natural_article_fallback_keeps_other_photo_results_before_promo():
    items = [
        _anime_item(id="1"),
        _anime_item(id="2", poster=None),
        _anime_item(id="3"),
    ]
    rendered = [inline_cards.build_inline_result("anime", item) for item in items]

    results = inline_cards.finalize_inline_results(rendered, page=1)

    assert results[:-1] == rendered
    assert [type(result) for result in results] == [
        InlineQueryResultPhoto,
        InlineQueryResultArticle,
        InlineQueryResultPhoto,
        InlineQueryResultArticle,
    ]


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
    assert "🏢 Издатель: Kadokawa" in caption
    assert "эп." not in caption and "мин." not in caption


def test_studio_and_publisher_labels_become_plural_for_multiple_names():
    anime_caption = inline_cards.build_card_caption(
        "anime",
        _anime_item(studios=[{"name": "OLM"}, {"name": "Sunrise"}]),
    )
    manga_caption = inline_cards.build_card_caption(
        "manga",
        _anime_item(
            studios=None,
            publishers=[{"name": "Kadokawa"}, {"name": "Shueisha"}],
        ),
    )

    assert "🎞 Студии: OLM · Sunrise" in anime_caption
    assert "🏢 Издатели: Kadokawa · Shueisha" in manga_caption


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


def test_parsed_caption_length_uses_telegram_utf16_units():
    assert inline_cards.parsed_caption_length("A&amp;B 🎬") == 6


def test_cleaner_removes_known_markup_but_keeps_plain_bracketed_names():
    assert inline_cards.clean_shikimori_description(
        "[url=https://example.test][character=1]Гатс[/character][/url] [ガッツ]"
    ) == "Гатс [ガッツ]"


def test_missing_result_id_is_rejected_instead_of_creating_collisions():
    with pytest.raises(ValueError, match="id"):
        inline_cards.build_inline_result("anime", _anime_item(id=None))
