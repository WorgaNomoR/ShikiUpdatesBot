# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистая декларативная модель публичных списков Shikimori."""

from dataclasses import dataclass
from math import isfinite
from unicodedata import normalize

from report_model import (
    Bold,
    Inline,
    Italic,
    Report,
    Table,
    TableCell,
    TableGroup,
    TableRow,
    Text,
    Title,
    heading,
    line,
    section,
    unit,
)
from stats import (
    classify_manga_presentation_kind,
    translate_origin,
)
from utils import (
    _rel_url,
    russian_count_word,
)

MEDIA_ANIME = "anime"
MEDIA_MANGA = "manga"
MEDIA_RANOBE = "ranobe"
MEDIA_COMBINED = "combined"
MEDIA_UNKNOWN = "unknown"

VIEW_COMPLETED = "completed"
VIEW_PLANNED = "planned"
VIEW_ALL = "all"

STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ListMediaDefinition:
    """Один пункт корневого меню и его presentation-категория."""

    key: str
    label: str
    emoji: str
    domain: str | None
    category: str | None
    terminal: bool = False


@dataclass(frozen=True)
class ListViewDefinition:
    """Один декларативный вид списка."""

    key: str
    label: str
    status: str | None


@dataclass(frozen=True)
class ListStatusDefinition:
    """Один известный статус с подписями просмотра и чтения."""

    key: str
    emoji: str
    anime_label: str
    reading_label: str


@dataclass(frozen=True)
class ListEntry:
    """Нормализованная presentation-копия одной сохранённой записи."""

    stable_id: str
    title: str
    url: str | None
    score: int | None
    status: str
    comment: str | None
    kind: str
    release_status: str
    year: int | None
    shiki_score: float | None
    genres: tuple[str, ...]
    themes: tuple[str, ...]
    demographic: tuple[str, ...]
    episodes_watched: int | None
    episodes_total: int | None
    duration: int | None
    rating: str
    origin: str
    studios: tuple[str, ...]
    chapters_read: int | None
    volumes_read: int | None
    chapters_total: int | None
    volumes_total: int | None
    publishers: tuple[str, ...]
    rewatches: int | None


LIST_MEDIA_DEFINITIONS: tuple[ListMediaDefinition, ...] = (
    ListMediaDefinition(MEDIA_ANIME, "🎬 Аниме", "🎬", "anime", MEDIA_ANIME),
    ListMediaDefinition(MEDIA_MANGA, "📚 Манга", "📚", "manga", MEDIA_MANGA),
    ListMediaDefinition(MEDIA_RANOBE, "📖 Ранобэ", "📖", "manga", MEDIA_RANOBE),
    ListMediaDefinition(
        MEDIA_COMBINED,
        "🗂 Всё вместе",
        "🗂",
        None,
        None,
        terminal=True,
    ),
)
LIST_MEDIA_BY_KEY = {definition.key: definition for definition in LIST_MEDIA_DEFINITIONS}

LIST_VIEW_DEFINITIONS: tuple[ListViewDefinition, ...] = (
    ListViewDefinition(VIEW_COMPLETED, "✅ Завершённое", VIEW_COMPLETED),
    ListViewDefinition(VIEW_PLANNED, "📌 Запланированное", VIEW_PLANNED),
    ListViewDefinition(VIEW_ALL, "📋 Полный список", None),
)
LIST_VIEW_BY_KEY = {definition.key: definition for definition in LIST_VIEW_DEFINITIONS}

LIST_STATUS_DEFINITIONS: tuple[ListStatusDefinition, ...] = (
    ListStatusDefinition("watching", "▶️", "Смотрю", "Читаю"),
    ListStatusDefinition("rewatching", "🔄", "Пересматриваю", "Перечитываю"),
    ListStatusDefinition("planned", "📌", "Запланировано", "Запланировано"),
    ListStatusDefinition("completed", "✅", "Просмотрено", "Прочитано"),
    ListStatusDefinition("on_hold", "⏸️", "Отложено", "Отложено"),
    ListStatusDefinition("dropped", "🗑️", "Брошено", "Брошено"),
)
LIST_STATUS_BY_KEY = {
    definition.key: definition for definition in LIST_STATUS_DEFINITIONS
}

