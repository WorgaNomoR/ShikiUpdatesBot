# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистая квартальная проекция принятой истории и ревизии корректировок."""

import re
from copy import deepcopy
from datetime import (
    datetime,
    timezone,
)

from source_history import (
    content_hash,
    event_at_seq,
    event_count,
    prefix_seq,
    semantic_hash,
    source_suffix,
)

EVENT_TIME_KEY = "event_time"
STAT_TYPES = frozenset({"completed", "dropped", "planned", "rewatching"})
SCORE_TYPES = frozenset({"score_set", "score_changed", "score_removed"})
TIME_REASONS = frozenset({"missing", "naive", "invalid", "future"})


class EventTimeStateError(ValueError):
    """Небезопасное состояние квартальной проекции."""


def _period(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9]{4}-Q[1-4]", value) is not None
        and not value.startswith("0000")
    )


def next_period(period: str) -> str:
    """Следующий календарный квартал, без перескока через пропущенные периоды."""
    if not _period(period) or period == "9999-Q4":
        raise EventTimeStateError("period_successor")
    year, quarter = int(period[:4]), int(period[-1])
    return f"{year + (quarter == 4):04d}-Q{1 if quarter == 4 else quarter + 1}"


def period_start(period: str) -> str:
    """Явная UTC-граница; конец периода — начало следующего квартала."""
    if not _period(period):
        raise EventTimeStateError("period_start")
    return datetime(
        int(period[:4]), 3 * (int(period[-1]) - 1) + 1, 1, tzinfo=timezone.utc
    ).isoformat()


def event_period(event: dict) -> tuple[str | None, str | None]:
    """Будущее относительно первого наблюдения не становится достоверным позже."""
    if event["time_quality"] != "aware":
        return None, event["time_quality"]
    moment = datetime.fromisoformat(event["event_at"]).astimezone(timezone.utc)
    observed = datetime.fromisoformat(event["observed_at"])
    if moment > observed:
        return None, "future"
    return f"{moment.year:04d}-Q{(moment.month - 1) // 3 + 1}", None


def _eligible(event: dict) -> bool:
    return (
        event["relevant"]
        and bool(event["target_id"])
        and event["event_type"] in STAT_TYPES | SCORE_TYPES
    )


def _score(value: object) -> int | None:
    return value if type(value) is int and 1 <= value <= 10 else None


def ensure_event_time(cur: dict) -> bool:
    """Тихая граница миграции: применённая история и legacy-события не пересчитываются."""
    if EVENT_TIME_KEY in cur:
        return False
    cur[EVENT_TIME_KEY] = {
        "version": 1,
        "baseline_seq": cur["event_projection"]["applied_seq"],
        "legacy_period": cur["period"],
        "legacy_events": deepcopy(cur["events"]),
        "periods": {
            cur["period"]: {
                "events": deepcopy(cur["events"]),
                "revision": 0,
                "announced_revision": 0,
            }
        },
        "unknown": dict.fromkeys(sorted(TIME_REASONS), 0),
        "report_ack": None,
    }
    return True


def index_event_periods(cur: dict, journal: dict) -> dict[str, list[dict]]:
    """Временный индекс применённого префикса; после сбоя/restore строится заново."""
    groups, _ = compact_source_groups(cur[EVENT_TIME_KEY], journal)
    for event in source_suffix(journal, cur[EVENT_TIME_KEY]["baseline_seq"], cur["event_projection"]["applied_seq"]):
        if _eligible(event):
            period, _ = event_period(event)
            if period is not None:
                groups.setdefault(period, []).append(event)
    return groups


