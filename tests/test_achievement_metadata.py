# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Подписи любых достижений из карточек/значков; повреждения дают фолбэк."""

import pytest

from achievement_metadata import (
    AchievementMetadata,
    parse_achievement_labels,
    parse_achievement_metadata,
)


def test_profile_card_criterion_is_plain_text_bound_to_exact_tier():
    html = '''<div class="b-achievement level-4" data-title="Беззаботный зритель"
        data-hint="  150  просмотренных аниме &amp; &lt;условие&gt; ">
        <div class="c-about"><a href="/achievements/common/animelist">Зритель</a>
        <div class="text">Ничего <b>серьёзного</b> &amp; опасного. Другое предложение.</div></div>
        <div class="c-image"><a href="/achievements/common/animelist"><img src="unused"></a></div>
        </div><div class="text">Чужое описание</div>
        <a class="b-badge level-0" title="Берсерк" href="/achievements/franchise/berserk"></a>'''
    assert parse_achievement_metadata(html) == {
        ("animelist", 4): AchievementMetadata("Беззаботный зритель", "common", "150 просмотренных аниме & <условие>"),
        ("berserk", 0): AchievementMetadata("Берсерк", "franchise", is_binary=True),
    }
    assert parse_achievement_labels(html)[("animelist", 4)] == ("Беззаботный зритель", "common")


def test_profile_criterion_is_exact_tier_and_does_not_replace_label_contract():
    html = '''<div class="b-achievement level-4" data-title="Беззаботный зритель"
        data-hint="150 просмотренных аниме"><a href="/achievements/common/animelist"></a>
        <div class="text">Ничего серьёзного.</div></div>'''
    metadata = parse_achievement_metadata(html)[("animelist", 4)]
    assert metadata.criterion == "150 просмотренных аниме"
    assert parse_achievement_labels(html) == {("animelist", 4): ("Беззаботный зритель", "common")}


def test_long_or_conflicting_criteria_do_not_invalidate_the_title():
    def card(criterion):
        return f'''<div class="b-achievement level-1" data-title="Название" data-hint="{criterion}">
            <a href="/achievements/common/future"></a></div>'''
    metadata = parse_achievement_metadata(card("слово " * 100))[("future", 1)]
    assert metadata.criterion == ""
    metadata = parse_achievement_metadata(card("Первое.") + card("Второе."))[("future", 1)]
    assert metadata == AchievementMetadata("Название", "common")


@pytest.mark.parametrize("hint", ["", "   ", " Название ", "название"])
def test_empty_or_title_repeating_hint_adds_no_condition(hint):
    html = f'''<div class="b-achievement level-1" data-title="Название" data-hint="{hint}">
        <a href="/achievements/common/future"></a><div class="text">Художественный текст.</div></div>'''
    assert parse_achievement_metadata(html) == {("future", 1): AchievementMetadata("Название", "common")}


@pytest.mark.parametrize("extra", [
    '<div class="b-achievement level-1" data-title="Значок"><a href="/achievements/franchise/future"></a></div>',
    '<a class="b-badge level-2" title="Второй уровень" href="/achievements/franchise/future"></a>',
])
def test_conflicting_card_kind_or_higher_tier_prevents_binary_classification(extra):
    badge = '<a class="b-badge level-1" title="Значок" href="/achievements/franchise/future"></a>'
    metadata = parse_achievement_metadata(badge + extra)
    assert metadata[("future", 1)].title == "Значок"
    assert metadata[("future", 1)].is_binary is False
    assert parse_achievement_labels(badge + extra)[("future", 1)] == ("Значок", "franchise")


def test_live_shaped_cards_and_badges_include_exact_levels_and_decoded_titles():
    html = '''
    <div class="b-achievement level-4" data-title="Беззаботный зритель">
      <div class="c-about"><a class="title" href="/achievements/common/animelist">Зритель</a></div>
      <div><img src="unused"><a href="/achievements/common/animelist"></a></div>
    </div>
    <a class="b-badge level-1" title="Новая франшиза &amp; &quot;герои&quot;"
       href="/achievements/franchise/new_family"><img src="unused"></a>
    <a class="b-badge level-2" title="Автор" href="/achievements/author/new_author"></a>
    '''
    assert parse_achievement_labels(html) == {
        ("animelist", 4): ("Беззаботный зритель", "common"),
        ("new_family", 1): ('Новая франшиза & "герои"', "franchise"),
        ("new_author", 2): ("Автор", "author"),
    }


@pytest.mark.parametrize("attributes", [
    'class="b-badge level-1 level-2" title="Неясный уровень" href="/achievements/genre/future"',
    'class="b-badge level-1" title=" " href="/achievements/genre/future"',
    'class="b-badge level-1" title="Опасная ссылка" href="https://evil.example/achievements/genre/future"',
    'class="b-badge level-1" title="Опасная ссылка" href="/achievements/genre/../../future"',
    'class="title" href="/achievements/genre/future"',
])
def test_ambiguous_or_unbound_metadata_is_omitted(attributes):
    assert parse_achievement_labels(f"<a {attributes}>Подпись</a>") == {}


def test_level_zero_badge_and_card_labels_are_presentation_metadata():
    html = '''<a class="b-badge unprocessed level-0" title="Берсерк"
        href="/achievements/franchise/berserk" data-progress="40"></a>
        <div class="b-achievement level-0" data-title="Будущая победа">
        <a href="/achievements/common/future"></a></div>'''
    assert parse_achievement_labels(html) == {
        ("berserk", 0): ("Берсерк", "franchise"), ("future", 0): ("Будущая победа", "common"),
    }


def test_conflicting_titles_are_omitted_without_losing_unrelated_metadata():
    html = ''.join(
        f'<a class="b-badge level-1" title="{title}" href="/achievements/genre/{family}"></a>'
        for family, title in (("same", "А"), ("same", "Б"), ("same", "А"), ("other", "В"))
    )
    assert parse_achievement_labels(html) == {("other", 1): ("В", "genre")}


def test_changed_markup_has_no_labels_and_closed_card_does_not_label_another_link():
    assert parse_achievement_labels('<html><div>Страница изменилась</div></html>') == {}
    assert parse_achievement_labels('''
        <div class="b-achievement level-2" data-title="Прежняя карточка"><br></div>
        <a class="title" href="/achievements/common/other">Другая ссылка</a>
    ''') == {}


def test_author_badge_card_has_its_title_on_container_and_only_an_image_link():
    html = '''
        <div class="b-achievement is-badge level-1" data-title="Хидэаки Анно">
            <div class="c-image"><div class="inner">
                <a href="/achievements/author/hideaki_anno"><div class="border"></div>
                    <img src="unused" loading="lazy">
                </a>
            </div></div>
        </div>
    '''
    assert parse_achievement_labels(html) == {("hideaki_anno", 1): ("Хидэаки Анно", "author")}
