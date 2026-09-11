# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистый доменный слой и presentation-контракты публичных списков."""

from copy import deepcopy
from html.parser import HTMLParser

import pytest

import lists
from report_model import (
    Heading,
    Italic,
    Line,
    Table,
    Text,
    Title,
    render_report,
    rendered_html,
)
from rich_report import render_rich_report


def _stats() -> dict:
    return {
        "updated_at": "2026-09-10T00:00:00+00:00",
        "anime": {"titles": {}, "aggregates": {}},
        "manga": {"titles": {}, "aggregates": {}},
    }


def _record(
    title: str,
    *,
    status: object = "completed",
    score: object = 0,
    kind: object = "tv",
    url: object = "",
    comment: object = None,
    **metadata,
) -> dict:
    return {
        "title": title,
        "status": status,
        "score": score,
        "kind": kind,
        "url": url,
        "comment": comment,
        **metadata,
    }


def _titles(report) -> list[str]:
    result = []
    for logical_unit in report.units:
        for logical_section in logical_unit.sections:
            for item in logical_section.items:
                if isinstance(item, Line):
                    result.extend(
                        part.text
                        for part in item.parts
                        if isinstance(part, Title)
                    )
                elif isinstance(item, Table):
                    result.extend(
                        part.text
                        for group in item.groups
                        for row in group.rows
                        for cell in row.cells
                        for part in cell.parts
                        if isinstance(part, Title)
                    )
    return result


def _headings(report) -> list[str]:
    return [
        "".join(getattr(part, "value", getattr(part, "text", "")) for part in item.parts)
        for logical_unit in report.units
        for logical_section in logical_unit.sections
        for item in logical_section.items
        if isinstance(item, Heading)
    ]


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _visible(markup: str) -> str:
    parser = _VisibleText()
    parser.feed(markup)
    parser.close()
    return "".join(parser.parts)


def test_declarative_registries_define_the_complete_public_surface():
    assert [definition.key for definition in lists.LIST_MEDIA_DEFINITIONS] == [
        "anime",
        "manga",
        "ranobe",
        "combined",
    ]
    assert [definition.key for definition in lists.LIST_VIEW_DEFINITIONS] == [
        "completed",
        "planned",
        "all",
    ]
    assert lists.LIST_VIEW_BY_KEY["all"].label == "📋 Полный список"
    assert lists.LIST_MEDIA_BY_KEY["combined"].terminal is True


def test_completed_and_planned_use_exact_normalized_statuses():
    stats = _stats()
    stats["anime"]["titles"] = {
        "1": _record("Completed", status=" COMPLETED "),
        "2": _record("Planned", status="PLANNED"),
        "3": _record("Future", status="completed_later"),
        "4": _record("Missing", status=None),
    }

    completed = lists.build_list_report(
        stats,
        "anime",
        "completed",
        base_url="https://shikimori.io",
    )
    planned = lists.build_list_report(
        stats,
        "anime",
        "planned",
        base_url="https://shikimori.io",
    )
    all_by_status = lists.build_list_report(
        stats,
        "anime",
        "all",
        base_url="https://shikimori.io",
    )

    assert _titles(completed) == ["Completed"]
    assert _titles(planned) == ["Planned"]
    assert sorted(_titles(all_by_status)) == [
        "Completed",
        "Future",
        "Missing",
        "Planned",
    ]
    assert "⚠️ Статус не определён · 2" in _headings(all_by_status)


@pytest.mark.parametrize(
    "count",
    [1, 2, 5],
)
def test_status_heading_uses_report_style_and_bare_count(count):
    stats = _stats()
    stats["anime"]["titles"] = {
        str(index): _record(f"Title {index}")
        for index in range(count)
    }

    report = lists.build_list_report(
        stats,
        "anime",
        "completed",
        base_url="https://shikimori.io",
    )

    assert f"✅ Просмотрено · {count}" in _headings(report)


def test_sorting_uses_positive_score_then_normalized_title_and_stable_id():
    stats = _stats()
    stats["anime"]["titles"] = {
        "10": _record("Бета", score=8),
        "2": _record("Альфа", score=10),
        "4": _record("  Ａ  title ", score=0),
        "3": _record("a title", score=None),
        "1": _record("a title", score=11),
        "5": _record("Invalid float", score=8.0),
    }

    report = lists.build_list_report(
        stats,
        "anime",
        "completed",
        base_url="https://shikimori.io",
    )

    assert _titles(report) == [
        "Альфа",
        "Бета",
        "a title",
        "a title",
        "A title",
        "Invalid float",
    ]
    assert "10⭐" in rendered_html(report)[0]
    assert "11⭐" not in rendered_html(report)[0]


