# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
import json
import random
import re
import time
from pathlib import Path
from string import Formatter

import pytest

import messages
from messages import (
    _strip_html,
    build_favourite_message,
    build_message,
    build_startup_snapshot,
    classify_event,
    clean_description,
    extract_score,
    extract_score_change,
    format_rate_entry,
)
from name_grammar import DisplayNameContext, build_display_name_context
from utils import _utcnow, h


def fixed_choice(seq):
    return seq[0]


def make_entry(
    description,
    title="Ergo Proxy",
    url="/animes/790-ergo-proxy",
    *,
    target_type=None,
    kind=None,
):
    entry = {
        "description": description,
        "target": {
            "name": title,
            "url": url,
        },
        "created_at": "2025-01-01T12:00:00.000Z",
    }
    if target_type is not None:
        entry["target"]["type"] = target_type
    if kind is not None:
        entry["target"]["kind"] = kind
    return entry


def render_message_template(
    template,
    context,
    *,
    media_key,
    title,
    url="",
    **values,
):
    title_html = h(title)
    if url:
        title_html = f'<a href="{messages.SHIKI_BASE_URL}{url}">{title_html}</a>'
    labeled_title = messages._label_media_title(title_html, media_key)
    return messages.format_name_template(
        template,
        context,
        title=labeled_title,
        **values,
    )


FALLBACK_NAME_CONTEXT = build_display_name_context("WorgaNomoR", "none")


@pytest.fixture(scope="module")
def history_event_fixtures():
    path = Path(__file__).with_name("fixtures") / "history_event_types.json"
    return json.loads(path.read_text(encoding="utf-8"))


def expected_score_changed_messages(bank_key, old, new, media_label=None):
    n = h(FALLBACK_NAME_CONTEXT.nominative)
    title_text = "Ergo Proxy"
    if media_label is not None:
        title_text += f" ({media_label})"
    title = f"<b>{title_text}</b>"
    banks = {
        "score_changed": {
            f"🔄 {n} пересмотрел оценку {title}: было {old}, стало {new}. Что-то изменилось.",
            f"🤔 Оценка {title} пересмотрена: {old} → {new}. {n} явно что-то переосмыслил.",
            f"🏹 {old} → {new} за {title}. {n} дал второй шанс (или отобрал).",
            f"⚖️ Весы справедливости скорректированы: {title} теперь {new}/10 вместо {old}.",
            f"✏️ {n} исправил оценку {title} с {old} на {new}. Бывает, мнения меняются.",
            f"📊 Обновление рейтинга: {title} {old} → {new}. {n} не стоит на месте.",
        },
        "score_changed_up": {
            f"📈 {title}: {old} → {new}. {n} пересмотрел и проникся.",
            f"✨ Оценка {title} выросла с {old} до {new}. Со временем впечатление стало лучше — и {n} это оценил.",
            f"🤝 Второй шанс сработал: {title} получает от {n} уже {new}/10 вместо {old}.",
            f"🧠 Послевкусие оказалось приятнее: {n} поднял {title} с {old} до {new}.",
            f"🚀 {title} идёт на повышение: {old} → {new}. Уважение заслужено.",
            f"💡 Что-то щёлкнуло — и {n} повысил оценку {title} с {old} до {new}.",
        },
        "score_changed_down": {
            f"📉 {title}: {old} → {new}. Похоже, {n} немного остыл.",
            f"🌧️ Оценка {title} снизилась с {old} до {new}. Без обид — просто настроение сменилось.",
            f"🫠 Магия чуть выветрилась: {n} опустил {title} с {old} до {new}.",
            f"🤷 Было {old}, стало {new}: {n} ещё подумал о {title} и решил быть честнее.",
            f"🧊 {title} теперь получает {new}/10 вместо {old}. Послевкусие немного остыло.",
            f"🔍 Чем дольше {n} размышляет о {title}, тем скромнее становится оценка: {old} → {new}.",
        },
    }
    return banks[bank_key]


# ==========================================================
# h()
# ==========================================================

def test_h_escapes_angle_brackets():
    assert h("<Ergo Proxy>") == "&lt;Ergo Proxy&gt;"


def test_h_escapes_ampersand():
    assert h("A&B") == "A&amp;B"


def test_h_escapes_quotes():
    assert h('"test"') == "&quot;test&quot;"


def test_h_plain_text():
    assert h("Evangelion") == "Evangelion"


# ==========================================================
# _status_block()
# ==========================================================

def test_status_block_uses_anime_labels_and_preserves_order():
    block = messages._status_block(
        {
            "total_completed": 3,
            "total_dropped": 2,
            "total_watching": 1,
            "total_planned": 4,
            "total_on_hold": 5,
        },
        completed_label="Завершено",
        watching_label="Смотрю",
    )

    assert block == [
        "📦 <b>Статусы</b>",
        "<code>Завершено · 3\n"
        "Брошено ··· 2\n"
        "Смотрю ···· 1\n"
        "В планах ·· 4\n"
        "Отложено ·· 5</code>",
    ]


def test_status_block_uses_manga_labels_and_hides_zeroes():
    block = messages._status_block(
        {
            "total_completed": 2,
            "total_dropped": 0,
            "total_watching": 1,
            "total_planned": 0,
            "total_on_hold": 0,
        },
        completed_label="Прочитано",
        watching_label="Читаю",
    )

    assert block == [
        "📦 <b>Статусы</b>",
        "<code>Прочитано · 2\nЧитаю ····· 1</code>",
    ]


def test_status_block_empty_aggregate_returns_empty_list():
    assert messages._status_block(
        {}, completed_label="Завершено", watching_label="Смотрю",
    ) == []


# ==========================================================
# build_message()
# ==========================================================

@pytest.mark.parametrize("desc, score, key", [
    ("просмотрено", None, "completed_no_score"),
    ("просмотрено и оценено на 3", 3, "completed_score_low"),
    ("просмотрено и оценено на 6", 6, "completed_score_mid"),
    ("просмотрено и оценено на 9", 9, "completed_score_high"),
    ("просмотрено и оценено на 10", 10, "completed_score_perfect"),
])
def test_build_message_completed_selects_bank_by_score(monkeypatch, desc, score, key):
    # с fixed_choice шаблон детерминирован -> точная сверка ВЫБРАННОГО банка,
    # а не «цифра где-то в тексте» (та проходит на любом банке -> мутационно дырява)
    monkeypatch.setattr(random, "choice", fixed_choice)
    msg = build_message(make_entry(desc))
    title = (
        f'<a href="{messages.SHIKI_BASE_URL}/animes/790-ergo-proxy">'
        "Ergo Proxy</a> (аниме)"
    )
    expected = messages.format_name_template(
        messages.MESSAGES["anime"][key][0],
        messages.DISPLAY_NAME_CONTEXT,
        title=title,
        score=score if score is not None else "?",
    )
    assert msg == expected