_KIND_LABELS = {
    "tv": "TV-сериал",
    "movie": "Фильм",
    "ova": "OVA",
    "ona": "ONA",
    "special": "Спешл",
    "tv_special": "TV-спешл",
    "music": "Клип",
    "pv": "Проморолик",
    "cm": "Реклама",
    "manga": "Манга",
    "manhwa": "Манхва",
    "manhua": "Маньхуа",
    "one_shot": "Ваншот",
    "doujin": "Додзинси",
    "light_novel": "Ранобэ",
    "novel": "Новелла",
    "ranobe": "Ранобэ",
}
_ANIME_RELEASE_STATUS_LABELS = {
    "anons": "Анонс",
    "ongoing": "Онгоинг",
    "released": "Вышло",
    "latest": "Недавно вышло",
}
_READING_RELEASE_STATUS_LABELS = {
    "anons": "Анонс",
    "ongoing": "Выходит",
    "released": "Издано",
    "latest": "Недавно издано",
    "paused": "Приостановлено",
    "discontinued": "Прекращено",
}


def _normalized_display_title(value: object) -> str:
    """Нормализовать видимый заголовок и его ключ сортировки."""
    if not isinstance(value, str):
        return ""
    return " ".join(normalize("NFKC", value).split())


def _stable_id(value: object) -> str:
    """Получить детерминированное строковое представление JSON-ключа."""
    try:
        rendered = str(value).strip()
    except Exception:
        rendered = ""
    return rendered or "?"


def _stable_id_sort_key(value: str) -> tuple[int, int, str]:
    """Сортировать положительные числовые ID численно, остальные строково."""
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return 1, 0, normalize("NFKC", value).casefold()
    if numeric > 0 and str(numeric) == value:
        return 0, numeric, ""
    return 1, 0, normalize("NFKC", value).casefold()


def _positive_owner_score(value: object) -> int | None:
    """Оставить только допустимую положительную пользовательскую оценку."""
    return value if type(value) is int and 1 <= value <= 10 else None


def _normalized_status(value: object) -> str:
    """Сопоставить только точный известный статус после нормализации строки."""
    if not isinstance(value, str):
        return STATUS_UNKNOWN
    normalized = value.strip().casefold()
    return normalized if normalized in LIST_STATUS_BY_KEY else STATUS_UNKNOWN


def _plain_comment(value: object) -> str | None:
    """Подготовить недоверенный многострочный plain text без интерпретации."""
    if not isinstance(value, str):
        return None
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized or None


def _plain_text(value: object) -> str:
    """Нормализовать необязательное локальное metadata-поле."""
    return _normalized_display_title(value)


def _plain_values(value: object) -> tuple[str, ...]:
    """Сохранить только непустые строки массива без изменения источника."""
    if not isinstance(value, list):
        return ()
    result = []
    for item in value:
        normalized = _plain_text(item)
        if normalized:
            result.append(normalized)
    return tuple(result)


def _nonnegative_int(value: object) -> int | None:
    """Принять целое неотрицательное metadata-значение без bool."""
    return value if type(value) is int and value >= 0 else None


def _positive_year(value: object) -> int | None:
    """Принять читабельный положительный год."""
    return value if type(value) is int and 1 <= value <= 9999 else None


def _positive_shiki_score(value: object) -> float | None:
    """Принять конечную положительную оценку Shikimori до десяти."""
    if type(value) not in {int, float}:
        return None
    score = float(value)
    return score if isfinite(score) and 0 < score <= 10 else None


def _normalized_shikimori_url(value: object, base_url: str) -> str | None:
    """Вернуть только нормализованную ссылку внутри заданного Shikimori host."""
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    relative = _rel_url(value)
    if not relative:
        return None
    relative = f"/{relative.lstrip('/')}"
    result = f"{base_url.rstrip('/')}{relative}"
    return result if len(result) <= 1000 else None


