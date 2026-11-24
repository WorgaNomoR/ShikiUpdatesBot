# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Изоляция категорий, повреждённые поля и достоверность локального среза."""

from copy import deepcopy
from datetime import (
    datetime,
    timezone,
)

import pytest

from storage import (
    STATS_ALL_INVALID,
    STATS_ALL_MISSING,
    STATS_ALL_VALID,
    StatsAllSnapshot,
)
from taste_card_data import build_taste_input

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)


def _snapshot(titles, *, media="manga", timestamp=None):
    data = {"anime": {"titles": {}}, "manga": {"titles": {}}, "updated_at": NOW.isoformat()}
    data[media] = {"titles": titles, "list_synced_at": timestamp}
    return StatsAllSnapshot(data, STATS_ALL_VALID)


def _build(snapshot, category="manga"):
    return build_taste_input(snapshot, category, profile="WNR", base_url="https://shikimori.io", now=NOW)


def _record(kind="manga", **kwargs):
    return {"kind": kind, "status": "completed", "score": 8, "title": "Название", **kwargs}


def test_category_isolation_unknown_kinds_and_no_mutation():
    snapshot = _snapshot({
        "1": _record(), "2": _record("manhwa"), "3": _record("novel"),
        "4": _record("light_novel"), "5": _record("future"),
        "6": _record("one_shot"), "7": _record("ranobe"),
    }, timestamp="2026-11-17T23:30:00+03:00")
    before = deepcopy(snapshot)
    manga, ranobe = _build(snapshot), _build(snapshot, "ranobe")
    assert [title.id for title in manga.titles] == [1, 2]
    assert [title.id for title in ranobe.titles] == [3, 4, 7]
    assert manga.unresolved == ranobe.unresolved == 1
    assert manga.synced_at == ranobe.synced_at == datetime(2026, 11, 17, 20, 30, tzinfo=timezone.utc)
    assert ranobe.titles[-1].kind == "light_novel"
    assert snapshot == before


def test_statuses_and_independent_bad_metadata_remain_unavailable():
    snapshot = _snapshot({
        "1": _record(score=0, status="dropped", shiki_score=float("inf"), year=True),
        "2": _record(status="planned", score=10),
        "3": _record(status="bad"),
        "4": _record(genres=[" Драма ", "драма", None], themes="Драма", shiki_score="8.7",
                     year=2027, url="javascript:bad"),
    })
    result = _build(snapshot)
    assert result.excluded == 1
    assert result.titles[0].score is None and result.titles[0].community is None
    assert result.titles[0].year is None
    assert result.titles[1].status == "planned"
    assert result.titles[2].genres == ("Драма",) and result.titles[2].themes == ()
    assert result.titles[2].year is None
    assert result.titles[2].url == "https://shikimori.io/mangas/4"


def test_duplicate_ids_collapse_but_conflict_quarantines_complete_id():
    record = _record()
    result = _build(_snapshot({"1": record, "01": dict(record), "2": record}))
    assert [title.id for title in result.titles] == [1, 2]
    result = _build(_snapshot({"1": record, "01": _record(score=9), "2": record}))
    assert [title.id for title in result.titles] == [2]
    assert result.excluded == 1


@pytest.mark.parametrize("timestamp", [None, "bad", "2027-01-01T00:00:00+00:00", "2026-11-17T00:00:00"])
def test_global_cache_date_cannot_substitute_missing_trusted_domain_date(timestamp):
    result = _build(_snapshot({"1": _record()}, timestamp=timestamp))
    assert result.synced_at is None and result.state == "ready"


@pytest.mark.parametrize(("state", "expected"), [
    (STATS_ALL_MISSING, "missing"), (STATS_ALL_INVALID, "invalid"),
])
def test_snapshot_read_states_are_not_fabricated_empty_categories(state, expected):
    snapshot = _snapshot({})
    result = _build(StatsAllSnapshot(snapshot.data, state))
    assert result.state == expected


def test_confirmed_empty_legacy_empty_unknown_and_corrupt_categories_are_distinct():
    assert _build(_snapshot({})).state == "missing"
    assert _build(_snapshot({}, timestamp=NOW.isoformat())).state == "empty"
    assert _build(_snapshot({"1": _record("future")})).state == "unresolved"
    assert _build(_snapshot({"1": None})).state == "invalid"
    assert _build(_snapshot({"1": _record(status="planned")})).state == "ready"


def test_valid_sibling_survives_damaged_domain():
    snapshot = _snapshot({"1": _record("tv")}, media="anime")
    snapshot.data["manga"] = None
    assert _build(snapshot, "anime").state == "ready"
    assert _build(snapshot, "manga").state == "invalid"
