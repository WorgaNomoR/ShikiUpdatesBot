# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Подписи любых достижений из карточек/значков; повреждения дают фолбэк."""

import pytest

from achievement_metadata import parse_achievement_labels


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
    'class="b-badge level-0" title="Ещё не получено" href="/achievements/genre/future"',
    'class="b-badge level-1 level-2" title="Неясный уровень" href="/achievements/genre/future"',
    'class="b-badge level-1" title=" " href="/achievements/genre/future"',
    'class="b-badge level-1" title="Опасная ссылка" href="https://evil.example/achievements/genre/future"',
    'class="b-badge level-1" title="Опасная ссылка" href="/achievements/genre/../../future"',
    'class="title" href="/achievements/genre/future"',
])
def test_unearned_ambiguous_or_unbound_metadata_is_omitted(attributes):
    assert parse_achievement_labels(f"<a {attributes}>Подпись</a>") == {}


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