def _entry(title_id: object, record: object, base_url: str) -> ListEntry:
    """Создать безопасную presentation-копию, не меняя исходную запись."""
    stable_id = _stable_id(title_id)
    source = record if isinstance(record, dict) else {}
    title = _normalized_display_title(source.get("title"))
    if not title:
        suffix = stable_id if len(stable_id) <= 80 else f"{stable_id[:79]}…"
        title = f"Без названия (ID {suffix})"
    elif len(title) > 1000:
        title = f"{title[:999].rstrip()}…"
    return ListEntry(
        stable_id=stable_id,
        title=title,
        url=_normalized_shikimori_url(source.get("url"), base_url),
        score=_positive_owner_score(source.get("score")),
        status=_normalized_status(source.get("status")),
        comment=_plain_comment(source.get("comment")),
        kind=_plain_text(source.get("kind")).casefold(),
        release_status=_plain_text(source.get("release_status")).casefold(),
        year=_positive_year(source.get("year")),
        shiki_score=_positive_shiki_score(source.get("shiki_score")),
        genres=_plain_values(source.get("genres")),
        themes=_plain_values(source.get("themes")),
        demographic=_plain_values(source.get("demographic")),
        episodes_watched=_nonnegative_int(source.get("episodes_watched")),
        episodes_total=_nonnegative_int(source.get("episodes_total")),
        duration=_nonnegative_int(source.get("duration")),
        rating=_plain_text(source.get("rating")),
        origin=_plain_text(source.get("origin")),
        studios=_plain_values(source.get("studios")),
        chapters_read=_nonnegative_int(source.get("chapters_read")),
        volumes_read=_nonnegative_int(source.get("volumes_read")),
        chapters_total=_nonnegative_int(source.get("chapters_total")),
        volumes_total=_nonnegative_int(source.get("volumes_total")),
        publishers=_plain_values(source.get("publishers")),
        rewatches=_nonnegative_int(source.get("rewatches")),
    )


def _entry_sort_key(entry: ListEntry) -> tuple:
    """Оценённые выше; затем нормализованный заголовок и стабильный ID."""
    return (
        entry.score is None,
        -(entry.score or 0),
        normalize("NFKC", entry.title).casefold(),
        _stable_id_sort_key(entry.stable_id),
    )


def _domain_titles(stats_all: object, domain: str) -> tuple[dict, bool]:
    """Прочитать один локальный titles-контейнер с явным признаком повреждения."""
    if not isinstance(stats_all, dict):
        return {}, True
    if domain not in stats_all:
        return {}, False
    media = stats_all.get(domain)
    if not isinstance(media, dict) or not isinstance(media.get("titles"), dict):
        return {}, True
    return media["titles"], False


def _partition_entries(
    stats_all: object,
    base_url: str,
) -> tuple[dict[str, list[ListEntry]], dict[str, bool]]:
    """Исчерпывающе разложить локальные записи по четырём категориям."""
    partitions = {
        MEDIA_ANIME: [],
        MEDIA_MANGA: [],
        MEDIA_RANOBE: [],
        MEDIA_UNKNOWN: [],
    }
    anime_titles, anime_unreadable = _domain_titles(stats_all, "anime")
    manga_titles, manga_unreadable = _domain_titles(stats_all, "manga")

    partitions[MEDIA_ANIME].extend(
        _entry(title_id, record, base_url)
        for title_id, record in anime_titles.items()
    )
    for title_id, record in manga_titles.items():
        kind = record.get("kind") if isinstance(record, dict) else None
        category = classify_manga_presentation_kind(kind)
        partitions[category].append(_entry(title_id, record, base_url))

    return partitions, {
        "anime": anime_unreadable,
        "manga": manga_unreadable,
    }


