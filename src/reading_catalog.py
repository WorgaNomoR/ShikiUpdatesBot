# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Дополняющие метрики чтения без хранения, запросов и отдельной доставки."""

from functools import partial
from html import escape

from achievement_plans import AchievementStateError
from achievement_scales import (
    CHAPTER_SCALE,
    RARE_SCALE,
    TAXONOMY_SCALE,
    VOLUME_SCALE,
)
from bot_achievements import (
    BotAchievement,
    Evaluation,
)
from reading_milestones import (
    calculate_reader,
    reading_count,
)


def marked_count(checkpoint):
    """Неизвестные отметки или пригодность не являются нулевым прогрессом."""
    if not isinstance(checkpoint, dict):
        raise AchievementStateError("reading_marks")
    reading_count({key: None for key in checkpoint})
    total = 0
    unknown = False
    for row in checkpoint.values():
        if (
            not isinstance(row, dict) or set(row) != {"eligible", "marks"}
            or (row["eligible"] is not None and type(row["eligible"]) is not bool)
            or (row["marks"] is not None and (type(row["marks"]) is not int or row["marks"] < 0))
        ):
            raise AchievementStateError("reading_marks")
        if row["eligible"] is False:
            continue
        if row["eligible"] is None or row["marks"] is None:
            unknown = True
        else:
            total += row["marks"]
    return None if unknown else total


def calculate_marks(previous, current):
    """Сначала новая пригодность со старыми отметками, затем настоящий прирост."""
    value = marked_count(current)
    quiet = 0
    for key, row in current.items():
        old = (previous or {}).get(key)
        if old is not None and row["eligible"] is True and row["marks"] is not None:
            # Восстановление неизвестного числа само по себе не доказывает чтение.
            quiet += min(row["marks"], old["marks"]) if old["marks"] is not None else row["marks"]
    return Evaluation({key: dict(row) for key, row in current.items()}, value, quiet)


def _criterion(level, *, scale, metric):
    return f"{metric}: {scale.threshold(level)}"


def _award(level, value, *, title, metric):
    return f"✨ <b>{escape(title)} — уровень {level}</b>\n{escape(metric)}: <b>{value}</b>."


def _definition(family, title, scale, metric, *, marks=False):
    """Явная регистрация связывает готовую метрику со шкалой и текстом."""
    return BotAchievement(
        family=family, title=title,
        criterion=partial(_criterion, scale=scale, metric=metric),
        level_for=scale.level_for, next_level=scale.next_threshold,
        calculate=calculate_marks if marks else calculate_reader,
        checkpoint_value=marked_count if marks else reading_count,
        award_text=partial(_award, title=title, metric=metric),
        level_threshold=scale.threshold,
    )


CHAPTERS = _definition("chapters", "Глава за главой", CHAPTER_SCALE, "Отмечено прочитанных глав", marks=True)
VOLUMES = _definition("volumes", "Книжная полка", VOLUME_SCALE, "Отмечено прочитанных томов", marks=True)
LONG_WORKS = _definition("long_works", "Долгая история", RARE_SCALE, "Завершено произведений длиной от 100 глав")
PRE_2000 = _definition("pre_2000", "У истоков", RARE_SCALE, "Завершено произведений, начатых до 2000 года")