def test_build_message_manga_uses_manga_bank(monkeypatch):
    # media_type определяется по target.type (не по url) -> проверяем банк manga
    monkeypatch.setattr(random, "choice", fixed_choice)
    entry = {
        "description": "прочитано и оценено на 3",
        "target": {"name": "Berserk", "url": "/mangas/25-berserk", "type": "Manga"},
        "created_at": "2025-01-01T12:00:00.000Z",
    }
    msg = build_message(entry)
    title = (
        f'<a href="{messages.SHIKI_BASE_URL}/mangas/25-berserk">'
        "Berserk</a> (манга)"
    )
    expected = messages.format_name_template(
        messages.MESSAGES["manga"]["completed_score_low"][0],
        messages.DISPLAY_NAME_CONTEXT,
        title=title,
        score=3,
    )
    assert msg == expected


@pytest.mark.parametrize("locale", ["ru", "en"])
@pytest.mark.parametrize(("target_type", "kind", "bank_key", "label", "url"), [
    ("Anime", "tv", "anime", "аниме", "/animes/1-title"),
    ("Manga", "manga", "manga", "манга", "/mangas/1-title"),
    ("Manga", "light_novel", "ranobe", "ранобэ", "/mangas/1-title"),
])
def test_on_hold_fixtures_route_to_media_bank(
    monkeypatch,
    history_event_fixtures,
    locale,
    target_type,
    kind,
    bank_key,
    label,
    url,
):
    monkeypatch.setattr(random, "choice", fixed_choice)
    description = history_event_fixtures["on_hold"][locale]["entry"]["description"]
    entry = make_entry(
        description,
        title="Title",
        url=url,
        target_type=target_type,
        kind=kind,
    )
    title = f'<a href="{messages.SHIKI_BASE_URL}{url}">Title</a> ({label})'
    expected = messages.format_name_template(
        messages.MESSAGES[bank_key]["on_hold"][0],
        messages.DISPLAY_NAME_CONTEXT,
        title=title,
        score="?",
    )

    assert build_message(entry) == expected