def test_combined_partition_is_exhaustive_and_uses_canonical_classifier(
    monkeypatch,
):
    stats = _stats()
    stats["anime"]["titles"] = {
        "1": _record("Anime"),
        "2": "broken anime record",
    }
    stats["manga"]["titles"] = {
        "3": _record("Manga", kind="manhwa"),
        "4": _record("Ranobe", kind="light_novel"),
        "5": _record("Future kind", kind="future_kind"),
        "6": None,
    }
    original = lists.classify_manga_presentation_kind
    calls = []

    def classify(kind):
        calls.append(kind)
        return original(kind)

    monkeypatch.setattr(lists, "classify_manga_presentation_kind", classify)
    before = deepcopy(stats)

    report = lists.build_list_report(
        stats,
        "combined",
        "all",
        base_url="https://shikimori.io",
    )

    assert len(report.units) == 4
    assert calls == ["manhwa", "light_novel", "future_kind", None]
    assert set(_titles(report)) == {
        "Anime",
        "Без названия (ID 2)",
        "Без названия (ID 6)",
        "Future kind",
        "Manga",
        "Ranobe",
    }
    assert len(_titles(report)) == len(set(_titles(report)))
    assert stats == before


def test_combined_omits_empty_unresolved_category():
    stats = _stats()
    stats["anime"]["titles"] = {"1": _record("Anime")}
    stats["manga"]["titles"] = {
        "2": _record("Manga", kind="manga"),
        "3": _record("Ranobe", kind="novel"),
    }

    report = lists.build_list_report(
        stats,
        "combined",
        "all",
        base_url="https://shikimori.io",
    )

    assert len(report.units) == 3
    assert all("НЕ ОПРЕДЕЛЕНО" not in heading for heading in _headings(report))


def test_unknown_manga_kinds_are_disclosed_but_not_guessed():
    stats = _stats()
    stats["manga"]["titles"] = {
        "1": _record("Manga", kind="manga"),
        "2": _record("Ranobe", kind="novel"),
        "3": _record("Unknown", kind="future"),
        "4": object(),
    }

    manga = lists.build_list_report(
        stats,
        "manga",
        "all",
        base_url="https://shikimori.io",
    )
    ranobe = lists.build_list_report(
        stats,
        "ranobe",
        "all",
        base_url="https://shikimori.io",
    )

    assert _titles(manga) == ["Manga"]
    assert _titles(ranobe) == ["Ranobe"]
    assert "Не удалось отнести к манге или ранобэ 2 тайтла" in rendered_html(manga)[0]
    assert "Не удалось отнести к манге или ранобэ 2 тайтла" in rendered_html(ranobe)[0]


def test_unknown_manga_kind_notice_respects_selected_status():
    stats = _stats()
    stats["manga"]["titles"] = {
        "1": _record("Completed unknown", kind="future", status="completed"),
        "2": _record("Planned unknown", kind="future", status="planned"),
        "3": _record("Another planned unknown", kind="future", status="planned"),
    }

    completed = lists.build_list_report(
        stats,
        "manga",
        "completed",
        base_url="https://shikimori.io",
    )
    planned = lists.build_list_report(
        stats,
        "ranobe",
        "planned",
        base_url="https://shikimori.io",
    )

    assert "Не удалось отнести к манге или ранобэ 1 тайтл" in rendered_html(
        completed
    )[0]
    assert "Не удалось отнести к манге или ранобэ 2 тайтла" in rendered_html(
        planned
    )[0]


@pytest.mark.parametrize("domain", ["anime", "manga"])
def test_malformed_titles_container_is_not_presented_as_empty(domain):
    stats = _stats()
    stats[domain]["titles"] = ["broken"]

    report = lists.build_list_report(
        stats,
        "combined" if domain == "manga" else "anime",
        "all",
        base_url="https://shikimori.io",
    )
    text = "\n".join(rendered_html(report))

    assert "повреждён" in text
    assert "тайтлы этой категории прочитать нельзя" in text


@pytest.mark.parametrize("domain", ["anime", "manga"])
def test_absent_media_domain_keeps_normal_empty_state(domain):
    stats = _stats()
    del stats[domain]

    report = lists.build_list_report(
        stats,
        domain,
        "all",
        base_url="https://shikimori.io",
    )
    text = "\n".join(rendered_html(report))

    assert "повреждён" not in text
    assert "В этом разделе пока нет тайтлов" in text