def _status_label(status: str, *, reading: bool) -> str:
    """Вернуть читательскую подпись известного или unresolved-статуса."""
    if status == STATUS_UNKNOWN:
        return "Статус не определён"
    definition = LIST_STATUS_BY_KEY[status]
    return definition.reading_label if reading else definition.anime_label


def _status_emoji(status: str) -> str:
    """Вернуть устойчивый визуальный маркер известного или будущего статуса."""
    if status == STATUS_UNKNOWN:
        return "⚠️"
    return LIST_STATUS_BY_KEY[status].emoji


def _cell(
    *parts: Inline | str,
    colspan: int = 1,
    align: str = "left",
    valign: str = "top",
) -> TableCell:
    """Собрать безопасную типизированную ячейку таблицы."""
    return TableCell(
        line(*parts).parts,
        colspan=colspan,
        align=align,
        valign=valign,
    )


def _kind_label(value: str) -> str:
    """Перевести известный kind и честно показать будущий вариант."""
    if not value:
        return "—"
    return _KIND_LABELS.get(value, value.replace("_", " ").capitalize())


def _release_status_label(value: str, *, reading: bool) -> str:
    """Перевести состояние тайтла в терминах Shikimori для его домена."""
    if not value:
        return ""
    labels = (
        _READING_RELEASE_STATUS_LABELS
        if reading
        else _ANIME_RELEASE_STATUS_LABELS
    )
    return labels.get(
        value,
        value.replace("_", " ").capitalize(),
    )


def _score_text(value: int | None) -> str:
    """Отобразить личную оценку в едином формате отчётов."""
    return f"{value}⭐" if value is not None else "—"


def _shiki_score_text(value: float) -> str:
    """Не добавлять искусственный десятичный ноль к оценке Shikimori."""
    return f"{value:g}⭐"


def _progress_text(entry: ListEntry, *, reading: bool) -> str:
    """Собрать локальный прогресс просмотра либо чтения."""
    if not reading:
        if entry.episodes_watched is None and entry.episodes_total is None:
            return ""
        watched = entry.episodes_watched or 0
        if entry.episodes_total is None:
            episode_word = russian_count_word(
                watched,
                "эпизод",
                "эпизода",
                "эпизодов",
            )
            return f"{watched} {episode_word}"
        episode_word = russian_count_word(
            entry.episodes_total,
            "эпизода",
            "эпизодов",
            "эпизодов",
        )
        return f"{watched}/{entry.episodes_total} {episode_word}"

    parts = []
    if entry.chapters_read is not None or entry.chapters_total is not None:
        read = entry.chapters_read or 0
        if entry.chapters_total is None:
            chapter_word = russian_count_word(
                read,
                "глава",
                "главы",
                "глав",
            )
            parts.append(f"{read} {chapter_word}")
        else:
            chapter_word = russian_count_word(
                entry.chapters_total,
                "главы",
                "глав",
                "глав",
            )
            parts.append(f"{read}/{entry.chapters_total} {chapter_word}")
    if entry.volumes_read is not None or entry.volumes_total is not None:
        read = entry.volumes_read or 0
        if entry.volumes_total is None:
            volume_word = russian_count_word(
                read,
                "том",
                "тома",
                "томов",
            )
            parts.append(f"{read} {volume_word}")
        else:
            volume_word = russian_count_word(
                entry.volumes_total,
                "тома",
                "томов",
                "томов",
            )
            parts.append(f"{read}/{entry.volumes_total} {volume_word}")
    return ", ".join(parts)


def _field_parts(fields: list[tuple[str, str]]) -> tuple[Inline, ...]:
    """Собрать одну компактную строку именованных metadata-полей."""
    parts: list[Inline] = []
    for label, value in fields:
        if not value:
            continue
        if parts:
            parts.append(Text("  ·  "))
        parts.extend((Bold(f"{label}: "), Text(value)))
    return tuple(parts)


