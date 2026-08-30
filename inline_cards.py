# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Безопасный рендер Telegram inline-карточек Shikimori."""

import html
import re
from urllib.parse import urlsplit

from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
)

from config import SHIKI_BASE_URL
from inline_facts import (
    InlineFact,
    select_inline_fact,
)
from project_meta import PROJECT_REPOSITORY
from utils import (
    _rel_url,
    h,
)

PHOTO_CAPTION_LIMIT = 1024
PROJECT_URL = f"https://github.com/{PROJECT_REPOSITORY}"

_HTML_TAG_RE = re.compile(r"<[^>]*>")
_HTML_BREAK_RE = re.compile(r"<\s*(?:br\s*/?|/p|/div)\s*>", re.IGNORECASE)
_BOT_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_SHIKI_BBCODE_RE = re.compile(
    r"\[/?(?:b|i|u|s|spoiler|quote|code|center|left|right|color|size|font|"
    r"url|img|character|person|anime|manga)(?:=[^\]]*)?\]",
    re.IGNORECASE,
)

_ICONS = {"anime": "🎬", "manga": "📚", "ranobe": "📖"}
# Карточка называет один тайтл; множественные подписи для счётчиков статистики
# остаются отдельным контрактом представления в stats.py.
_CARD_KIND_LABELS = {
    "tv": "TV-сериал",
    "movie": "Фильм",
    "ova": "OVA",
    "ona": "ONA",
    "special": "Спецвыпуск",
    "tv_special": "ТВ-спецвыпуск",
    "music": "Клип",
    "manga": "Манга",
    "manhwa": "Манхва",
    "manhua": "Маньхуа",
    "one_shot": "Ваншот",
    "doujin": "Додзинси",
    "light_novel": "Ранобэ",
    "novel": "Новелла",
}
_TAXONOMY = (
    ("demographic", "👥", "Демография"),
    ("genre", "🎭", "Жанры"),
    ("theme", "🏷", "Темы"),
)


def clean_shikimori_description(value: object) -> str:
    """Убрать HTML/BBCode Shikimori, сохранив обычный текст в скобках."""
    if not isinstance(value, str):
        return ""
    text = _HTML_BREAK_RE.sub(" ", value)
    text = _HTML_TAG_RE.sub("", text)
    text = _SHIKI_BBCODE_RE.sub("", text)
    return " ".join(html.unescape(text).split())


def parsed_caption_length(caption: str) -> int:
    """Посчитать видимый текст в используемых Telegram единицах UTF-16."""
    visible = html.unescape(_HTML_TAG_RE.sub("", caption))
    return len(visible.encode("utf-16-le")) // 2


