# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Неизменяемый портрет сохранённого списка и объяснимые расчёты без I/O."""

from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

CATEGORY_LABELS = {"anime": "Аниме", "manga": "Манга", "ranobe": "Ранобэ"}
FORMAT_LABELS = {
    "tv": "Сериалы", "movie": "Фильмы", "ova": "OVA", "ona": "ONA",
    "manga": "Манга", "manhwa": "Манхва", "manhua": "Маньхуа",
    "light_novel": "Ранобэ", "novel": "Новеллы",
}
AXES = ("genre", "theme", "format")


@dataclass(frozen=True)
class TasteTitle:
    """Пригодные независимые поля одного уникального тайтла."""

    id: int
    title: str
    url: str
    status: str
    kind: str
    score: int | None = None
    community: Fraction | None = None
    year: int | None = None
    genres: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TasteInput:
    """Срез одной категории; отсутствие источника отличается от пустого списка."""

    profile: str
    category: str
    generated_at: datetime
    synced_at: datetime | None
    titles: tuple[TasteTitle, ...] = ()
    state: str = "ready"
    unresolved: int = 0
    excluded: int = 0
    planned: int = 0


@dataclass(frozen=True)
class Difference:
    """Разница двух оценок одного тайтла."""

    title: TasteTitle
    delta: Fraction


@dataclass(frozen=True)
class Group:
    """Направление, заданное сохранённой меткой, без догадок о пропусках."""

    axis: str
    label: str
    titles: tuple[TasteTitle, ...]

    @property
    def key(self) -> tuple[int, str]:
        """Стабильный порядок осей и нормализованных меток."""
        return AXES.index(self.axis), self.label.casefold()


@dataclass(frozen=True)
class Preference:
    """Поддержанное отклонение средней группы от обычного уровня профиля."""

    group: Group
    mean: Fraction
    delta: Fraction
    example: TasteTitle


@dataclass(frozen=True)
class Preferences:
    """Две независимые стороны предпочтений и покрытие каждой оси."""

    state: str
    baseline: Fraction | None
    higher: Preference | None = None
    lower: Preference | None = None
    coverage: tuple[int, int, int] = (0, 0, 0)


@dataclass(frozen=True)
class Diversity:
    """Описательные факты об оценённой части, без единого балла разнообразия."""

    years: tuple[int, ...]
    decades: tuple[int, ...]
    formats: tuple[tuple[str, int], ...]
    genre: tuple[str, int] | None
    theme: tuple[str, int] | None
    rare: TasteTitle | None
    rare_label: str = ""


@dataclass(frozen=True)
class DropPattern:
    """Поддержанная повышенная доля отказов в одном направлении."""

    group: Group
    dropped: int
    delta: Fraction
    example: TasteTitle


@dataclass(frozen=True)
class Dropping:
    """Общая доля среди конечных исходов и отдельная интерпретация меток."""

    state: str
    total: int
    dropped: int
    pattern: DropPattern | None = None
    coverage: tuple[int, int, int] = (0, 0, 0)

    @property
    def rate(self) -> Fraction | None:
        """Пустая база не превращается в нулевую долю."""
        return Fraction(self.dropped, self.total) if self.total else None


@dataclass(frozen=True)
class TasteCard:
    """Результат четырёх независимых блоков и их собственные выборки."""

    source: TasteInput
    rated: int
    paired: int
    comparison_state: str
    above: Difference | None
    below: Difference | None
    preferences: Preferences
    diversity: Diversity
    dropping: Dropping

    @property
    def examples(self) -> tuple[TasteTitle, ...]:
        """Не более шести различных примеров в порядке блоков."""
        candidates = (
            self.above.title if self.above else None,
            self.below.title if self.below else None,
            self.preferences.higher.example if self.preferences.higher else None,
            self.preferences.lower.example if self.preferences.lower else None,
            self.diversity.rare,
            self.dropping.pattern.example if self.dropping.pattern else None,
        )
        found = {}
        for title in candidates:
            if title is not None:
                found.setdefault(title.id, title)
        return tuple(found.values())

    @property
    def available(self) -> bool:
        """Карточка требует хотя бы оценку или пригодный конечный исход."""
        return self.source.state == "ready" and bool(self.rated or self.dropping.total)