def test_unknown_history_message_is_neutral_and_html_safe(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(make_entry("<script>alert('&')</script>"))

    assert "Неизвест" not in msg
    assert "alert(&#x27;&amp;&#x27;)" in msg
    assert "&lt;script&gt;" not in msg
    assert "<script>" not in msg


def test_score_set_has_own_notification_and_is_not_completion(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(make_entry("Оценено на <b>8</b>"))

    assert msg == messages.format_name_template(
        messages.MESSAGES["score_set"][0],
        messages.DISPLAY_NAME_CONTEXT,
        title=(
            f'<a href="{messages.SHIKI_BASE_URL}/animes/790-ergo-proxy">'
            "Ergo Proxy</a> (аниме)"
        ),
        score=8,
    )


@pytest.mark.parametrize("kind", ["light_novel", "novel", "ranobe"])
@pytest.mark.parametrize(("description", "bank_key", "score"), [
    ("добавлено в список", "planned", "?"),
    ("читаю", "watching", "?"),
    ("перечитываю", "rewatching", "?"),
    ("брошено", "dropped", "?"),
    ("прочитано", "completed_no_score", "?"),
    ("прочитано и оценено на 3", "completed_score_low", 3),
    ("прочитано и оценено на 6", "completed_score_mid", 6),
    ("прочитано и оценено на 9", "completed_score_high", 9),
    ("прочитано и оценено на 10", "completed_score_perfect", 10),
])
def test_build_message_ranobe_kinds_use_every_ranobe_bank(
    kind,
    description,
    bank_key,
    score,
):
    relative_url = "/mangas/1-book"
    title = (
        f'<a href="{messages.SHIKI_BASE_URL}{relative_url}">'
        "Ergo Proxy</a> (ранобэ)"
    )
    expected_messages = {
        messages.format_name_template(
            template,
            messages.DISPLAY_NAME_CONTEXT,
            title=title,
            score=score,
        )
        for template in messages.MESSAGES["ranobe"][bank_key]
    }

    msg = build_message(make_entry(
        description,
        url=relative_url,
        target_type="Manga",
        kind=kind,
    ))

    assert msg in expected_messages
    assert msg.count(messages.SHIKI_BASE_URL) == 1


@pytest.mark.parametrize(("description", "bank_key", "old", "new"), [
    ("изменена оценка с 1 на 10", "score_changed_up", 1, 10),
    ("изменена оценка с 10 на 1", "score_changed_down", 10, 1),
    ("изменена оценка с 5 на 5", "score_changed", 5, 5),
    ("изменена оценка с 0 на 5", "score_changed", 0, 5),
    ("изменена оценка с 5 на 0", "score_changed", 5, 0),
    ("изменена оценка с 11 на 5", "score_changed", 11, 5),
    ("изменена оценка с 5 на 11", "score_changed", 5, 11),
])
def test_score_changed_selects_direction_bank(
    monkeypatch,
    description,
    bank_key,
    old,
    new,
):
    monkeypatch.setattr(messages, "DISPLAY_NAME_CONTEXT", FALLBACK_NAME_CONTEXT)
    msg = build_message(make_entry(description, url=""))
    assert msg in expected_score_changed_messages(bank_key, old, new, "аниме")


def test_score_changed_unparseable_uses_neutral_bank(monkeypatch):
    monkeypatch.setattr(messages, "DISPLAY_NAME_CONTEXT", FALLBACK_NAME_CONTEXT)
    msg = build_message(make_entry("изменена оценка", url=""))
    assert msg in expected_score_changed_messages(
        "score_changed", "?", "?", "аниме",
    )


@pytest.mark.parametrize(("target_type", "kind", "label", "url"), [
    ("Anime", "tv", "аниме", "/animes/790-ergo-proxy"),
    ("Manga", "manga", "манга", "/mangas/790-ergo-proxy"),
    ("Manga", "light_novel", "ранобэ", "/mangas/790-ergo-proxy"),
    ("Manga", "novel", "ранобэ", "/mangas/790-ergo-proxy"),
    ("Manga", "ranobe", "ранобэ", "/mangas/790-ergo-proxy"),
])
def test_score_changed_includes_human_media_label(
    target_type,
    kind,
    label,
    url,
):
    msg = build_message(make_entry(
        "изменена оценка с 5 на 1",
        url=url,
        target_type=target_type,
        kind=kind,
    ))

    assert f"Ergo Proxy</a> ({label})</b>" in msg
    assert msg.count(messages.SHIKI_BASE_URL) == 1


@pytest.mark.parametrize("description", [
    "добавлено в список",
    "смотрю",
    "пересматриваю",
    "отложено",
    "брошено",
    "просмотрено",
    "просмотрено и оценено на 8",
    "оценено на 8",
    "изменена оценка с 5 на 8",
    "неизвестный новый формат",
])
@pytest.mark.parametrize(("target_type", "kind", "label", "url"), [
    ("Anime", "tv", "аниме", "/animes/1-title"),
    ("Manga", "manga", "манга", "/mangas/1-title"),
    ("Manga", "light_novel", "ранобэ", "/mangas/1-title"),
])
def test_every_history_path_includes_media_label_once(
    description,
    target_type,
    kind,
    label,
    url,
):
    msg = build_message(make_entry(
        description,
        url=url,
        target_type=target_type,
        kind=kind,
    ))

    assert msg.count(f"({label})") == 1


FINAL_RENDER_CONTEXTS = [
    DisplayNameContext(
        nominative="Иван",
        genitive="Ивана",
        dative="Ивану",
        accusative="Ивана",
        instrumental="Иваном",
        gender="male",
        inflection_applied=True,
    ),
    DisplayNameContext(
        nominative="Анна",
        genitive="Анны",
        dative="Анне",
        accusative="Анну",
        instrumental="Анной",
        gender="female",
        inflection_applied=True,
    ),
    DisplayNameContext(
        nominative="WNR",
        genitive="WNR",
        dative="WNR",
        accusative="WNR",
        instrumental="WNR",
        gender=None,
        inflection_applied=False,
    ),
]


HISTORY_MEDIA_CASES = {
    "anime": ("Anime", "tv", "аниме", "/animes/1-audit"),
    "manga": ("Manga", "manga", "манга", "/mangas/1-audit"),
    "ranobe": ("Manga", "light_novel", "ранобэ", "/mangas/1-audit"),
}


def _history_description(bank_key, message_key):
    reading = bank_key != "anime"
    descriptions = {
        "planned": "добавлено в список",
        "watching": "читаю" if reading else "смотрю",
        "rewatching": "перечитываю" if reading else "пересматриваю",
        "on_hold": "отложено",
        "dropped": "брошено",
        "completed_no_score": "прочитано" if reading else "просмотрено",
        "completed_score_low": (
            "прочитано и оценено на 3" if reading
            else "просмотрено и оценено на 3"
        ),
        "completed_score_mid": (
            "прочитано и оценено на 6" if reading
            else "просмотрено и оценено на 6"
        ),
        "completed_score_high": (
            "прочитано и оценено на 9" if reading
            else "просмотрено и оценено на 9"
        ),
        "completed_score_perfect": (
            "прочитано и оценено на 10" if reading
            else "просмотрено и оценено на 10"
        ),
    }
    return descriptions[message_key]


@pytest.mark.parametrize(
    "context",
    FINAL_RENDER_CONTEXTS,
    ids=["male", "female", "fallback"],
)
def test_every_history_template_renders_final_media_title_once(
    context,
):
    for bank_key, (_target_type, _kind, label, url) in HISTORY_MEDIA_CASES.items():
        for message_key, templates in messages.MESSAGES[bank_key].items():
            for template in templates:
                rendered = render_message_template(
                    template,
                    context,
                    media_key=bank_key,
                    title="AuditTitle",
                    url=url,
                    score=8,
                    old=4,
                    new=9,
                    description="новый формат события",
                )
                plain = _strip_html(rendered)

                assert rendered.count(f"({label})") == 1, template
                assert rendered.count(messages.SHIKI_BASE_URL) == 1, template
                assert "{" not in rendered and "}" not in rendered, template
                assert re.search(
                    rf"(?iu)\b(?:аниме|манг\w*|ранобэ)\s+"
                    rf"AuditTitle \({label}\)",
                    plain,
                ) is None, template


SHARED_HISTORY_CASES = {
    "score_changed": "изменена оценка с 5 на 5",
    "score_changed_up": "изменена оценка с 4 на 9",
    "score_changed_down": "изменена оценка с 9 на 4",
    "score_set": "оценено на 8",
    "unknown": "новый формат события",
}


@pytest.mark.parametrize(
    "context",
    FINAL_RENDER_CONTEXTS,
    ids=["male", "female", "fallback"],
)
def test_every_shared_history_template_renders_for_every_media(
    context,
):
    for media_key, (_target_type, _kind, label, url) in HISTORY_MEDIA_CASES.items():
        for message_key, description in SHARED_HISTORY_CASES.items():
            for template in messages.MESSAGES[message_key]:
                change = extract_score_change(description) or ("?", "?")
                score = extract_score(description)
                rendered = render_message_template(
                    template,
                    context,
                    media_key=media_key,
                    title="AuditTitle",
                    url=url,
                    score=score if score is not None else "?",
                    old=change[0],
                    new=change[1],
                    description=description,
                )

                assert rendered.count(f"({label})") == 1, template
                assert rendered.count(messages.SHIKI_BASE_URL) == 1, template
                assert "{" not in rendered and "}" not in rendered, template


@pytest.mark.parametrize(
    ("bank_key", "message_key", "template_index", "description", "expected"),
    [
        (
            "anime",
            "planned",
            1,
            "добавлено в список",
            "🗂️ <b>Title (аниме)</b> заняло своё место в очереди. "
            "Дождётся ли? Обязательно! Скоро ли? Ну, как повезет!",
        ),
        (
            "manga",
            "rewatching",
            0,
            "перечитываю",
            "🔁 WorgaNomoR перечитывает <b>Title (манга)</b>. "
            "Значит, она того стоила.",
        ),
        (
            "manga",
            "completed_no_score",
            1,
            "прочитано",
            "🏁 <b>Title (манга)</b> — прочитана. "
            "WorgaNomoR ставит точку без комментариев.",
        ),
        (
            "manga",
            "completed_no_score",
            9,
            "прочитано",
            "📚 <b>Title (манга)</b> прочитана. "
            "WorgaNomoR приберёг оценку, видимо.",
        ),
        (
            "manga",
            "completed_score_low",
            5,
            "прочитано и оценено на 3",
            "🔥 <b>Title (манга)</b> — 3/10. "
            "Сожжена, забыта, не рекомендуется.",
        ),
        (
            "manga",
            "completed_score_mid",
            6,
            "прочитано и оценено на 6",
            "⚖️ 6/10 за <b>Title (манга)</b>. "
            "Прочитана, оценена, забыта к утру.",
        ),
        (
            "manga",
            "score_changed",
            1,
            "изменена оценка с 5 на 5",
            "🤔 Оценка <b>Title (манга)</b> пересмотрена: 5 → 5. "
            "WorgaNomoR явно что-то переосмыслил.",
        ),
        (
            "manga",
            "score_changed_up",
            1,
            "изменена оценка с 4 на 9",
            "✨ Оценка <b>Title (манга)</b> выросла с 4 до 9. "
            "Со временем впечатление стало лучше — и WorgaNomoR это оценил.",
        ),
    ],
)
def test_history_media_agreement_uses_independent_expected_sentences(
    bank_key,
    message_key,
    template_index,
    description,
    expected,
):
    templates = (
        messages.MESSAGES[message_key]
        if message_key in SHARED_HISTORY_CASES
        else messages.MESSAGES[bank_key][message_key]
    )
    selected = templates[template_index]
    change = extract_score_change(description) or ("?", "?")
    score = extract_score(description)
    rendered = render_message_template(
        selected,
        FALLBACK_NAME_CONTEXT,
        media_key=bank_key,
        title="Title",
        url="",
        score=score if score is not None else "?",
        old=change[0],
        new=change[1],
        description=description,
    )

    assert rendered == expected


NEW_RANOBE_EXPECTED = {
    "planned": [
        "📖 <b><a href=\"https://shikimori.io/mangas/1-book\">Книга</a> "
        "(ранобэ)</b> пополнило планы WorgaNomoR. Для хорошего ранобэ место "
        "в очереди найдётся.",
        "🗒️ WorgaNomoR записал <b><a href=\"https://shikimori.io/mangas/1-book\">"
        "Книга</a> (ранобэ)</b> в книжную очередь. Для нового ранобэ место нашлось.",
    ],
    "watching": [
        "🔦 <b><a href=\"https://shikimori.io/mangas/1-book\">Книга</a> "
        "(ранобэ)</b> уже открыто у WorgaNomoR. Похоже, это ранобэ украдёт не один вечер.",
        "📕 WorgaNomoR начал знакомство с <b><a href=\"https://shikimori.io/mangas/1-book\">"
        "Книга</a> (ранобэ)</b>. Вечер официально отдан новому ранобэ.",
    ],
    "rewatching": [
        "📖 <b><a href=\"https://shikimori.io/mangas/1-book\">Книга</a> "
        "(ранобэ)</b> снова в руках у WorgaNomoR. Хорошее ранобэ при перечитывании "
        "только богатеет.",
        "🔄 WorgaNomoR открыл <b><a href=\"https://shikimori.io/mangas/1-book\">"
        "Книга</a> (ранобэ)</b> ещё раз. Это ранобэ явно не отпустило после первого прочтения.",
    ],
    "on_hold": [
        "📕 <b><a href=\"https://shikimori.io/mangas/1-book\">Книга</a> "
        "(ранобэ)</b> ждёт возвращения WorgaNomoR. Даже увлекательное ранобэ "
        "иногда приходится отложить.",
        "⏳ WorgaNomoR оставил <b><a href=\"https://shikimori.io/mangas/1-book\">"
        "Книга</a> (ранобэ)</b> до более подходящего вечера. Ранобэ никуда не денется.",
    ],
    "dropped": [
        "📕 <b><a href=\"https://shikimori.io/mangas/1-book\">Книга</a> "
        "(ранобэ)</b> не прошло проверку WorgaNomoR. Не всякое ранобэ добирается "
        "до последней страницы.",
        "🧹 WorgaNomoR убрал <b><a href=\"https://shikimori.io/mangas/1-book\">"
        "Книга</a> (ранобэ)</b> с книжной полки. Для этого ранобэ чтение закончилось "
        "раньше финала.",
    ],
    "completed_no_score": [
        "📘 WorgaNomoR добрался до финала <b><a href=\"https://shikimori.io/mangas/1-book\">"
        "Книга</a> (ранобэ)</b>. Ранобэ дочитано, оценка ещё обдумывается.",
        "📝 <b><a href=\"https://shikimori.io/mangas/1-book\">Книга</a> "
        "(ранобэ)</b> закончено, а WorgaNomoR пока хранит молчание. Для этого ранобэ "
        "вердикт ещё не написан.",
    ],
    "completed_score_low": [
        "🗑️ WorgaNomoR оценил <b><a href=\"https://shikimori.io/mangas/1-book\">"
        "Книга</a> (ранобэ)</b> на 3/10. Это ранобэ не спас даже финальный том.",
        "📕 <b><a href=\"https://shikimori.io/mangas/1-book\">Книга</a> "
        "(ранобэ)</b> получило от WorgaNomoR всего 3/10. К этому ранобэ WorgaNomoR "
        "точно не вернётся.",
    ],
    "completed_score_mid": [
        "📚 WorgaNomoR поставил <b><a href=\"https://shikimori.io/mangas/1-book\">"
        "Книга</a> (ранобэ)</b> 6/10. Нормальное ранобэ, но без места на любимой полке.",
        "📝 <b><a href=\"https://shikimori.io/mangas/1-book\">Книга</a> "
        "(ранобэ)</b> получило 6/10 от WorgaNomoR. Это ранобэ оказалось ровно на один раз.",
    ],
    "completed_score_high": [
        "📚 WorgaNomoR оценил <b><a href=\"https://shikimori.io/mangas/1-book\">"
        "Книга</a> (ранобэ)</b> на 9/10. Ранобэ уверенно поселилось в памяти.",
        "💛 <b><a href=\"https://shikimori.io/mangas/1-book\">Книга</a> "
        "(ранобэ)</b> получило от WorgaNomoR 9/10. К этому ранобэ приятно будет вернуться.",
    ],
    "completed_score_perfect": [
        "🌠 WorgaNomoR отдал <b><a href=\"https://shikimori.io/mangas/1-book\">"
        "Книга</a> (ранобэ)</b> безоговорочные 10/10. Это ранобэ попало точно в сердце.",
        "📚 <b><a href=\"https://shikimori.io/mangas/1-book\">Книга</a> "
        "(ранобэ)</b> получило десятку от WorgaNomoR. Ранобэ отправляется на самую "
        "почётную полку.",
    ],
}

RANOBE_BANK_SIZES_AFTER_EXPANSION = {
    "planned": 8,
    "watching": 8,
    "rewatching": 8,
    "on_hold": 6,
    "dropped": 8,
    "completed_no_score": 8,
    "completed_score_low": 8,
    "completed_score_mid": 8,
    "completed_score_high": 8,
    "completed_score_perfect": 8,
}


def test_new_ranobe_templates_match_independent_final_sentences():
    for message_key, expected_messages in NEW_RANOBE_EXPECTED.items():
        templates = messages.MESSAGES["ranobe"][message_key]
        assert len(templates) == RANOBE_BANK_SIZES_AFTER_EXPANSION[message_key]
        description = _history_description("ranobe", message_key)
        for template, expected in zip(templates[-2:], expected_messages):
            assert "{n" in template
            score = extract_score(description)
            rendered = render_message_template(
                template,
                FALLBACK_NAME_CONTEXT,
                media_key="ranobe",
                title="Книга",
                url="/mangas/1-book",
                score=score if score is not None else "?",
            )

            assert rendered == expected


@pytest.mark.parametrize(
    "bank_key", ["score_changed", "score_changed_up", "score_changed_down"],
)
def test_score_changed_banks_match_independent_expected_messages(monkeypatch, bank_key):
    monkeypatch.setattr(messages, "DISPLAY_NAME_CONTEXT", FALLBACK_NAME_CONTEXT)
    templates = messages.MESSAGES[bank_key]
    rendered = {
        messages.format_name_template(
            template,
            FALLBACK_NAME_CONTEXT,
            title="Ergo Proxy",
            old=4,
            new=9,
        )
        for template in templates
    }
    assert rendered == expected_score_changed_messages(bank_key, 4, 9)


@pytest.mark.parametrize("bank_key", ["score_changed_up", "score_changed_down"])
def test_score_changed_direction_templates_keep_required_fields(bank_key):
    required_fields = {"title", "old", "new"}
    for template in messages.MESSAGES[bank_key]:
        fields = {
            field_name
            for _, field_name, _, _ in Formatter().parse(template)
            if field_name
        }
        assert required_fields <= fields


def test_html_title_escape(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry(
            "оценено на 8",
            "<Ergo & Proxy>"
        )
    )

    assert "&lt;Ergo &amp; Proxy&gt;" in msg


# ==========================================================
# links
# ==========================================================

def test_message_contains_shikimori_link(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry("оценено на 8")
    )

    assert '<a href="' in msg
    assert "ergo-proxy" in msg


def test_message_without_url(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry(
            "оценено на 8",
            url=""
        )
    )

    assert '<a href="' not in msg


# ── экранирование DISPLAY_NAME в HTML-шаблонах (Codacy MEDIUM) ──────

def test_display_name_html_constant_is_escaped():
    """DISPLAY_NAME из env экранируется для HTML — иначе < > & в имени → Telegram 400."""
    import config
    assert messages._DISPLAY_NAME_HTML == h(config.DISPLAY_NAME)


def test_favourite_message_uses_escaped_name(monkeypatch):
    monkeypatch.setattr(
        messages,
        "DISPLAY_NAME_CONTEXT",
        build_display_name_context("Ампер&Санд", "none"),
    )
    monkeypatch.setattr(random, "choice", fixed_choice)
    item = {"id": 1, "name": "X", "russian": "Икс", "url": None}
    text = messages.build_favourite_message("animes", item)
    assert "Ампер&amp;Санд" in text


def test_broadcast_header_escapes_special_chars(monkeypatch):
    import importlib

    import config
    monkeypatch.setattr(config, "DISPLAY_NAME", "A<b>&Co", raising=False)
    importlib.reload(messages)
    try:
        assert "A&lt;b&gt;&amp;Co" in messages.BROADCAST_HEADER
        assert "A<b>&Co" not in messages.BROADCAST_HEADER
    finally:
        monkeypatch.undo()
        importlib.reload(messages)
# ==========================================================
# build_startup_snapshot — стартовый health-снапшот (owner-gate)
# ==========================================================
def _snap(**over):
    base = dict(
        display_name="Пётр", shiki_user="WNR", check_interval_sec=600,
        subscriber_count=3, seen_ids_count=1240, seen_favs_count=37,
        stats_updated_at=_utcnow().isoformat(), last_backup_at=time.time(),
    )
    base.update(over)
    return build_startup_snapshot(**base)


def test_startup_snapshot_normal_state():
    txt = _snap()
    assert txt.startswith("🟢 Бот запущен")
    assert "Имя: Пётр" in txt and "Шики-логин: WNR" in txt
    assert "проверка каждые 10 мин" in txt          # 600 сек -> 10 мин
    assert "Подписчиков: 3" in txt
    assert "история 1240" in txt and "избранное 37" in txt
    assert "события за простой догоним" in txt
    assert "Последняя синхронизация статистики:" in txt
    assert "нет данных" not in txt                   # обе метки свежие


def test_startup_snapshot_full_wipe_collapses_to_banner():
    txt = _snap(subscriber_count=0, seen_ids_count=0, seen_favs_count=0,
                stats_updated_at=None, last_backup_at=None)
    assert "Чистый инстанс" in txt
    assert "не догоним" in txt
    assert "нет данных" not in txt                   # схлопнуто в один баннер
    assert "🗂 Отслеживание:" not in txt             # обычной строки отслеживания нет
    assert "Последняя синхронизация статистики:" not in txt


def test_startup_snapshot_tracking_not_initialized_but_stats_present():
    # seen_ids пусто, но stats_all есть -> не вайп, а предупреждение
    txt = _snap(seen_ids_count=0, stats_updated_at=_utcnow().isoformat(),
                last_backup_at=None)
    assert "⚠️ Отслеживание не инициализировано" in txt
    assert "уйдут в тишину" in txt
    assert "Чистый инстанс" not in txt
    assert "Последняя синхронизация статистики:" in txt
    assert "💾 Последний плановый бэкап: нет данных" in txt    # бэкапа не было


def test_startup_snapshot_survives_bad_timestamps():
    txt = _snap(stats_updated_at="не-дата", last_backup_at="тоже-не-число")
    # кривые метки не роняют билдер, деградируют в 'нет данных'
    assert "🟢 Бот запущен" in txt
    assert "нет данных" in txt


def test_startup_snapshot_normalizes_aware_stats_timestamp_to_utc():
    txt = _snap(stats_updated_at="2026-08-06T15:12:30+03:00")

    assert "06.08.2026 12:12" in txt
    assert "06.08.2026 15:12" not in txt


# ==========================================================
# _strip_html
# ==========================================================

def test_strip_html_bold():
    assert _strip_html("оценено на <b>7</b>") == "оценено на 7"


def test_strip_html_strong():
    assert _strip_html("оценено на <strong>8</strong>") == "оценено на 8"


def test_strip_html_multiple_tags():
    assert (
        _strip_html("изменена оценка с <b>5</b> на <i>9</i>")
        == "изменена оценка с 5 на 9"
    )


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (None, ""),
        (0, "0"),
        (False, "False"),
        (7, "7"),
        ({"unexpected": True}, "{'unexpected': True}"),
    ],
)
def test_clean_description_normalizes_non_string(description, expected):
    assert clean_description(description) == expected