def test_links_scores_and_hostile_multiline_comments_stay_safe_and_lossless():
    stats = _stats()
    hostile = "line <b>not bold</b> & [x](javascript:boom)\n" + "😀<&>" * 1500
    stats["anime"]["titles"] = {
        "1": _record(
            "First <title>",
            score=9,
            url="https://shikimori.one/animes/1-first",
        ),
        "2": _record(
            "Commented",
            score=8,
            url="/animes/2-commented",
            comment=hostile,
        ),
        "3": _record("Last & title", score=7, url="animes/3-last"),
    }
    report = lists.build_list_report(
        stats,
        "anime",
        "completed",
        base_url="https://shikimori.io",
    )

    chunks = render_report(report)
    markup = "".join(chunk.html for chunk in chunks)
    visible = "".join(_visible(chunk.html) for chunk in chunks)

    assert len(chunks) > 1
    assert all(chunk.visible_length <= 4096 for chunk in chunks)
    assert 'href="https://shikimori.io/animes/1-first"' in markup
    assert "shikimori.one" not in markup
    assert "<b>not bold</b>" not in markup
    assert "&lt;b&gt;not bold&lt;/b&gt;" in markup
    assert visible.count("First <title>") == 1
    assert visible.count("Commented") == 1
    assert visible.count("Last & title") == 1
    assert hostile in visible
    assert visible.index("Commented") < visible.index(hostile)
    assert visible.index(hostile) < visible.index("Last & title")


def test_unknown_registry_keys_are_rejected_without_guessing():
    with pytest.raises(ValueError, match="Неизвестное определение"):
        lists.build_list_report(
            _stats(),
            "anime",
            "future-view",
            base_url="https://shikimori.io",
        )


def test_comment_is_a_plain_text_line_directly_after_its_title():
    stats = _stats()
    stats["anime"]["titles"]["1"] = _record(
        "Title",
        comment="<details>\n* list\n[link](https://evil.test)",
    )

    report = lists.build_list_report(
        stats,
        "anime",
        "completed",
        base_url="https://shikimori.io",
    )
    table = next(
        item
        for logical_section in report.units[0].sections
        for item in logical_section.items
        if isinstance(item, Table)
    )
    group = table.groups[0]

    assert group.rows[0].cells[0].parts[0] == Text("1")
    assert isinstance(group.rows[0].cells[1].parts[0], Title)
    assert all(
        "<details>" not in str(part)
        for row in group.rows
        for cell in row.cells
        for part in cell.parts
    )
    assert group.after == (
        Line((
            Text("💬 "),
            Italic("<details>\n* list\n[link](https://evil.test)"),
        )),
    )
    payload = render_rich_report(report)[0].payload
    details = next(block for block in payload["blocks"] if block["type"] == "details")
    assert details["blocks"][0]["type"] == "table"
    assert details["blocks"][1]["type"] == "paragraph"
    assert details["blocks"][1]["text"][1]["type"] == "italic"
    assert details["blocks"][1]["text"][1]["text"] == (
        "<details>\n* list\n[link](https://evil.test)"
    )