def _derive(
    state: dict, journal: dict, seq: int, period: str, *, source: list[dict] | None = None
) -> list[dict]:
    """Перестроить только известную post-migration часть одного квартала."""
    result = deepcopy(state["legacy_events"]) if period == state["legacy_period"] else []
    records = {
        (ev.get("media"), str(ev.get("id")), ev.get("event")): ev
        for ev in result
        if isinstance(ev, dict)
        and isinstance(ev.get("media"), str)
        and isinstance(ev.get("event"), str)
        and isinstance(ev.get("id"), (str, int))
    }
    legacy_keys = set(records)
    scores = {
        (media, tid): ev.get("score")
        for (media, tid, kind), ev in records.items()
        if kind == "completed"
    }
    if source is None:
        source = [
            ev
            for ev in source_suffix(journal, state["baseline_seq"], seq)
            if _eligible(ev) and event_period(ev)[0] == period
        ]
        source = compact_source_groups(state, journal)[0].get(period, []) + source
    else:
        source = list(source)
    source.sort(key=lambda ev: (ev["event_at"], ev["history_id"]))
    for event in source:
        media, tid, kind = event["media"], event["target_id"], event["event_type"]
        key = (media, tid)
        score = _score(event["score"])
        if kind in SCORE_TYPES:
            if kind != "score_removed" and score is None:
                continue
            scores[key] = None if kind == "score_removed" else score
            completed = records.get((*key, "completed"))
            if completed is not None:
                # Legacy None сохраняет прежний export-fallback. Явное снятие
                # записываем как 0, иначе None → None потеряет эту семантику.
                completed["score"] = (
                    0 if kind == "score_removed" and (*key, "completed") in legacy_keys
                    else scores[key]
                )
            continue
        if kind == "completed" and score is not None:
            scores[key] = score
            if (*key, kind) in records:
                records[(*key, kind)]["score"] = score
        if (*key, kind) in records:
            continue
        record = {
            "id": tid,
            "media": media,
            "event": kind,
            "score": scores.get(key) if kind == "completed" else score,
            "recorded_at": event["event_at"],
            "title": deepcopy(event["title"]),
            "kind": event["kind"],
        }
        result.append(record)
        records[(*key, kind)] = record
    return result


def project_event(
    cur: dict, journal: dict, seq: int, *, period_events: dict[str, list[dict]] | None = None
) -> None:
    """Caller публикует дельту и applied_seq одной заменой stats_current."""
    state = cur[EVENT_TIME_KEY]
    event = event_at_seq(journal, seq)
    if not _eligible(event):
        return
    period, reason = event_period(event)
    if period is None:
        state["unknown"][reason] += 1
        return
    bucket = state["periods"].setdefault(
        period, {"events": [], "revision": 0, "announced_revision": 0}
    )
    source = None
    if period_events is not None:
        source = period_events.setdefault(period, [])
        source.append(event)
    events = _derive(state, journal, seq, period, source=source)
    if events != bucket["events"]:
        bucket["events"] = events
        bucket["revision"] += 1
    if period == cur["period"]:
        cur["events"] = deepcopy(events)


def correction_periods(cur: dict) -> list[str]:
    """Новые ревизии закрытых периодов, ещё не включённые в подтверждённый отчёт."""
    return sorted(
        period
        for period, bucket in cur[EVENT_TIME_KEY]["periods"].items()
        if period < cur["period"] and bucket["revision"] > bucket["announced_revision"]
    )


def report_revisions(cur: dict) -> dict[str, int]:
    """Watermark исходного отчёта и поправок войдёт в хеш frozen plan."""
    state = cur[EVENT_TIME_KEY]
    revisions = {period: state["periods"][period]["revision"] for period in correction_periods(cur)}
    revisions[cur["period"]] = state["periods"].get(cur["period"], {}).get("revision", 0)
    return revisions


def rotate_event_time(cur: dict, fresh: dict, plan: dict) -> None:
    """Заморозить только watermark ревизий, не живые события в pending report."""
    state = deepcopy(cur[EVENT_TIME_KEY])
    state["periods"].setdefault(
        cur["period"], {"events": deepcopy(cur["events"]), "revision": 0, "announced_revision": 0}
    )
    revisions = dict(plan["event_time_revisions"])
    state["report_ack"] = {"plan_id": plan["plan_id"], "revisions": revisions}
    fresh[EVENT_TIME_KEY] = state
    fresh["period_start"] = period_start(fresh["period"])
    fresh["tracking_since"] = fresh["period_start"]
    fresh["events"] = deepcopy(state["periods"].get(fresh["period"], {}).get("events", []))


def acknowledge_revisions(cur: dict) -> None:
    """Поздние изменения сверх frozen watermark остаются задолженностью."""
    state = cur.get(EVENT_TIME_KEY)
    if state is None or state["report_ack"] is None:
        return
    for period, revision in state["report_ack"]["revisions"].items():
        bucket = state["periods"][period]
        bucket["announced_revision"] = max(bucket["announced_revision"], revision)