# ==========================================================
# extract_score
# ==========================================================

def test_extract_score_ru():
    assert extract_score("оценено на 9") == 9


def test_extract_score_alt_ru_male():
    assert extract_score("выставил оценку 8") == 8


def test_extract_score_alt_ru_female():
    assert extract_score("выставила оценку 6") == 6


def test_extract_score_rated():
    assert extract_score("rated 7") == 7


def test_extract_score_scored():
    assert extract_score("scored 10") == 10


def test_extract_score_html_bold():
    assert extract_score("оценено на <b>7</b>") == 7


def test_extract_score_html_strong():
    assert extract_score("оценено на <strong>8</strong>") == 8


def test_extract_score_invalid():
    assert extract_score("какой-то текст") is None


def test_extract_score_empty():
    assert extract_score("") is None


# ==========================================================
# extract_score_change
# ==========================================================

def test_extract_score_change_ru():
    assert extract_score_change(
        "изменена оценка с 5 на 9"
    ) == (5, 9)


def test_extract_score_change_html():
    assert extract_score_change(
        "изменена оценка с <b>5</b> на <b>9</b>"
    ) == (5, 9)


def test_extract_score_change_latin_c_homoglyph():
    # Shikimori шлёт латинскую "c" (U+0063), не кириллическую "с" (U+0441);
    # реальная строка ещё и оборачивает оценки в <b>. Регресс на "?/10 вместо ?".
    assert extract_score_change(
        "Изменена оценка c <b>6</b> на <b>7</b>"
    ) == (6, 7)