def _entry_detail_lines(
    entry: ListEntry,
    *,
    reading: bool,
) -> tuple[tuple[Inline, ...], ...]:
    """Разложить metadata тайтла на короткие мобильные строки."""
    overview = [
        ("Год", str(entry.year) if entry.year is not None else "—"),
        ("Тип", _kind_label(entry.kind)),
    ]
    if entry.shiki_score is not None:
        overview.append(("Shikimori", _shiki_score_text(entry.shiki_score)))

    progress = _progress_text(entry, reading=reading)
    state = []
    if progress:
        state.append(("Прогресс", progress))
    release_status = _release_status_label(entry.release_status, reading=reading)
    if release_status:
        state.append(("Статус", release_status))
    if entry.rewatches:
        state.append((
            "Повторно прочитано" if reading else "Повторно просмотрено",
            f"{entry.rewatches} "
            f"{russian_count_word(entry.rewatches, 'раз', 'раза', 'раз')}",
        ))

    taxonomy = []
    if entry.demographic:
        taxonomy.append(("Демография", ", ".join(entry.demographic)))
    if entry.genres:
        taxonomy.append(("Жанры", ", ".join(entry.genres)))
    if entry.themes:
        taxonomy.append(("Темы", ", ".join(entry.themes)))

    production = []
    if reading:
        if entry.publishers:
            production.append((
                "Издатели" if len(entry.publishers) > 1 else "Издатель",
                ", ".join(entry.publishers),
            ))
    else:
        if entry.studios:
            production.append((
                "Студии" if len(entry.studios) > 1 else "Студия",
                ", ".join(entry.studios),
            ))
        if entry.duration:
            minute_word = russian_count_word(
                entry.duration,
                "минута",
                "минуты",
                "минут",
            )
            production.append((
                "Длительность",
                f"{entry.duration} {minute_word}/эп.",
            ))
    if entry.origin:
        production.append(("Первоисточник", translate_origin(entry.origin) or ""))
    if entry.rating:
        production.append(("Возрастной рейтинг", entry.rating))

    return tuple(
        parts
        for fields in (overview, state, taxonomy, production)
        if (parts := _field_parts(fields))
    )


def _entry_table_group(
    entry: ListEntry,
    number: int,
    *,
    reading: bool,
) -> TableGroup:
    """Сохранить title/comment/metadata как одну логическую группу."""
    score = _score_text(entry.score)
    rows = [TableRow((
        _cell(str(number), align="right", valign="middle"),
        _cell(Title(entry.title, entry.url)),
        _cell(score, align="center", valign="middle"),
    ))]
    fallback = [
        line(
            f"{number}. ",
            Title(entry.title, entry.url),
            "  ·  ",
            Bold("Оценено: "),
            score,
        ),
    ]
    for detail_parts in _entry_detail_lines(entry, reading=reading):
        rows.append(TableRow((_cell(*detail_parts, colspan=3),)))
        fallback.append(line(*detail_parts))
    after = (
        (line("💬 ", Italic(entry.comment)),)
        if entry.comment is not None
        else ()
    )
    return TableGroup(tuple(rows), tuple(fallback), after)


def _entries_table(entries: list[ListEntry], *, reading: bool) -> Table:
    """Собрать нумерованный каталог из самостоятельных мобильных карточек."""
    return Table(
        columns=3,
        groups=tuple(
            _entry_table_group(entry, number, reading=reading)
            for number, entry in enumerate(entries, 1)
        ),
        separate_groups=True,
    )


def _count_summary(total: int, status_count: int, *, all_statuses: bool) -> str:
    """Вернуть естественный счётчик тайтлов и непустых статусов."""
    result = (
        f"{total} "
        f"{russian_count_word(total, 'тайтл', 'тайтла', 'тайтлов')}"
    )
    if all_statuses:
        result += (
            f"  ·  {status_count} "
            f"{russian_count_word(status_count, 'статус', 'статуса', 'статусов')}"
        )
    return result