def _mean(titles: tuple[TasteTitle, ...]) -> Fraction:
    """Средняя явных оценок без округления перед отбором."""
    return Fraction(sum(title.score for title in titles), len(titles))


def _groups(titles: tuple[TasteTitle, ...]) -> tuple[tuple[Group, ...], tuple[int, int, int]]:
    """Собрать метки и независимо посчитать покрытие осей."""
    rows = {}
    coverage = [0, 0, 0]
    for title in titles:
        for index, values in enumerate((title.genres, title.themes, (title.kind,))):
            if values:
                coverage[index] += 1
            for label in values:
                key = (AXES[index], label.casefold())
                display = FORMAT_LABELS.get(label, label) if index == 2 else label
                if key not in rows:
                    rows[key] = (display, [])
                rows[key][1].append(title)
    groups = tuple(sorted(
        (Group(axis, label, tuple(members)) for (axis, _), (label, members) in rows.items()),
        key=lambda group: group.key,
    ))
    return groups, tuple(coverage)


def _preferences(titles: tuple[TasteTitle, ...]) -> Preferences:
    """Симметричный отбор с проверкой без единственного лучшего/худшего примера."""
    groups, coverage = _groups(titles)
    count = len(titles)
    if not count:
        return Preferences("unavailable", None, coverage=coverage)
    baseline = _mean(titles)
    if count < 30:
        return Preferences("insufficient", baseline, coverage=coverage)
    candidates = {1: [], -1: []}
    tested = 0
    total = sum(title.score for title in titles)
    for group in groups:
        size = len(group.titles)
        if (
            size < 8 or count - size < 10
            or coverage[AXES.index(group.axis)] * 10 < count * 7
        ):
            continue
        tested += 1
        mean = _mean(group.titles)
        delta = mean - baseline
        sign = 1 if delta >= 0 else -1
        if delta * sign < Fraction(3, 4):
            continue
        extreme = max(title.score for title in group.titles) if sign == 1 else min(
            title.score for title in group.titles
        )
        reduced = Fraction(sum(title.score for title in group.titles) - extreme, size - 1)
        reduced_baseline = Fraction(total - extreme, count - 1)
        if (reduced - reduced_baseline) * sign < Fraction(1, 2):
            continue
        example = min(group.titles, key=lambda title: (abs(title.score - mean), title.id))
        candidates[sign].append(Preference(group, mean, delta, example))
    selected = []
    for sign in (1, -1):
        ordered = sorted(candidates[sign], key=lambda item: (
            -abs(item.delta), -len(item.group.titles), item.group.key,
        ))
        selected.append(ordered[0] if ordered else None)
    higher, lower = selected
    state = "signal" if higher or lower else "neutral" if tested else "insufficient"
    return Preferences(state, baseline, higher, lower, coverage)