def test_extract_score_change_invalid():
    assert extract_score_change(
        "изменена оценка"
    ) is None


def test_extract_score_homoglyph_in_russian_word():
    # Латинская "о" (U+006f) внутри «оценено» — mixed-script, чинится.
    assert extract_score("\u006fценено на 9") == 9


def test_classify_event_homoglyph_in_russian_word():
    # Латинская "o" в «брошено» — без нормализации классификатор промахнётся.
    assert classify_event("бр\u006fшено") == "dropped"


# ==========================================================
# classify_event
# ==========================================================

def test_classify_score_changed():
    assert classify_event(
        "изменена оценка с 5 на 8"
    ) == "score_changed"


def test_classify_watching_smotryu():
    assert classify_event("смотрю") == "watching"


def test_classify_watching_smotrit():
    assert classify_event("смотрит") == "watching"


def test_classify_watching_chitayu():
    assert classify_event("читаю") == "watching"


def test_classify_watching_reading():
    assert classify_event("reading") == "watching"


def test_classify_rewatching_ru():
    assert classify_event("пересматриваю") == "rewatching"


def test_classify_rereading_ru():
    assert classify_event("перечитываю") == "rewatching"


def test_classify_rewatching_en():
    assert classify_event("rewatching") == "rewatching"


def test_classify_planned():
    assert classify_event("добавлено в список") == "planned"