def validate_event_time(cur: dict, journal: dict | None = None) -> None:
    """Один структурный и recovery-контракт для диска, публикации и импорта."""
    state = cur.get(EVENT_TIME_KEY)
    pending = cur.get("pending_quarter_delivery")
    if state is None and EVENT_TIME_KEY not in cur:
        if isinstance(pending, dict) and pending.get("version") == 3:
            raise EventTimeStateError("event_time_missing")
        return
    projection = cur.get("event_projection")
    if (
        not isinstance(state, dict)
        or set(state)
        != {
            "version",
            "baseline_seq",
            "legacy_period",
            "legacy_events",
            "periods",
            "unknown",
            "report_ack",
        }
        or type(state["version"]) is not int
        or state["version"] != 1
        or not isinstance(projection, dict)
        or type(projection.get("baseline_seq")) is not int
        or type(projection.get("applied_seq")) is not int
        or type(state["baseline_seq"]) is not int
        or not _period(cur.get("period"))
        or not isinstance(cur.get("events"), list)
        or not projection["baseline_seq"] <= state["baseline_seq"] <= projection["applied_seq"]
        or not _period(state["legacy_period"])
        or state["legacy_period"] > cur["period"]
        or not isinstance(state["legacy_events"], list)
        or any(not isinstance(ev, dict) for ev in state["legacy_events"])
        or not isinstance(state["periods"], dict)
        or not isinstance(state["unknown"], dict)
        or set(state["unknown"]) != TIME_REASONS
        or any(type(count) is not int or count < 0 for count in state["unknown"].values())
        or sum(state["unknown"].values()) > projection["applied_seq"] - state["baseline_seq"]
    ):
        raise EventTimeStateError("event_time_structure")
    for period, bucket in state["periods"].items():
        if (
            not _period(period)
            or not isinstance(bucket, dict)
            or set(bucket) != {"events", "revision", "announced_revision"}
            or not isinstance(bucket["events"], list)
            or any(not isinstance(ev, dict) for ev in bucket["events"])
            or type(bucket["revision"]) is not int
            or type(bucket["announced_revision"]) is not int
            or not 0
            <= bucket["announced_revision"]
            <= bucket["revision"]
            <= projection["applied_seq"] - state["baseline_seq"]
        ):
            raise EventTimeStateError("event_time_bucket")
        known = set()
        for ev in bucket["events"]:
            legacy_match = period == state["legacy_period"] and any(
                {key: value for key, value in ev.items() if key != "score"}
                == {key: value for key, value in old.items() if key != "score"}
                for old in state["legacy_events"]
            )
            if legacy_match:
                if (
                    ev not in state["legacy_events"]
                    and ev.get("score") is not None
                    and _score(ev.get("score")) is None
                    and not (
                        ev.get("event") == "completed"
                        and type(ev.get("score")) is int and ev["score"] == 0
                    )
                ):
                    raise EventTimeStateError("event_time_legacy_score")
                continue
            if (
                set(ev) != {"id", "media", "event", "score", "recorded_at", "title", "kind"}
                or not isinstance(ev["id"], str)
                or not ev["id"]
                or not isinstance(ev["media"], str)
                or ev["media"] not in {"anime", "manga"}
                or not isinstance(ev["event"], str)
                or ev["event"] not in STAT_TYPES
                or (ev["score"] is not None and _score(ev["score"]) is None)
                or not isinstance(ev["kind"], str)
                or not isinstance(ev["title"], dict)
                or set(ev["title"]) != {"name", "russian", "url"}
                or any(not isinstance(text, str) for text in ev["title"].values())
            ):
                raise EventTimeStateError("event_time_record")
            try:
                moment = datetime.fromisoformat(ev["recorded_at"])
                if (
                    moment.tzinfo is None
                    or moment.astimezone(timezone.utc).isoformat() != ev["recorded_at"]
                ):
                    raise ValueError
                if f"{moment.year:04d}-Q{(moment.month - 1) // 3 + 1}" != period:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                raise EventTimeStateError("event_time_record_time") from None
            key = (ev["media"], ev["id"], ev["event"])
            if key in known:
                raise EventTimeStateError("event_time_duplicate")
            known.add(key)
    if cur["events"] != state["periods"].get(cur["period"], {}).get("events", []):
        raise EventTimeStateError("event_time_current")
    ack = state["report_ack"]
    if ack is not None:
        if (
            not isinstance(ack, dict)
            or set(ack) != {"plan_id", "revisions"}
            or not isinstance(pending, dict)
            or ack["plan_id"] != pending.get("plan_id")
            or not isinstance(ack["revisions"], dict)
            or pending.get("old_period") not in ack["revisions"]
            or pending.get("event_time_revisions") != ack["revisions"]
        ):
            raise EventTimeStateError("event_time_ack")
        for period, revision in ack["revisions"].items():
            if (
                period not in state["periods"]
                or period >= cur["period"]
                or type(revision) is not int
                or not 0 <= revision <= state["periods"][period]["revision"]
            ):
                raise EventTimeStateError("event_time_ack_revision")
            if (
                cur.get("last_report_sent") == cur["period"]
                and state["periods"][period]["announced_revision"] < revision
            ):
                raise EventTimeStateError("event_time_unacknowledged_completion")
    elif isinstance(pending, dict) and pending.get("version") == 3:
        raise EventTimeStateError("event_time_ack_missing")
    if journal is not None:
        applied = projection["applied_seq"]
        if applied > event_count(journal):
            raise EventTimeStateError("event_time_recovery")
        groups, unknown = compact_source_groups(state, journal)
        for ev in source_suffix(journal, state["baseline_seq"], applied):
            if _eligible(ev):
                period, reason = event_period(ev)
                if period is None:
                    unknown[reason] += 1
                else:
                    groups.setdefault(period, []).append(ev)
        if (
            unknown != state["unknown"]
            or not groups.keys() <= state["periods"].keys()
            or state["legacy_period"] not in state["periods"]
        ):
            raise EventTimeStateError("event_time_recovery_counts")
        for period, bucket in state["periods"].items():
            expected = _derive(state, journal, applied, period, source=groups.get(period, []))
            if expected != bucket["events"]:
                raise EventTimeStateError("event_time_recovery_payload")