def _media_unit(
    definition: ListMediaDefinition,
    entries: list[ListEntry],
    view: ListViewDefinition,
    *,
    unreadable: bool = False,
    unresolved_kind_count: int = 0,
):
    """Собрать одну самостоятельную media-тему выбранного вида."""
    statuses = (
        (view.status,)
        if view.status is not None
        else tuple(definition.key for definition in LIST_STATUS_DEFINITIONS)
        + (STATUS_UNKNOWN,)
    )
    reading = definition.key != MEDIA_ANIME
    selected_by_status = []
    for status in statuses:
        selected = sorted(
            (entry for entry in entries if entry.status == status),
            key=_entry_sort_key,
        )
        if selected:
            selected_by_status.append((status, selected))
    selected_total = sum(len(selected) for _, selected in selected_by_status)

    header_items = [heading(
        f"{definition.emoji} ",
        Bold(definition.label.removeprefix(f"{definition.emoji} ").upper()),
        level=1,
    )]
    if not unreadable:
        header_items.append(line(Italic(_count_summary(
            selected_total,
            len(selected_by_status),
            all_statuses=view.status is None,
        ))))
    if unreadable:
        header_items.append(line(
            "⚠️ Сохранённый список повреждён: тайтлы этой категории прочитать нельзя."
        ))
    if unresolved_kind_count:
        title_word = russian_count_word(
            unresolved_kind_count,
            "тайтл",
            "тайтла",
            "тайтлов",
        )
        header_items.append(line(
            "⚠️ Не удалось отнести к манге или ранобэ "
            f"{unresolved_kind_count} {title_word}. "
            "Эти тайтлы доступны в разделе «Всё вместе»."
        ))
    sections = [section(*header_items)]

    for status, selected in selected_by_status:
        sections.append(section(
            heading(
                f"{_status_emoji(status)} ",
                Bold(
                    f"{_status_label(status, reading=reading)} · "
                    f"{len(selected)}"
                ),
                level=2,
                collapsible=True,
                is_open=view.status is not None,
            ),
            _entries_table(selected, reading=reading),
        ))

    if selected_total == 0 and not unreadable:
        sections.append(section(line(Italic("В этом разделе пока нет тайтлов."))))
    return unit(*sections)


def build_list_report(
    stats_all: object,
    media_key: str,
    view_key: str,
    *,
    base_url: str,
) -> Report:
    """Построить typed Report из локального stats_all без побочных эффектов."""
    media = LIST_MEDIA_BY_KEY.get(media_key)
    view = LIST_VIEW_BY_KEY.get(view_key)
    if media is None or view is None:
        raise ValueError("Неизвестное определение списка")

    partitions, unreadable = _partition_entries(stats_all, base_url)
    if media.key == MEDIA_COMBINED:
        combined_view = LIST_VIEW_BY_KEY[VIEW_ALL]
        definitions = [
            LIST_MEDIA_BY_KEY[MEDIA_ANIME],
            LIST_MEDIA_BY_KEY[MEDIA_MANGA],
            LIST_MEDIA_BY_KEY[MEDIA_RANOBE],
        ]
        if partitions[MEDIA_UNKNOWN] or unreadable["manga"]:
            definitions.append(ListMediaDefinition(
                MEDIA_UNKNOWN,
                "⚠️ Не определено",
                "⚠️",
                "manga",
                MEDIA_UNKNOWN,
            ))
        return Report(tuple(
            _media_unit(
                definition,
                partitions[definition.key],
                combined_view,
                unreadable=(
                    unreadable["anime"]
                    if definition.key == MEDIA_ANIME
                    else unreadable["manga"]
                ),
            )
            for definition in definitions
        ))

    unresolved_kind_count = (
        sum(
            1
            for entry in partitions[MEDIA_UNKNOWN]
            if view.status is None or entry.status == view.status
        )
        if media.key in {MEDIA_MANGA, MEDIA_RANOBE}
        else 0
    )
    return Report((_media_unit(
        media,
        partitions[media.key],
        view,
        unreadable=unreadable[media.domain or "anime"],
        unresolved_kind_count=unresolved_kind_count,
    ),))