def test_catalog_table_contains_counts_metadata_and_report_navigation():
    stats = _stats()
    stats["anime"]["titles"] = {
        "1": _record(
            "Detailed",
            score=9,
            kind="tv",
            url="/animes/1-detailed",
            year=2024,
            shiki_score=8.75,
            release_status="released",
            episodes_watched=12,
            episodes_total=24,
            duration=24,
            rewatches=2,
            demographic=["Сэйнэн"],
            genres=["Драма", "Фантастика"],
            themes=["Путешествие во времени"],
            studios=["Studio <unsafe>"],
            origin="Манга",
            rating="R-17",
        ),
        "2": _record("Planned", status="planned", kind="movie"),
    }

    report = lists.build_list_report(
        stats,
        "anime",
        "all",
        base_url="https://shikimori.io",
    )
    ordinary = "\n".join(rendered_html(report))
    visible = _visible(ordinary)
    payload = render_rich_report(report)[0].payload

    assert "2 тайтла  ·  2 статуса" in visible
    for token in (
        "Оценено: 9⭐",
        "Год: 2024",
        "Тип: TV-сериал",
        "Shikimori: 8.75⭐",
        "Прогресс: 12/24 эпизодов",
        "Статус: Вышло",
        "Повторно просмотрено: 2 раза",
        "Демография: Сэйнэн",
        "Жанры: Драма, Фантастика",
        "Темы: Путешествие во времени",
        "Студия: Studio <unsafe>",
        "Длительность: 24 минуты/эп.",
        "Первоисточник: Манга",
        "Возрастной рейтинг: R-17",
    ):
        assert token in visible
    assert "Studio &lt;unsafe&gt;" in ordinary
    assert payload["blocks"][-1]["text"]["text"]["text"] == "↑ К началу"
    details = [block for block in payload["blocks"] if block["type"] == "details"]
    assert len(details) == 2
    assert all(detail["is_open"] is False for detail in details)
    detailed_table = next(
        block
        for detail in details
        for block in detail["blocks"]
        if block["type"] == "table"
        if any(
            isinstance(cell.get("text"), dict)
            and cell["text"].get("type") == "url"
            for row in block["cells"]
            for cell in row
        )
    )
    assert len(detailed_table["cells"]) == 5
    assert len(detailed_table["cells"][0]) == 3
    assert detailed_table["cells"][0][0]["text"] == "1"
    assert detailed_table["cells"][0][1]["text"]["type"] == "url"
    assert all(
        row[0]["colspan"] == 3
        for row in detailed_table["cells"][1:]
    )
    detail_texts = [
        "".join(
            part.get("text", "") if isinstance(part, dict) else part
            for part in row[0]["text"]
        )
        for row in detailed_table["cells"][1:]
    ]
    assert detail_texts == [
        "Год: 2024  ·  Тип: TV-сериал  ·  Shikimori: 8.75⭐",
        "Прогресс: 12/24 эпизодов  ·  Статус: Вышло  ·  "
        "Повторно просмотрено: 2 раза",
        "Демография: Сэйнэн  ·  Жанры: Драма, Фантастика  ·  "
        "Темы: Путешествие во времени",
        "Студия: Studio <unsafe>  ·  Длительность: 24 минуты/эп.  ·  "
        "Первоисточник: Манга  ·  Возрастной рейтинг: R-17",
    ]


@pytest.mark.parametrize(
    ("count", "episode_word", "minute_word", "time_word"),
    [
        (1, "эпизод", "минута", "раз"),
        (2, "эпизода", "минуты", "раза"),
        (5, "эпизодов", "минут", "раз"),
    ],
)
def test_anime_numeric_metadata_uses_russian_count_forms(
    count,
    episode_word,
    minute_word,
    time_word,
):
    stats = _stats()
    stats["anime"]["titles"] = {
        "1": _record(
            "Title",
            episodes_watched=count,
            duration=count,
            rewatches=count,
        ),
    }

    visible = _visible("\n".join(rendered_html(lists.build_list_report(
        stats,
        "anime",
        "completed",
        base_url="https://shikimori.io",
    ))))

    assert f"Прогресс: {count} {episode_word}" in visible
    assert f"Длительность: {count} {minute_word}/эп." in visible
    assert f"Повторно просмотрено: {count} {time_word}" in visible


@pytest.mark.parametrize(
    ("watched", "total", "expected"),
    [
        (44, 44, "44/44 эпизодов"),
        (0, 24, "0/24 эпизодов"),
        (0, 4, "0/4 эпизодов"),
        (0, 1, "0/1 эпизода"),
    ],
)
def test_episode_progress_uses_total_for_russian_count_form(
    watched,
    total,
    expected,
):
    stats = _stats()
    stats["anime"]["titles"] = {
        "1": _record(
            "Title",
            episodes_watched=watched,
            episodes_total=total,
        ),
    }

    visible = _visible("\n".join(rendered_html(lists.build_list_report(
        stats,
        "anime",
        "completed",
        base_url="https://shikimori.io",
    ))))

    assert f"Прогресс: {expected}" in visible


@pytest.mark.parametrize(
    ("media", "kind", "release_status", "expected"),
    [
        ("anime", "tv", "anons", "Анонс"),
        ("anime", "tv", "ongoing", "Онгоинг"),
        ("anime", "tv", "released", "Вышло"),
        ("anime", "tv", "latest", "Недавно вышло"),
        ("manga", "manga", "anons", "Анонс"),
        ("manga", "manga", "ongoing", "Выходит"),
        ("manga", "manga", "released", "Издано"),
        ("manga", "manga", "latest", "Недавно издано"),
        ("manga", "manga", "paused", "Приостановлено"),
        ("manga", "manga", "discontinued", "Прекращено"),
    ],
)
def test_release_status_uses_shikimori_media_wording(
    media,
    kind,
    release_status,
    expected,
):
    stats = _stats()
    stats[media]["titles"] = {
        "1": _record("Title", kind=kind, release_status=release_status),
    }

    visible = _visible("\n".join(rendered_html(lists.build_list_report(
        stats,
        media,
        "completed",
        base_url="https://shikimori.io",
    ))))

    assert f"Статус: {expected}" in visible