def _plain(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _positive_number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _format_number(value: object) -> str:
    number = _positive_number(value)
    if number is None:
        return ""
    return f"{number:g}"


def _shikimori_url(media_type: str, item: dict) -> str:
    rel = _rel_url(_plain(item.get("url")))
    if not rel.startswith("/"):
        domain = "animes" if media_type == "anime" else "mangas"
        item_id = _plain(item.get("id"))
        rel = f"/{domain}/{item_id}" if item_id else f"/{domain}"
    return f"{SHIKI_BASE_URL.rstrip('/')}{rel}"


def _web_url(value: object) -> str:
    raw = _plain(value)
    if raw.startswith("/"):
        raw = f"{SHIKI_BASE_URL.rstrip('/')}{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return raw


def _taxonomy_items(item: dict, wanted_kind: str) -> list[str]:
    result: list[str] = []
    genres = item.get("genres")
    if not isinstance(genres, list):
        return result
    for genre in genres:
        if not isinstance(genre, dict) or genre.get("kind") != wanted_kind:
            continue
        name = _plain(genre.get("russian") or genre.get("name"))
        if name:
            result.append(name)
    return result


def _people_names(item: dict, field: str) -> list[str]:
    values = item.get(field)
    if not isinstance(values, list):
        return []
    return [
        name
        for value in values
        if isinstance(value, dict) and (name := _plain(value.get("name")))
    ]


def _facts(media_type: str, item: dict) -> list[str]:
    kind = _plain(item.get("kind"))
    kind_label = _CARD_KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())
    aired_on = item.get("airedOn")
    year = _plain(aired_on.get("year")) if isinstance(aired_on, dict) else ""
    heading = " · ".join(part for part in (kind_label, year) if part)

    lines: list[str] = []
    if heading:
        lines.append(f"<b>{h(heading)}</b>")

    score = _format_number(item.get("score"))
    if score:
        lines.append(f"⭐ Оценка: {h(score)}")
    if media_type == "anime":
        metrics: list[str] = []
        episodes = _format_number(item.get("episodes"))
        duration = _format_number(item.get("duration"))
        if episodes:
            metrics.append(f"{episodes} эп.")
        if duration:
            metrics.append(f"{duration} мин.")
        if metrics:
            lines.append(f"⏱ {h(' · '.join(metrics))}")
        origin = _plain(item.get("origin"))
        rating = _plain(item.get("rating"))
        if origin:
            lines.append(f"📖 Первоисточник: {h(origin)}")
        if rating:
            lines.append(f"🔖 Возрастной рейтинг: {h(rating)}")
        people = _people_names(item, "studios")
        people_icon = "🎞"
        people_label = "Студия" if len(people) == 1 else "Студии"
    else:
        metrics = []
        chapters = _format_number(item.get("chapters"))
        volumes = _format_number(item.get("volumes"))
        if chapters:
            metrics.append(f"{chapters} гл.")
        if volumes:
            metrics.append(f"{volumes} т.")
        if metrics:
            lines.append(f"📄 {h(' · '.join(metrics))}")
        people = _people_names(item, "publishers")
        people_icon = "🏢"
        people_label = "Издатель" if len(people) == 1 else "Издатели"

    if _plain(item.get("status")) == "ongoing":
        lines.append("🟢 Онгоинг")
    if people:
        lines.append(f"{people_icon} {people_label}: {h(' · '.join(people))}")
    return lines


def _short_description(description: str, word_count: int) -> str:
    words = description.split()
    if word_count >= len(words):
        return description
    if word_count <= 0:
        return ""
    return " ".join(words[:word_count]).rstrip(".,;:!?") + "…"


def _render_caption(
    *,
    media_type: str,
    display_title: str,
    japanese_title: str,
    url: str,
    facts: list[str],
    taxonomies: list[tuple[str, str, list[str], int]],
    description: str,
) -> str:
    lines = [
        f'{_ICONS[media_type]} <b><a href="{h(url)}">{h(display_title)}</a></b>'
    ]
    if japanese_title:
        lines.append(f"<i>{h(japanese_title)}</i>")
    if facts:
        lines.extend(("", f"<blockquote>{'\n'.join(facts)}</blockquote>"))
    taxonomy_lines = []
    for emoji, label, items, omitted in taxonomies:
        rendered = [h(value) for value in items]
        if omitted:
            rendered.append(f"… ещё {omitted}")
        if rendered:
            taxonomy_lines.append(
                f"{emoji} <b>{label}:</b> {' · '.join(rendered)}"
            )
    if taxonomy_lines:
        lines.extend(("", *taxonomy_lines))
    if description:
        lines.extend((
            "",
            "📝 <b>Описание</b>",
            f"<blockquote><tg-spoiler>{h(description)}</tg-spoiler></blockquote>",
        ))
    return "\n".join(lines)