# Только ID пространства Manga и категории GraphQL; имена и REST kind не являются доказательством.
TAXONOMY_FAMILIES = (
    ("fantasy", "Магия между строк", "genre", 57, "фэнтези"),
    ("romance", "Сердце в переплёте", "genre", 62, "романтика"),
    ("comedy", "Смех сквозь страницы", "genre", 49, "комедия"),
    ("drama", "Истории на разрыв", "genre", 50, "драма"),
    ("horror", "Читать при включённом свете", "genre", 80, "ужасы"),
    ("mystery", "Распутыватель сюжетов", "genre", 46, "детектив"),
    ("sci_fi", "За пределами возможного", "genre", 53, "фантастика"),
    ("sports", "На пути к чемпионству", "genre", 76, "спорт"),
    ("shounen", "Дорога к мечте", "demographic", 47, "сёнэн"),
    ("shoujo", "Палитра чувств", "demographic", 63, "сёдзё"),
    ("seinen", "Истории со вторым дном", "demographic", 52, "сэйнэн"),
    ("josei", "Жизнь крупным планом", "demographic", 87, "дзёсэй"),
)
READING_ACHIEVEMENTS = (
    CHAPTERS, VOLUMES, LONG_WORKS, PRE_2000,
    *(_definition(family, title, TAXONOMY_SCALE, f"Завершено произведений категории «{label}»")
      for family, title, _, _, label in TAXONOMY_FAMILIES),
)


def _nonnegative(value):
    return type(value) is int and value >= 0


def manga_metadata_evidence(item):
    """Сохраняем только доказанные поля; явная неизвестность отличается от пропуска."""
    evidence = {}
    if "chapters" in item and (item["chapters"] is None or _nonnegative(item["chapters"])):
        evidence["chapters"] = item["chapters"]
    aired = item.get("airedOn")
    if "airedOn" in item and aired is None:
        evidence["year"] = None
    elif isinstance(aired, dict) and "year" in aired and (
        aired["year"] is None or (type(aired["year"]) is int and aired["year"] > 0)
    ):
        evidence["year"] = aired["year"]
    taxonomy = item.get("genres")
    if isinstance(taxonomy, list):
        pairs = set()
        for tag in taxonomy:
            if not isinstance(tag, dict) or tag.get("kind") not in ("genre", "demographic", "theme"):
                break
            raw_id = tag.get("id")
            if not (
                (type(raw_id) is int and raw_id > 0)
                or (isinstance(raw_id, str) and raw_id.isascii() and raw_id.isdecimal() and int(raw_id) > 0)
            ):
                break
            pairs.add((int(raw_id), tag["kind"]))
        else:
            evidence["taxonomy"] = [[tag_id, kind] for tag_id, kind in sorted(pairs)]
    return evidence


def reading_family_inputs(rows, eligibility, metadata):
    """Один проверенный экспорт и общая мета дают независимые входы всех метрик."""
    inputs = {definition.family: {} for definition in READING_ACHIEVEMENTS}
    for key, row in rows.items():
        eligible = eligibility[key]
        for family, field in (("chapters", "chapters"), ("volumes", "volumes")):
            marks = row.get(field)
            inputs[family][key] = {"eligible": eligible, "marks": marks if _nonnegative(marks) else None}
        if row["status"].lower() != "completed":
            continue
        evidence = reading_evidence(metadata.get(key))
        for family, field, qualifies in (
            ("long_works", "chapters", lambda value: value is not None and value >= 100),
            ("pre_2000", "year", lambda value: value is not None and value < 2000),
        ):
            inputs[family][key] = eligible if eligible is not True else (
                bool(qualifies(evidence[field])) if field in evidence else None
            )
        for family, _, kind, tag_id, _ in TAXONOMY_FAMILIES:
            inputs[family][key] = eligible if eligible is not True else (
                [tag_id, kind] in evidence["taxonomy"] if "taxonomy" in evidence else None
            )
    return inputs


def reading_evidence(record):
    """Перепроверяем восстановимый кэш, не принимая повреждённые поля за истину."""
    raw = record.get("reading_evidence") if isinstance(record, dict) else None
    if not isinstance(raw, dict):
        return {}
    source = {}
    if "chapters" in raw:
        source["chapters"] = raw["chapters"]
    if "year" in raw:
        source["airedOn"] = {"year": raw["year"]}
    taxonomy = raw.get("taxonomy")
    if isinstance(taxonomy, list) and all(isinstance(pair, list) and len(pair) == 2 for pair in taxonomy):
        source["genres"] = [{"id": pair[0], "kind": pair[1]} for pair in taxonomy]
    return manga_metadata_evidence(source)
