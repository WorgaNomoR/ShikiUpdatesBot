# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистое описание и рендеринг единого меню профиля."""

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape as h
from types import MappingProxyType

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

MENU_CALLBACK_PREFIX = "menu:"


@dataclass(frozen=True)
class MenuView:
    """Готовое представление одного экрана без побочных эффектов."""

    text: str
    keyboard: InlineKeyboardMarkup
    artwork: str
    caption: str | None = None


@dataclass(frozen=True)
class MenuEntry:
    """Одна централизованная кнопка меню."""

    text: str
    callback_data: str | None = None
    switch_inline_query: str | None = None
    switch_inline_query_current_chat: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        """Не допустить неоднозначную либо неработающую кнопку."""
        targets = (
            self.callback_data,
            self.switch_inline_query,
            self.switch_inline_query_current_chat,
            self.url,
        )
        if sum(target is not None for target in targets) != 1:
            raise ValueError("MenuEntry должен иметь ровно одну цель")


PUBLIC_HOME_ROWS: tuple[tuple[MenuEntry, ...], ...] = (
    (
        MenuEntry("👀 Сейчас", "menu:status"),
        MenuEntry("📊 Статистика", "menu:stats"),
    ),
    (
        MenuEntry("📋 Списки", "menu:lists"),
        MenuEntry("❤️ Избранное", "menu:favs"),
    ),
    (
        MenuEntry("💡 Интересный факт", "menu:fact"),
        MenuEntry("ℹ️ О боте", "menu:info"),
    ),
)

OWNER_ROWS: tuple[tuple[MenuEntry, ...], ...] = (
    (MenuEntry("🛠 Инструменты владельца", "menu:owner"),),
)

LIST_MEDIA: Mapping[str, tuple[str, str]] = MappingProxyType({
    "anime": ("🎬", "Аниме"),
    "manga": ("📚", "Манга"),
    "ranobe": ("📖", "Ранобэ"),
})

MAIN_MENU_ASSETS: Mapping[str, str] = MappingProxyType({
    "home": "home-v1.jpg",
    "subscription": "subscription-v1.jpg",
    "stats": "stats-v1.jpg",
    "lists": "lists-v1.jpg",
    "lists:anime": "lists-anime-v1.jpg",
    "lists:manga": "lists-manga-v1.jpg",
    "lists:ranobe": "lists-ranobe-v1.jpg",
    "owner": "owner-v1.jpg",
    "owner:backup": "owner-backup-v1.jpg",
    "owner:broadcast": "owner-broadcast-v1.jpg",
})