def build_card_caption(media_type: str, item: dict) -> str:
    """Построить безопасную подпись с приоритетным сокращением описания."""
    display_title = _plain(item.get("russian") or item.get("name")) or "Без названия"
    japanese_title = _plain(item.get("japanese"))
    url = _shikimori_url(media_type, item)
    facts = _facts(media_type, item)
    taxonomies = [
        (emoji, label, _taxonomy_items(item, kind), 0)
        for kind, emoji, label in _TAXONOMY
    ]
    description = clean_shikimori_description(item.get("description"))

    def render(current_description: str) -> str:
        return _render_caption(
            media_type=media_type,
            display_title=display_title,
            japanese_title=japanese_title,
            url=url,
            facts=facts,
            taxonomies=taxonomies,
            description=current_description,
        )

    caption = render(description)
    if parsed_caption_length(caption) <= PHOTO_CAPTION_LIMIT:
        return caption

    words = description.split()
    low, high = 0, len(words)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate = _short_description(description, middle)
        candidate_caption = render(candidate)
        if parsed_caption_length(candidate_caption) <= PHOTO_CAPTION_LIMIT:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    caption = render(best)

    while parsed_caption_length(caption) > PHOTO_CAPTION_LIMIT:
        changed = False
        for index in range(len(taxonomies) - 1, -1, -1):
            emoji, label, items, omitted = taxonomies[index]
            if not items:
                continue
            taxonomies[index] = (emoji, label, items[:-1], omitted + 1)
            changed = True
            break
        if not changed:
            break
        caption = render(best)

    if parsed_caption_length(caption) <= PHOTO_CAPTION_LIMIT:
        return caption

    # Защитный путь для патологически длинных заголовков из API: сохраняем
    # целые HTML-теги и гарантируем Telegram-лимит, не режем entity вручную.
    japanese_title = ""
    facts = []
    taxonomies = []
    caption = render("")
    if parsed_caption_length(caption) <= PHOTO_CAPTION_LIMIT:
        return caption
    words = display_title.split()
    while words and parsed_caption_length(caption) > PHOTO_CAPTION_LIMIT:
        words.pop()
        display_title = " ".join(words).rstrip(".,;:!?") + "…"
        caption = render("")
    return caption


def _chooser_description(media_type: str, item: dict) -> str:
    kind = _plain(item.get("kind"))
    kind_label = _CARD_KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())
    aired_on = item.get("airedOn")
    year = _plain(aired_on.get("year")) if isinstance(aired_on, dict) else ""
    score = _format_number(item.get("score"))
    parts = [part for part in (kind_label, year) if part]
    if score:
        parts.append(f"⭐ {score}")
    return " · ".join(parts)