def test_classify_planned_english():
    assert classify_event("planned") == "planned"


def test_classify_dropped():
    assert classify_event("брошено") == "dropped"


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_classify_real_on_hold_fixture(history_event_fixtures, locale):
    description = history_event_fixtures["on_hold"][locale]["entry"]["description"]
    assert classify_event(description) == "on_hold"


def test_classify_completed_without_score():
    assert classify_event("просмотрено") == "completed"


def test_classify_completed_with_score():
    assert classify_event("Просмотрено и оценено на <b>8</b>") == "completed"


def test_classify_live_completion_descriptions(history_event_fixtures):
    for description in history_event_fixtures["live_ru_descriptions"]["completed"]:
        assert classify_event(description) == "completed"


def test_classify_live_score_descriptions(history_event_fixtures):
    for description in history_event_fixtures["live_ru_descriptions"]["score_set"]:
        assert classify_event(description) == "score_set"
    for description in history_event_fixtures["live_ru_descriptions"]["score_changed"]:
        assert classify_event(description) == "score_changed"


def test_classify_live_progress_descriptions_as_ignored(history_event_fixtures):
    for description in history_event_fixtures["live_ru_descriptions"]["progress"]:
        assert classify_event(description) == "ignored"


def test_classify_official_non_product_descriptions_as_ignored(history_event_fixtures):
    for description in history_event_fixtures["official_ignored_descriptions"]:
        assert classify_event(description) == "ignored"


def test_classify_rating_removal_as_silent_state_correction(history_event_fixtures):
    for description in history_event_fixtures["score_removed_descriptions"]:
        assert classify_event(description) == "score_removed"


@pytest.mark.parametrize("description", [
    "Completed",
    "Completed and rated 8",
])
def test_classify_official_english_completion_descriptions(description):
    assert classify_event(description) == "completed"


@pytest.mark.parametrize("description", ["Rated 8", "Scored 8"])
def test_classify_official_english_score_descriptions(description):
    assert classify_event(description) == "score_set"