def _keyboard(rows: tuple[tuple[MenuEntry, ...], ...]) -> InlineKeyboardMarkup:
    """Преобразовать неизменяемое описание рядов в Telegram-клавиатуру."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=entry.text,
                callback_data=entry.callback_data,
                switch_inline_query=entry.switch_inline_query,
                switch_inline_query_current_chat=(
                    entry.switch_inline_query_current_chat
                ),
                url=entry.url,
            )
            for entry in row
        ]
        for row in rows
    ])


def home_view(
    display_name: str,
    profile_url: str,
    *,
    subscribed: bool,
    owner_tools: bool,
    inline_search_allowed: bool,
    private_chat: bool,
    private_search_url: str | None = None,
    notice: str | None = None,
) -> MenuView:
    """Собрать корневой экран для текущей роли и состояния подписки."""
    subscription = MenuEntry(
        "🔕 Отписаться" if subscribed else "🔔 Подписаться",
        "menu:subscription",
    )
    if inline_search_allowed:
        search = MenuEntry(
            "🔎 Найти тайтл",
            switch_inline_query_current_chat="",
        )
    elif private_chat or private_search_url is None:
        search = MenuEntry("🔎 Найти тайтл", "menu:inline_search")
    else:
        search = MenuEntry("🔎 Найти тайтл", url=private_search_url)
    rows = ((search,), (subscription,), *PUBLIC_HOME_ROWS)
    if owner_tools:
        rows = (*rows, *OWNER_ROWS)
    rows = (*rows, (MenuEntry("❌ Закрыть", "menu:close"),))
    prefix = f"{notice}\n\n" if notice else ""
    text = (
        f'{prefix}🎌 <b><a href="{h(profile_url, quote=True)}">'
        f"{h(display_name)}</a></b>\n"
        "Профиль Shikimori прямо в Telegram.\n\n"
        "🔔 <b>Подписка</b> — уведомления о новых событиях профиля\n"
        "👀 <b>Сейчас</b> — что смотрит и читает\n"
        "📊 <b>Статистика</b> — жанры, студии, оценки и время\n"
        "📋 <b>Списки</b> — аниме, манга и ранобэ по статусам\n"
        "❤️ <b>Избранное</b> — тайтлы, персонажи и люди индустрии\n"
        "💡 <b>Факт</b> — об аниме или Японии\n"
        "ℹ️ <b>О боте</b> — возможности, версия и проект\n\n"
        "🔎 <b>Поиск</b> — <code>аниме</code> (<code>а</code>), "
        "<code>манга</code> (<code>м</code>) или "
        "<code>ранобэ</code> (<code>р</code>) + название\n"
        "Например: <code>а Фрирен</code>"
    )
    return MenuView(
        text=text,
        keyboard=_keyboard(rows),
        artwork="home",
        caption=text,
    )


def subscription_view(*, subscribed: bool, target: bool) -> MenuView:
    """Собрать подтверждение включения или отключения уведомлений."""
    if target:
        text = (
            "🔔 <b>Подписаться на уведомления?</b>\n\n"
            "Бот будет присылать новости об активности профиля Shikimori."
        )
        confirm = MenuEntry("✅ Подписаться", "menu:subscription:confirm:on")
    else:
        text = (
            "🔕 <b>Отписаться от уведомлений?</b>\n\n"
            "Статус, статистика, списки, избранное и остальные функции "
            "останутся доступны через главное меню."
        )
        confirm = MenuEntry(
            "✅ Отписаться",
            "menu:subscription:confirm:off",
        )
    if subscribed == target:
        text += "\n\nЭто состояние уже установлено; подтверждение ничего не изменит."
    rows = (
        (confirm,),
        (MenuEntry("⬅️ Назад", "menu:home"),),
    )
    return MenuView(
        text=text,
        keyboard=_keyboard(rows),
        artwork="subscription",
        caption=text,
    )


def stats_view() -> MenuView:
    """Собрать выбор существующего статистического отчёта."""
    rows = (
        (MenuEntry("📆 За текущий квартал", "menu:stats:current"),),
        (MenuEntry("📚 За всё время", "menu:stats:all"),),
        (MenuEntry("⬅️ Назад", "menu:home"),),
    )
    return MenuView(
        text="📊 <b>Какую статистику показать?</b>",
        keyboard=_keyboard(rows),
        artwork="stats",
    )


def lists_view() -> MenuView:
    """Собрать корень браузера локальных списков."""
    rows = tuple(
        (MenuEntry(f"{emoji} {label}", f"menu:lists:{key}"),)
        for key, (emoji, label) in LIST_MEDIA.items()
    )
    rows = (
        *rows,
        (MenuEntry("🗂 Всё вместе", "menu:lists:combined"),),
        (MenuEntry("⬅️ Назад", "menu:home"),),
    )
    caption = "Данные берутся из последнего сохранённого обновления профиля."
    text = f"📋 <b>Какие списки показать?</b>\n\n{caption}"
    return MenuView(
        text=text,
        keyboard=_keyboard(rows),
        artwork="lists",
        caption=caption,
    )


def list_media_view(media_key: str) -> MenuView:
    """Собрать общий второй уровень выбранной media-категории."""
    emoji, label = LIST_MEDIA[media_key]
    rows = (
        (MenuEntry("✅ Завершённое", f"menu:lists:{media_key}:completed"),),
        (MenuEntry("📝 Запланированное", f"menu:lists:{media_key}:planned"),),
        (MenuEntry("📚 Полный список", f"menu:lists:{media_key}:all"),),
        (MenuEntry("⬅️ Назад", "menu:lists"),),
    )
    return MenuView(
        text=f"{emoji} <b>{label}</b>\n\nЧто показать?",
        keyboard=_keyboard(rows),
        artwork=f"lists:{media_key}",
    )


def owner_view() -> MenuView:
    """Собрать приватный корень инструментов владельца."""
    rows = (
        (MenuEntry("🎲 Подбор из планов", "menu:owner:pick"),),
        (MenuEntry("📢 Рассылка", "menu:owner:broadcast"),),
        (MenuEntry("👥 Пользователи", "menu:owner:users"),),
        (MenuEntry("🗃 Банк фактов", "menu:owner:facts"),),
        (MenuEntry("💾 Резервная копия", "menu:owner:backup"),),
        (MenuEntry("⬅️ Назад", "menu:home"),),
    )
    return MenuView(
        text="🛠 <b>Инструменты владельца</b>",
        keyboard=_keyboard(rows),
        artwork="owner",
    )


def owner_backup_view() -> MenuView:
    """Собрать экран существующих операций резервного копирования."""
    rows = (
        (
            MenuEntry("📤 Экспорт", "menu:owner:backup:export"),
            MenuEntry("📥 Импорт", "menu:owner:backup:import"),
        ),
        (MenuEntry("⬅️ Назад", "menu:owner"),),
    )
    caption = (
        "Экспорт создаёт архив состояния, а импорт восстанавливает "
        "поддерживаемые данные из такого архива."
    )
    text = f"💾 <b>Резервное копирование</b>\n\n{caption}"
    return MenuView(
        text=text,
        keyboard=_keyboard(rows),
        artwork="owner:backup",
        caption=caption,
    )


def owner_broadcast_view() -> MenuView:
    """Собрать безопасный вход в существующий FSM рассылки."""
    rows = (
        (MenuEntry("📢 Начать рассылку", "menu:owner:broadcast:start"),),
        (MenuEntry("⬅️ Назад", "menu:owner"),),
    )
    caption = (
        "После начала пришли одно сообщение. Перед отправкой бот покажет "
        "предпросмотр и запросит подтверждение."
    )
    text = f"📢 <b>Рассылка подписчикам</b>\n\n{caption}"
    return MenuView(
        text=text,
        keyboard=_keyboard(rows),
        artwork="owner:broadcast",
        caption=caption,
    )


def owner_operation_back_keyboard(parent_callback: str) -> InlineKeyboardMarkup:
    """Собрать Back для обратимого шага owner FSM до получения данных."""
    return _keyboard(((MenuEntry("⬅️ Назад", parent_callback),),))


def inline_return_keyboard() -> InlineKeyboardMarkup:
    """Собрать ручной возврат из deep link к выбору чата для поиска."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="Вернуться к поиску",
        switch_inline_query="",
    )]])
