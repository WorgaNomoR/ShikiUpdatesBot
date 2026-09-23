# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистая карта экранов и централизованных кнопок главного меню."""

import pytest

import main_menu


def _callbacks(view):
    return [
        button.callback_data
        for row in view.keyboard.inline_keyboard
        for button in row
    ]


def _texts(view):
    return [
        button.text
        for row in view.keyboard.inline_keyboard
        for button in row
    ]


def _home(**overrides):
    values = {
        "display_name": "WNR",
        "profile_url": "https://shikimori.io/WNR",
        "subscribed": False,
        "owner_tools": False,
        "inline_search_allowed": False,
        "private_chat": True,
    }
    values.update(overrides)
    return main_menu.home_view(**values)


def test_public_home_exact_actions_and_subscription_label():
    unsubscribed = _home()
    subscribed = _home(
        subscribed=True,
        inline_search_allowed=True,
    )

    assert _callbacks(unsubscribed) == [
        "menu:inline_search",
        "menu:subscription",
        "menu:status",
        "menu:stats",
        "menu:lists",
        "menu:favs",
        "menu:fact",
        "menu:info",
        "menu:close",
    ]
    assert _texts(unsubscribed)[:2] == ["🔎 Найти тайтл", "🔔 Подписаться"]
    assert _texts(subscribed)[:2] == ["🔎 Найти тайтл", "🔕 Отписаться"]
    search = subscribed.keyboard.inline_keyboard[0][0]
    assert search.switch_inline_query is None
    assert search.switch_inline_query_current_chat == ""
    assert "menu:owner" not in _callbacks(unsubscribed)


def test_home_explains_sections_search_and_links_escaped_profile():
    view = _home(
        display_name="<WNR>",
        profile_url='https://shikimori.io/WNR?x="unsafe"&y=1',
    )

    assert (
        '<a href="https://shikimori.io/WNR?x=&quot;unsafe&quot;&amp;y=1">'
        "&lt;WNR&gt;</a>"
    ) in view.text
    assert "Профиль Shikimori прямо в Telegram" in view.text
    assert "<b>Подписка</b> — уведомления" in view.text
    assert view.text.index("<b>Подписка</b>") < view.text.index("<b>Сейчас</b>")
    assert view.text.index("<b>Поиск</b>") > view.text.index("<b>О боте</b>")
    assert "<code>аниме</code> (<code>а</code>)" in view.text
    assert "<code>а Фрирен</code>" in view.text


def test_group_non_subscriber_search_uses_private_deep_link():
    view = _home(
        private_chat=False,
        private_search_url="https://t.me/WorgaTestBot?start=inline_search",
    )

    button = view.keyboard.inline_keyboard[0][0]
    assert button.callback_data is None
    assert button.url == "https://t.me/WorgaTestBot?start=inline_search"


def test_owner_home_adds_one_centralized_private_tools_entry():
    view = _home(
        owner_tools=True,
        inline_search_allowed=True,
    )

    assert _callbacks(view)[-2:] == ["menu:owner", "menu:close"]


def test_owner_tools_prioritize_useful_actions_and_omit_version():
    view = main_menu.owner_view()

    assert _texts(view) == [
        "🎲 Подбор из планов",
        "📢 Рассылка",
        "👥 Пользователи",
        "🗃 Банк фактов",
        "💾 Резервная копия",
        "⬅️ Назад",
    ]
    assert _callbacks(view) == [
        "menu:owner:pick",
        "menu:owner:broadcast",
        "menu:owner:users",
        "menu:owner:facts",
        "menu:owner:backup",
        "menu:home",
    ]


def test_every_logical_child_has_back_to_immediate_parent():
    assert _callbacks(main_menu.subscription_view(
        subscribed=False,
        target=True,
    ))[-1] == "menu:home"
    assert _callbacks(main_menu.stats_view())[-1] == "menu:home"
    assert _callbacks(main_menu.lists_view())[-1] == "menu:home"
    assert _callbacks(main_menu.list_media_view("anime"))[-1] == "menu:lists"
    assert _callbacks(main_menu.owner_view())[-1] == "menu:home"
    assert _callbacks(main_menu.owner_backup_view())[-1] == "menu:owner"
    assert _callbacks(main_menu.owner_broadcast_view())[-1] == "menu:owner"


def test_root_has_close_but_no_back():
    callbacks = _callbacks(_home())

    assert callbacks[-1] == "menu:close"
    assert "menu:home" not in callbacks


def test_every_screen_uses_one_versioned_artwork_from_central_registry():
    views = {
        "home": _home(),
        "subscription": main_menu.subscription_view(
            subscribed=False,
            target=True,
        ),
        "stats": main_menu.stats_view(),
        "lists": main_menu.lists_view(),
        "lists:anime": main_menu.list_media_view("anime"),
        "lists:manga": main_menu.list_media_view("manga"),
        "lists:ranobe": main_menu.list_media_view("ranobe"),
        "owner": main_menu.owner_view(),
        "owner:backup": main_menu.owner_backup_view(),
        "owner:broadcast": main_menu.owner_broadcast_view(),
    }

    assert set(views) == set(main_menu.MAIN_MENU_ASSETS)
    for key, view in views.items():
        assert view.artwork == key
        assert main_menu.MAIN_MENU_ASSETS[key].endswith("-v1.jpg")


@pytest.mark.parametrize("registry", [
    main_menu.LIST_MEDIA,
    main_menu.MAIN_MENU_ASSETS,
])
def test_central_menu_registries_are_immutable(registry):
    with pytest.raises(TypeError):
        registry["foreign"] = "value"


def test_captions_keep_dynamic_or_instructional_text_only():
    home = _home()
    subscription = main_menu.subscription_view(
        subscribed=False,
        target=True,
    )

    assert home.caption == home.text
    assert subscription.caption == subscription.text
    assert main_menu.stats_view().caption is None
    assert main_menu.list_media_view("anime").caption is None
    assert main_menu.owner_view().caption is None
    for view in (
        main_menu.lists_view(),
        main_menu.owner_backup_view(),
        main_menu.owner_broadcast_view(),
    ):
        assert view.caption is not None
        assert view.text.endswith(view.caption)