_SOURCE_FIELDS = {
    "seq", "history_id", "event_at", "event_type", "media", "target_id",
    "kind", "score", "title",
}
_SOURCE_SCORE_FIELDS = {"seq", "history_id", "event_at", "event_type", "media", "target_id", "score"}


def _source_binding(state: dict) -> dict:
    """Привязка к неизменной legacy-семантике, а не к готовой проекции."""
    return {
        "baseline_seq": state["baseline_seq"],
        "legacy_period": state["legacy_period"],
        "legacy_hash": content_hash(state["legacy_events"]),
    }


def compact_source_groups(state: dict, journal: dict) -> tuple[dict, dict]:
    """Старая quarter-only граница исключает прежние compact source факты."""
    base = journal.get("source_base")
    unknown = dict.fromkeys(sorted(TIME_REASONS), 0)
    if base is None or state["baseline_seq"] >= base["through_seq"]:
        return {}, unknown
    if base["binding"] != _source_binding(state):
        raise EventTimeStateError("source_binding")
    return deepcopy(base["periods"]), dict(base["unknown"])


def _minimal_sources(source: list[dict]) -> list[dict]:
    """Первые записи определяют порядок/metadata; последняя оценка — итог.

    Для каждой статистической тройки reducer добавляет только первую запись.
    Все присваивания оценки одного title заменяют предыдущее; достаточно
    последнего, включая оценённый повтор completed. При произвольной поздней
    вставке min(first, new) и max(last, new) дают те же факты и порядок.
    """
    first, last = {}, {}
    for event in sorted(source, key=lambda ev: (ev["event_at"], ev["history_id"])):
        key = (event["media"], event["target_id"])
        kind = event["event_type"]
        if kind in STAT_TYPES:
            first.setdefault((*key, kind), event)
        if kind == "score_removed" or kind in SCORE_TYPES | {"completed"} and _score(event["score"]) is not None:
            last[key] = event
    selected = {event["seq"]: event for event in [*first.values(), *last.values()]}
    first_seq = {event["seq"] for event in first.values()}
    result = []
    for event in sorted(selected.values(), key=lambda ev: (ev["event_at"], ev["history_id"])):
        if event["seq"] in first_seq:
            result.append({
                **{key: deepcopy(event[key]) for key in sorted(_SOURCE_FIELDS)},
                "score": _score(event["score"]),
            })
        else:
            # Присваиванию не нужны title/kind или семантика повторного completed.
            result.append({
                **{key: event[key] for key in sorted(_SOURCE_SCORE_FIELDS)},
                "event_type": "score_removed" if event["event_type"] == "score_removed" else "score_set",
                "score": None if event["event_type"] == "score_removed" else _score(event["score"]),
            })
    return result