def test_ignored_history_message_is_empty():
    assert build_message(make_entry("Просмотрено 15 эпизодов")) == ""


def test_score_removed_history_message_is_empty():
    assert build_message(make_entry("Отменена оценка")) == ""


def test_classify_unknown_is_explicit():
    assert classify_event("неизвестный новый формат") == "unknown"


# ============================================================
# format_rate_entry()
# ============================================================

def test_format_rate_entry_russian_title_priority():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Ergo Proxy",
            "russian": "Эрго Прокси",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "Эрго Прокси" in result
    assert "Ergo Proxy" not in result


def test_format_rate_entry_fallback_to_english():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Ergo Proxy",
            "russian": "",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "Ergo Proxy" in result


def test_format_rate_entry_html_escape():
    item = {
        "_status": "watching",
        "anime": {
            "name": "<Ergo & Proxy>",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "&lt;Ergo &amp; Proxy&gt;" in result


def test_format_rate_entry_watching_icon():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert result.startswith("▶️")


def test_format_rate_entry_rewatching_icon():
    item = {
        "_status": "rewatching",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert result.startswith("🔁")


def test_format_rate_entry_unknown_icon():
    item = {
        "_status": "something",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert result.startswith("•")


def test_format_rate_entry_with_link():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Anime",
            "url": "/animes/1-anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert 'href="' in result
    assert "/animes/1-anime" in result


def test_format_rate_entry_without_link():
    item = {
        "_status": "watching",
        "anime": {
            "name": "Anime",
        },
    }

    result = format_rate_entry(item, "anime")

    assert "href=" not in result


# ============================================================
# Message building
# ============================================================

def test_build_favourite_message_prefers_russian():
    item = {
        "russian": "Эрго Прокси",
        "name": "Ergo Proxy",
    }

    msg = build_favourite_message("animes", item)

    assert "Эрго Прокси" in msg


def test_build_favourite_message_english_fallback():
    item = {
        "name": "Ergo Proxy",
    }

    msg = build_favourite_message("animes", item)

    assert "Ergo Proxy" in msg


def test_build_favourite_message_html_escape():
    item = {
        "name": "<Ergo & Proxy>",
    }

    msg = build_favourite_message("animes", item)

    assert "&lt;Ergo &amp; Proxy&gt;" in msg


def test_build_favourite_message_link():
    item = {
        "name": "Ergo Proxy",
        "url": "/animes/790-ergo-proxy",
    }

    msg = build_favourite_message("animes", item)

    assert "shikimori.io/animes/790-ergo-proxy" in msg


def test_build_favourite_message_ranobe_uses_dedicated_bank():
    relative_url = "/mangas/74697-re-zero"
    item = {
        "id": 74697,
        "name": "Re:Zero",
        "russian": "Re:Zero",
        "url": relative_url,
    }
    linked_title = f'<a href="{messages.SHIKI_BASE_URL}{relative_url}">Re:Zero</a>'
    ranobe_title = f"{linked_title} (ранобэ)"
    manga_title = f"{linked_title} (манга)"
    ranobe_bank = {
        messages.format_name_template(
            template,
            messages.DISPLAY_NAME_CONTEXT,
            title=ranobe_title,
        )
        for template in messages.MESSAGES["favourites"]["ranobe"]
    }
    manga_bank = {
        messages.format_name_template(
            template,
            messages.DISPLAY_NAME_CONTEXT,
            title=manga_title,
        )
        for template in messages.MESSAGES["favourites"]["manga"]
    }

    ranobe_text = messages.build_favourite_message("ranobe", item)
    manga_text = messages.build_favourite_message("mangas", item)

    assert ranobe_text in ranobe_bank
    assert manga_text in manga_bank
    assert ranobe_text.count(messages.SHIKI_BASE_URL) == 1
    assert manga_text.count(messages.SHIKI_BASE_URL) == 1


@pytest.mark.parametrize(
    "context",
    FINAL_RENDER_CONTEXTS,
    ids=["male", "female", "fallback"],
)
def test_every_favourite_template_uses_expected_media_label(
    context,
):
    media_cases = {
        "anime": ("аниме", "/animes/1-audit"),
        "manga": ("манга", "/mangas/1-audit"),
        "ranobe": ("ранобэ", "/mangas/1-audit"),
    }

    for bank_key, (label, url) in media_cases.items():
        for template in messages.MESSAGES["favourites"][bank_key]:
            rendered = render_message_template(
                template,
                context,
                media_key=bank_key,
                title="AuditTitle",
                url=url,
            )
            plain = _strip_html(rendered)

            assert rendered.count(f"({label})") == 1, template
            assert rendered.count(messages.SHIKI_BASE_URL) == 1, template
            assert "{" not in rendered and "}" not in rendered, template
            assert re.search(
                rf"(?iu)\b(?:аниме|манг\w*|ранобэ)\s+"
                rf"AuditTitle \({label}\)",
                plain,
            ) is None, template

    for bank_key in ("character", "person"):
        for template in messages.MESSAGES["favourites"][bank_key]:
            rendered = render_message_template(
                template,
                context,
                media_key=bank_key,
                title="AuditTitle",
                url="/people/1-audit",
            )

            assert "(аниме)" not in rendered, template
            assert "(манга)" not in rendered, template
            assert "(ранобэ)" not in rendered, template
            assert rendered.count(messages.SHIKI_BASE_URL) == 1, template
            assert _strip_html(rendered).count("AuditTitle") == 1, template


MANGA_FAVOURITE_REWRITES = {
    0: "⭐ WorgaNomoR добавил <b><a href=\"https://shikimori.io/mangas/25-berserk\">"
       "Berserk</a> (манга)</b> в избранное. Художник может гордиться.",
    2: "🏅 Особая отметка: <b><a href=\"https://shikimori.io/mangas/25-berserk\">"
       "Berserk</a> (манга)</b> в избранном WorgaNomoR. Это не просто хорошо.",
    6: "🌟 WorgaNomoR занёс <b><a href=\"https://shikimori.io/mangas/25-berserk\">"
       "Berserk</a> (манга)</b> в избранное. Высшая полка, рядом с любимыми.",
}


RANOBE_FAVOURITE_REWRITES = [
    "⭐ WorgaNomoR добавил <b><a href=\"https://shikimori.io/mangas/74697-re-zero\">"
    "Re:Zero</a> (ранобэ)</b> в избранное. Это ранобэ действительно запало в душу.",
    "💫 <b><a href=\"https://shikimori.io/mangas/74697-re-zero\">Re:Zero</a> "
    "(ранобэ)</b> теперь в избранном у WorgaNomoR. Место на любимой книжной полке заслужено.",
    "🏅 Особая отметка: <b><a href=\"https://shikimori.io/mangas/74697-re-zero\">"
    "Re:Zero</a> (ранобэ)</b> попало в избранное WorgaNomoR. Это дорогого стоит.",
    "✨ WorgaNomoR выделил <b><a href=\"https://shikimori.io/mangas/74697-re-zero\">"
    "Re:Zero</a> (ранобэ)</b> среди всех прочитанных историй.",
    "🌟 <b><a href=\"https://shikimori.io/mangas/74697-re-zero\">Re:Zero</a> "
    "(ранобэ)</b> — в избранном. Значит, это ранобэ не закончилось с последней страницей.",
    "📚 WorgaNomoR занёс <b><a href=\"https://shikimori.io/mangas/74697-re-zero\">"
    "Re:Zero</a> (ранобэ)</b> на любимую книжную полку.",
    "❤️ <b><a href=\"https://shikimori.io/mangas/74697-re-zero\">Re:Zero</a> "
    "(ранобэ)</b> зацепило WorgaNomoR по-настоящему — прямиком в избранное.",
    "🔖 <b><a href=\"https://shikimori.io/mangas/74697-re-zero\">Re:Zero</a> "
    "(ранобэ)</b> стало одним из любимых у WorgaNomoR. Такие ранобэ остаются надолго.",
]


def test_rewritten_favourite_templates_match_independent_sentences():
    for index, expected in MANGA_FAVOURITE_REWRITES.items():
        selected = messages.MESSAGES["favourites"]["manga"][index]
        rendered = render_message_template(
            selected,
            FALLBACK_NAME_CONTEXT,
            media_key="manga",
            title="Berserk",
            url="/mangas/25-berserk",
        )
        assert rendered == expected

    for selected, expected in zip(
        messages.MESSAGES["favourites"]["ranobe"],
        RANOBE_FAVOURITE_REWRITES,
    ):
        rendered = render_message_template(
            selected,
            FALLBACK_NAME_CONTEXT,
            media_key="ranobe",
            title="Re:Zero",
            url="/mangas/74697-re-zero",
        )
        assert rendered == expected


def test_every_ranobe_template_uses_title_and_avoids_manga_wording():
    history_templates = [
        template
        for templates in messages.MESSAGES["ranobe"].values()
        for template in templates
    ]
    favourite_templates = messages.MESSAGES["favourites"]["ranobe"]

    for template in [*history_templates, *favourite_templates]:
        lowered = template.lower()
        assert "{title}" in template, template
        assert "манг" not in lowered, template
        assert "художник" not in lowered, template


def test_build_favourite_message_industry_uses_person_bank():
    item = {"id": 34785, "name": "Rie Takahashi", "russian": "Риэ Такахаси", "url": None}
    for cat in ("seyu", "mangakas", "producers", "people"):
        text = messages.build_favourite_message(cat, item)
        person_bank = [
            messages.format_name_template(
                template,
                messages.DISPLAY_NAME_CONTEXT,
                title="Риэ Такахаси",
            )
            for template in messages.MESSAGES["favourites"]["person"]
        ]
        assert text in person_bank, f"категория {cat} ушла не в банк person"


def _all_message_templates():
    for value in messages.MESSAGES.values():
        if isinstance(value, list):
            yield from value
            continue
        for nested in value.values():
            if isinstance(nested, list):
                yield from nested
                continue
            for templates in nested.values():
                yield from templates


@pytest.mark.parametrize("gender", ["male", "female", None])
def test_every_message_template_renders_with_independent_name_forms(gender):
    context = DisplayNameContext(
        nominative="ИМ",
        genitive="РОД",
        dative="ДАТ",
        accusative="ВИН",
        instrumental="ТВОР",
        gender=gender,
        inflection_applied=True,
    )
    allowed_fields = {
        "n",
        "n_gen",
        "n_dat",
        "n_acc",
        "n_ins",
        "g",
        "title",
        "score",
        "old",
        "new",
        "description",
    }

    for template in _all_message_templates():
        fields = {
            field_name
            for _, field_name, _, _ in Formatter().parse(template)
            if field_name
        }
        assert fields <= allowed_fields, template
        rendered = messages.format_name_template(
            template,
            context,
            title="ТАЙТЛ",
            score=8,
            old=4,
            new=8,
            description="ОПИСАНИЕ",
        )
        assert "{" not in rendered and "}" not in rendered, template
        for field, sentinel in {
            "n": "ИМ",
            "n_gen": "РОД",
            "n_dat": "ДАТ",
            "n_acc": "ВИН",
            "n_ins": "ТВОР",
        }.items():
            if field in fields:
                assert sentinel in rendered, template


def test_kostya_regressions_cover_every_required_case():
    context = build_display_name_context("Костя")
    rendered_bank = "\n".join(
        messages.format_name_template(
            template,
            context,
            title="ТАЙТЛ",
            score=8,
            old=4,
            new=8,
            description="ОПИСАНИЕ",
        )
        for template in _all_message_templates()
    )

    assert "в руках у Кости" in rendered_bank
    assert "не потрясло мир Кости" in rendered_bank
    assert "Мнение Кости — тайна" in rendered_bank
    assert "зацепило Костю" in rendered_bank
    assert "легла Косте на душу" in rendered_bank
    assert "не пережило встречи с Костей" in rendered_bank


def test_female_context_selects_female_surrounding_grammar():
    context = build_display_name_context("Анна")
    samples = [
        "{n} {g:посмотрел|посмотрела} <b>{title}</b>.",
        "{n} {g:доволен|довольна}.",
        "И это {g:его|её} право.",
        "{n} {g:дочитал|дочитала} и {g:уставился|уставилась} в стену.",
    ]

    assert [
        messages.format_name_template(template, context, title="ТАЙТЛ")
        for template in samples
    ] == [
        "Анна посмотрела <b>ТАЙТЛ</b>.",
        "Анна довольна.",
        "И это её право.",
        "Анна дочитала и уставилась в стену.",
    ]