def test_origin_and_novel_kind_reuse_canonical_shikimori_wording():
    stats = _stats()
    stats["anime"]["titles"] = {
        "1": _record("Anime", origin="mixed_media"),
    }
    stats["manga"]["titles"] = {
        "2": _record("Novel", kind="novel"),
    }

    anime = _visible("\n".join(rendered_html(lists.build_list_report(
        stats,
        "anime",
        "completed",
        base_url="https://shikimori.io",
    ))))
    ranobe = _visible("\n".join(rendered_html(lists.build_list_report(
        stats,
        "ranobe",
        "completed",
        base_url="https://shikimori.io",
    ))))

    assert "Первоисточник: Более одного" in anime
    assert "mixed_media" not in anime
    assert "Тип: Новелла" in ranobe


def test_reading_catalog_uses_chapters_volumes_and_publishers():
    stats = _stats()
    stats["manga"]["titles"] = {
        "1": _record(
            "Manga",
            kind="manga",
            chapters_read=10,
            chapters_total=100,
            volumes_read=2,
            volumes_total=20,
            publishers=["One", "Two"],
        ),
    }

    report = lists.build_list_report(
        stats,
        "manga",
        "completed",
        base_url="https://shikimori.io",
    )
    ordinary = "\n".join(rendered_html(report))
    visible = _visible(ordinary)
    payload = render_rich_report(report)[0].payload

    assert "1 тайтл" in visible
    assert "Прогресс: 10/100 глав, 2/20 томов" in visible
    assert "Издатели: One, Two" in visible
    details = next(block for block in payload["blocks"] if block["type"] == "details")
    assert details["is_open"] is True
    assert payload["blocks"][-1]["text"]["text"]["text"] == "↑ К началу"


def test_reading_progress_fraction_uses_genitive_forms():
    stats = _stats()
    stats["manga"]["titles"] = {
        "1": _record(
            "Manga",
            kind="manga",
            chapters_read=0,
            chapters_total=1,
            volumes_read=0,
            volumes_total=4,
        ),
    }

    visible = _visible("\n".join(rendered_html(lists.build_list_report(
        stats,
        "manga",
        "completed",
        base_url="https://shikimori.io",
    ))))

    assert "Прогресс: 0/1 главы, 0/4 томов" in visible


@pytest.mark.parametrize(
    ("count", "chapter_word", "volume_word", "time_word"),
    [
        (1, "глава", "том", "раз"),
        (2, "главы", "тома", "раза"),
        (5, "глав", "томов", "раз"),
    ],
)
def test_reading_numeric_metadata_uses_russian_count_forms(
    count,
    chapter_word,
    volume_word,
    time_word,
):
    stats = _stats()
    stats["manga"]["titles"] = {
        "1": _record(
            "Manga",
            kind="manga",
            chapters_read=count,
            volumes_read=count,
            rewatches=count,
        ),
    }

    visible = _visible("\n".join(rendered_html(lists.build_list_report(
        stats,
        "manga",
        "completed",
        base_url="https://shikimori.io",
    ))))

    assert f"Прогресс: {count} {chapter_word}, {count} {volume_word}" in visible
    assert f"Повторно прочитано: {count} {time_word}" in visible


def test_malformed_optional_metadata_is_omitted_without_hiding_the_title():
    stats = _stats()
    stats["anime"]["titles"] = {
        "1": _record(
            "Still visible",
            kind=["tv"],
            year=True,
            shiki_score=float("nan"),
            release_status={"ongoing": True},
            episodes_watched=True,
            episodes_total=-1,
            duration=-24,
            rewatches="2",
            genres=["Драма", 17, None],
            themes="Школа",
            demographic=[object()],
            studios={"Studio": "Name"},
        ),
    }

    report = lists.build_list_report(
        stats,
        "anime",
        "completed",
        base_url="https://shikimori.io",
    )
    visible = _visible("\n".join(rendered_html(report)))

    assert "Still visible" in visible
    assert "Жанры: Драма" in visible
    for token in ("Shikimori:", "Прогресс:", "Статус:", "Длительность:"):
        assert token not in visible