def compact_source_history(journal: dict, cur: dict, through: int) -> dict:
    """Чистый candidate; caller проверяет consumers и публикует один member."""
    state = cur[EVENT_TIME_KEY]
    periods, unknown = compact_source_groups(state, journal)
    old = journal.get("source_base")
    ids = list(old["ids"]) if old else []
    removed = source_suffix(journal, prefix_seq(journal), through)
    ids.extend([event["history_id"], semantic_hash(event)] for event in removed)
    for event in removed:
        if event["seq"] <= state["baseline_seq"] or not _eligible(event):
            continue
        period, reason = event_period(event)
        if period is None:
            unknown[reason] += 1
        else:
            periods.setdefault(period, []).append(event)
    base = {
        "version": 1, "through_seq": through, "ids": ids,
        "binding": _source_binding(state),
        "periods": {period: _minimal_sources(source) for period, source in sorted(periods.items())},
        "unknown": unknown,
    }
    base["checksum"] = content_hash(base)
    return {**journal, "source_base": base, "events": journal["events"][through - prefix_seq(journal):]}


def validate_source_base(journal: dict) -> None:
    """Полная форма compact source, точные ID, checksum и canonical min/max."""
    if "source_base" not in journal:
        return
    base = journal["source_base"]
    def digest(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

    if (
        not isinstance(base, dict)
        or set(base) != {"version", "through_seq", "ids", "binding", "periods", "unknown", "checksum"}
        or type(base["version"]) is not int or base["version"] != 1
        or type(base["through_seq"]) is not int or not 1 <= base["through_seq"] <= journal["processed_seq"]
        or not isinstance(base["ids"], list) or len(base["ids"]) != base["through_seq"]
        or not isinstance(base["binding"], dict)
        or set(base["binding"]) != {"baseline_seq", "legacy_period", "legacy_hash"}
        or type(base["binding"]["baseline_seq"]) is not int
        or not 0 <= base["binding"]["baseline_seq"] <= min(journal["processed_seq"] + 1, event_count(journal))
        or not _period(base["binding"]["legacy_period"]) or not digest(base["binding"]["legacy_hash"])
        or not isinstance(base["periods"], dict)
        or not isinstance(base["unknown"], dict) or set(base["unknown"]) != TIME_REASONS
        or any(type(count) is not int or count < 0 for count in base["unknown"].values())
        or sum(base["unknown"].values()) > max(0, base["through_seq"] - base["binding"]["baseline_seq"])
        or not digest(base["checksum"])
        or base["checksum"] != content_hash({key: value for key, value in base.items() if key != "checksum"})
    ):
        raise EventTimeStateError("source_structure")
    known = set(journal["baseline_ids"])
    for item in base["ids"]:
        if (
            not isinstance(item, list) or len(item) != 2
            or type(item[0]) is not int or item[0] in known or not digest(item[1])
        ):
            raise EventTimeStateError("source_ids")
        known.add(item[0])
    retained_seq = set()
    for period, source in base["periods"].items():
        if not _period(period) or not isinstance(source, list):
            raise EventTimeStateError("source_period")
        for ev in source:
            if (
                not isinstance(ev, dict) or set(ev) != (
                    _SOURCE_SCORE_FIELDS if ev.get("event_type") in SCORE_TYPES else _SOURCE_FIELDS
                )
                or type(ev["seq"]) is not int
                or not base["binding"]["baseline_seq"] < ev["seq"] <= base["through_seq"]
                or ev["seq"] in retained_seq
                or type(ev["history_id"]) is not int or ev["history_id"] != base["ids"][ev["seq"] - 1][0]
                or not isinstance(ev["media"], str) or ev["media"] not in {"anime", "manga"}
                or not isinstance(ev["target_id"], str) or not ev["target_id"]
                or not isinstance(ev["event_type"], str) or ev["event_type"] not in STAT_TYPES | SCORE_TYPES
                or (ev["score"] is not None and _score(ev["score"]) is None)
            ):
                raise EventTimeStateError("source_record")
            if ev["event_type"] in STAT_TYPES and (
                not isinstance(ev["kind"], str)
                or not isinstance(ev["title"], dict) or set(ev["title"]) != {"name", "russian", "url"}
                or any(not isinstance(text, str) for text in ev["title"].values())
            ):
                raise EventTimeStateError("source_record")
            try:
                moment = datetime.fromisoformat(ev["event_at"])
                if (
                    moment.tzinfo is None or moment.astimezone(timezone.utc).isoformat() != ev["event_at"]
                    or f"{moment.year:04d}-Q{(moment.month - 1) // 3 + 1}" != period
                ):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                raise EventTimeStateError("source_time") from None
            retained_seq.add(ev["seq"])
        if _minimal_sources(source) != source:
            raise EventTimeStateError("source_noncanonical")