def _diversity(titles: tuple[TasteTitle, ...]) -> Diversity:
    """Состав, частые направления и один редкий пример по известным полям."""
    count = len(titles)
    groups, coverage = _groups(titles)
    years = tuple(sorted(title.year for title in titles if title.year is not None))
    formats = tuple(sorted(
        ((group.label, len(group.titles)) for group in groups if group.axis == "format"),
        key=lambda row: (-row[1], row[0].casefold()),
    ))
    frequent = []
    for index, axis in enumerate(AXES[:2]):
        choices = [group for group in groups if group.axis == axis]
        best = min(choices, key=lambda group: (-len(group.titles), group.key)) if choices else None
        frequent.append(
            (best.label, len(best.titles))
            if best and count >= 20 and len(best.titles) >= 3
            and coverage[index] * 10 >= count * 7
            and len(best.titles) * 4 >= coverage[index] else None
        )
    rare_candidates = []
    if count >= 20:
        format_groups = [group for group in groups if group.axis == "format"]
        if format_groups:
            dominant = min(format_groups, key=lambda group: (-len(group.titles), group.key))
            for group in format_groups:
                if group != dominant and len(group.titles) * 10 <= count:
                    rare_candidates.append((len(group.titles), 0, group.label, group.titles[0]))
        if len(years) * 5 >= count * 4:
            decades = {}
            for title in titles:
                if title.year is not None:
                    decades.setdefault(title.year // 10 * 10, []).append(title)
            dominant = min(decades, key=lambda decade: (-len(decades[decade]), decade))
            for decade, members in decades.items():
                if decade != dominant and len(members) * 10 <= len(years):
                    rare_candidates.append((len(members), 1, f"{decade}-е", members[0]))
    rare = min(rare_candidates, key=lambda row: (row[:3], row[3].id)) if rare_candidates else None
    return Diversity(
        years, tuple(sorted({year // 10 * 10 for year in years})), formats,
        frequent[0], frequent[1], rare[3] if rare else None, rare[2] if rare else "",
    )


def _dropping(titles: tuple[TasteTitle, ...]) -> Dropping:
    """Сравнить доли, не подменяя их сырыми счётчиками и оценками."""
    total = len(titles)
    dropped = sum(title.status == "dropped" for title in titles)
    groups, coverage = _groups(titles)
    if not total:
        return Dropping("unavailable", 0, 0, coverage=coverage)
    baseline = Fraction(dropped, total)
    tested = 0
    candidates = []
    if total >= 30:
        for group in groups:
            size = len(group.titles)
            if (
                size < 10 or total - size < 10
                or coverage[AXES.index(group.axis)] * 10 < total * 7
            ):
                continue
            tested += 1
            declined = tuple(title for title in group.titles if title.status == "dropped")
            delta = Fraction(len(declined), size) - baseline
            if len(declined) >= 3 and delta >= Fraction(15, 100):
                candidates.append(DropPattern(group, len(declined), delta, declined[0]))
    best = min(candidates, key=lambda item: (
        -item.delta, -len(item.group.titles), item.group.key,
    )) if candidates else None
    state = "signal" if best else "neutral" if tested else "insufficient"
    return Dropping(state, total, dropped, best, coverage)


def build_taste_card(source: TasteInput) -> TasteCard:
    """Рассчитать независимые блоки по одному неизменяемому срезу."""
    rated = tuple(sorted(
        (title for title in source.titles if title.score is not None), key=lambda title: title.id,
    ))
    paired = tuple(title for title in rated if title.community is not None)
    differences = tuple(Difference(title, title.score - title.community) for title in paired)
    above = min(
        (item for item in differences if item.delta >= Fraction(3, 2)),
        key=lambda item: (-item.delta, item.title.id), default=None,
    )
    below = min(
        (item for item in differences if item.delta <= -Fraction(3, 2)),
        key=lambda item: (item.delta, item.title.id), default=None,
    )
    comparison_state = (
        "signal" if above or below else "neutral" if len(paired) >= 5
        else "insufficient" if paired else "unavailable"
    )
    finals = tuple(sorted(
        (title for title in source.titles if title.status in {"completed", "dropped"}),
        key=lambda title: title.id,
    ))
    return TasteCard(
        source, len(rated), len(paired), comparison_state, above, below,
        _preferences(rated), _diversity(rated), _dropping(finals),
    )


def unavailable_text(card: TasteCard) -> str:
    """Короткое объяснение вместо изображения из придуманных нулей."""
    state = card.source.state
    if state == "invalid":
        return "Не получилось прочитать сохранённые данные этой категории. Попробуй после обновления данных."
    if state == "missing":
        return "Список этой категории ещё не готов. Попробуй после следующего обновления данных."
    if state == "unresolved":
        return "В сохранённом списке есть записи, тип которых пока не определён. Карточка этой категории пока недоступна."
    if state == "empty":
        return "В этой категории пока нет тайтлов для карточки. Запланированное не учитывается."
    return "Для карточки пока нет оценок или завершённых и брошенных тайтлов."