def _card_keyboard(
    bot_username: str | None,
    shikimori_url: str,
) -> InlineKeyboardMarkup:
    """Добавить полезные действия без расходования лимита подписи."""
    buttons = [
        InlineKeyboardButton(
            text="🔎 Новый поиск",
            switch_inline_query_current_chat="",
        ),
        InlineKeyboardButton(
            text="На Shikimori",
            url=shikimori_url,
        ),
    ]
    username = _plain(bot_username).removeprefix("@")
    if _BOT_USERNAME_RE.fullmatch(username):
        buttons.append(
            InlineKeyboardButton(
                text="ℹ️ О боте",
                url=f"https://t.me/{username}?start=info",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def _build_inline_result(
    media_type: str,
    item: dict,
    *,
    allow_photo: bool,
    bot_username: str | None,
) -> InlineQueryResultPhoto | InlineQueryResultArticle:
    """Собрать один результат с учётом выбранного режима представления."""
    item_id = _plain(item.get("id"))
    if not item_id.isdigit():
        raise ValueError("карточка Shikimori не содержит корректный id")
    result_id = f"{media_type}:{item_id}"
    display_title = _plain(item.get("russian") or item.get("name")) or "Без названия"
    caption = build_card_caption(media_type, item)
    description = _chooser_description(media_type, item)
    url = _shikimori_url(media_type, item)
    keyboard = _card_keyboard(bot_username, url)
    poster = item.get("poster")
    poster = poster if isinstance(poster, dict) else {}
    photo_url = _web_url(poster.get("originalUrl"))
    thumbnail_url = _web_url(poster.get("mainUrl")) or photo_url

    if photo_url and allow_photo:
        return InlineQueryResultPhoto(
            id=result_id,
            photo_url=photo_url,
            thumbnail_url=thumbnail_url,
            title=display_title,
            description=description or None,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    return InlineQueryResultArticle(
        id=result_id,
        title=display_title,
        description=description or None,
        url=url,
        hide_url=True,
        thumbnail_url=(photo_url or thumbnail_url) or None,
        reply_markup=keyboard,
        input_message_content=InputTextMessageContent(
            message_text=caption,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        ),
    )


def build_inline_result(
    media_type: str,
    item: dict,
    *,
    bot_username: str | None = None,
) -> InlineQueryResultPhoto | InlineQueryResultArticle:
    """Собрать результат с постером или равноправную текстовую карточку."""
    return _build_inline_result(
        media_type,
        item,
        allow_photo=True,
        bot_username=bot_username,
    )


def build_project_promo_result() -> InlineQueryResultArticle:
    """Собрать честно обозначенную карточку для распространения проекта."""
    title = "ShikiUpdatesBot: активность и поиск на Shikimori"
    message = (
        f'🎌 <b><a href="{h(PROJECT_URL)}">{h(title)}</a></b>\n\n'
        "Telegram-бот для уведомлений об активности на Shikimori, просмотра "
        "статистики и поиска карточек аниме, манги и ранобэ прямо из любого "
        "чата.\n\n"
        "Исходный код и инструкция по запуску собственного бота доступны "
        f'на <a href="{h(PROJECT_URL)}">GitHub</a>.'
    )
    return InlineQueryResultArticle(
        id="project:share",
        title="📣 Поделиться ShikiUpdatesBot",
        description="Отправит в чат сообщение о боте и ссылку на GitHub",
        url=PROJECT_URL,
        hide_url=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="GitHub и установка",
                url=PROJECT_URL,
            ),
        ]]),
        input_message_content=InputTextMessageContent(
            message_text=message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        ),
    )


def build_fact_text(fact: InlineFact) -> str:
    """Собрать единый безопасный HTML факта для команды и inline-отправки."""
    owner_note = ""
    if fact.owner_pick:
        owner_note = "\n\n🎁 <i>Выбор владельца ShikiUpdatesBot</i>"
    return (
        "💡 <b>Интересный факт</b>\n\n"
        f"{h(fact.text)}{owner_note}"
    )


def build_fact_keyboard(*, next_callback_data: str) -> InlineKeyboardMarkup:
    """Собрать личные кнопки обновления факта и ручного перехода к отправке."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Ещё факт",
            callback_data=next_callback_data,
        ),
        InlineKeyboardButton(
            text="Поделиться",
            switch_inline_query="fact",
        ),
    ]])


def build_fact_result(fact: InlineFact, *, page: int) -> InlineQueryResultArticle:
    """Собрать явно подписанный результат, отправляющий факт в чат."""
    return InlineQueryResultArticle(
        id=f"fact:{page}:{fact.id}",
        title="💡 Отправить интересный факт",
        description=(
            "При выборе отправит в чат факт об аниме или Японии"
        ),
        input_message_content=InputTextMessageContent(
            message_text=build_fact_text(fact),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        ),
    )


def finalize_inline_results(
    rendered: list[InlineQueryResultPhoto | InlineQueryResultArticle],
    *,
    page: int,
    fact_seed: str,
) -> list[InlineQueryResultPhoto | InlineQueryResultArticle]:
    """Сохранить смешанный список без замены карточек с постерами."""
    results = list(rendered)
    if not results:
        return results
    if page == 1:
        results.append(build_project_promo_result())
    elif all(isinstance(result, InlineQueryResultPhoto) for result in results):
        fact = select_inline_fact(fact_seed, page=page)
        results.append(build_fact_result(fact, page=page))
    return results
