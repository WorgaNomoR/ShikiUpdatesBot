# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Сохранённые исходные кварталы, корректировки и границы публикации."""

import io
import json
import zipfile
from unittest.mock import AsyncMock

import pytest

import backup
import handlers
import stats
import storage
from event_time_stats import (
    correction_periods,
    project_event,
    validate_event_time,
)
from messages import normalize_history_event
from report_model import rendered_html


@pytest.fixture
async def event_time_env(backup_env, monkeypatch):
    storage.save_stats_current({"period": "2026-Q1", "events": []}, strict=True)
    await handlers._initialize_history_journal({1}, storage.restorable_restore_generation())
    monkeypatch.setattr("handlers.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("handlers.send_to_all_chats", AsyncMock())
    monkeypatch.setattr("handlers.send_backup", AsyncMock(return_value=True))
    monkeypatch.setattr("handlers.current_quarter", lambda: "2026-Q4")
    return backup_env


def _append(history_id, source, event_type="completed", score=8, target=None):
    journal = storage.load_event_journal()
    event = normalize_history_event(
        {
            "id": history_id,
            "created_at": source,
            "description": "Просмотрено",
            "target": {"id": target or history_id, "kind": "tv", "name": f"Title {history_id}"},
        },
        "2026-10-01T00:00:00+00:00",
    )
    event.update(event_type=event_type, score=score, seq=len(journal["events"]) + 1)
    journal["events"].append(event)
    storage.save_event_journal(journal, admitting=True)


def _archive():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zf:
        zf.writestr("event_journal.json", storage.EVENT_JOURNAL_FILE.read_bytes())
        zf.writestr("stats_current.json", storage.STATS_CURRENT_FILE.read_bytes())
    return stream.getvalue()


@pytest.mark.asyncio
async def test_three_missed_quarters_publish_separate_snapshots_and_plans(event_time_env):
    for history_id, source in [
        (90, "2026-07-01T00:00:00Z"),
        (2, "2026-01-01T00:00:00Z"),
        (10, "2026-04-01T00:00:00Z"),
    ]:
        _append(history_id, source)
    await handlers._drain_history_journal(AsyncMock())
    stats_all = storage._empty_stats_all()
    for old, new, title_id in [
        ("2026-Q1", "2026-Q2", "2"),
        ("2026-Q2", "2026-Q3", "10"),
        ("2026-Q3", "2026-Q4", "90"),
    ]:
        returned = await handlers.rotate_quarter_if_needed(AsyncMock(), {}, stats_all, resync=False)
        assert returned["period"] == new
        frozen = json.loads(
            (event_time_env / "quarters" / f"{old}.json").read_text(encoding="utf-8")
        )
        assert [event["id"] for event in frozen["events"]] == [title_id]
        assert frozen["time_basis"] == "UTC"
        assert frozen["history_complete"] is False
        assert returned["event_projection"]["applied_seq"] == 3
    assert handlers.send_backup.await_count == 3
    assert handlers.send_to_all_chats.await_count == 3
    assert all(
        value["completed"] == 1 for value in stats_all["anime"]["aggregates"]["by_quarter"].values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [2, 12])
async def test_drain_scans_source_prefix_once_and_resumes_applied_event(
    event_time_env, journal_factory, monkeypatch, pending,
):
    journal = journal_factory(count=20, processed=20 - pending)
    cur = storage.load_stats_current(strict=True)
    cur["event_projection"]["journal_id"] = journal["journal_id"]
    for seq, event in enumerate(journal["events"], 1):
        quarter = (seq - 1) % 3 + 1
        source = f"2026-{3 * (quarter - 1) + 1:02d}-02T00:00:00+00:00"
        event.update(created_at=source, event_at=source, observed_at="2026-10-01T00:00:00+00:00")
        if seq <= journal["processed_seq"] + 1:
            project_event(cur, journal, seq)
            cur["event_projection"]["applied_seq"] = seq
    storage.save_event_journal(journal)
    storage.save_stats_current(cur, strict=True)
    prefix_reads = []

    class TracedEvents(list):
        def __getitem__(self, key):
            result = super().__getitem__(key)
            if isinstance(key, slice):
                prefix_reads.append(len(result))
            return result

    real_load = storage.load_event_journal

    def load():
        published = real_load()
        published["events"] = TracedEvents(published["events"])
        return published

    monkeypatch.setattr("handlers.load_event_journal", load)
    await handlers._drain_history_journal(AsyncMock())

    # Полная recovery-сверка и индекс читают префикс до цикла; размер
    # очереди не добавляет повторных проходов по всей исходной истории.
    assert len(prefix_reads) <= 2
    published = real_load()
    projected = storage.load_stats_current(strict=True)
    validate_event_time(projected, published)
    assert published["processed_seq"] == projected["event_projection"]["applied_seq"] == 20
    assert [len(projected["event_time"]["periods"][f"2026-Q{q}"]["events"]) for q in (1, 2, 3)] == [7, 7, 6]
    assert handlers.send_to_all_chats.await_count == pending


@pytest.mark.asyncio
async def test_drain_rejects_inconsistent_applied_prefix_before_publication(event_time_env):
    _append(2, "2026-01-01T00:00:00Z")
    await handlers._drain_history_journal(AsyncMock())
    cur = storage.load_stats_current(strict=True)
    cur["events"][0]["score"] = 9
    cur["event_time"]["periods"]["2026-Q1"]["events"][0]["score"] = 9
    storage.save_stats_current(cur, strict=True)
    _append(3, "2026-04-01T00:00:00Z")
    before = _archive()
    handlers.send_to_all_chats.reset_mock()

    with pytest.raises(storage.EventJournalStateError):
        await handlers._drain_history_journal(AsyncMock())

    assert _archive() == before
    handlers.send_to_all_chats.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_events_correct_cache_and_next_report_without_rewriting_original(event_time_env):
    _append(2, "2026-01-01T00:00:00Z")
    await handlers._drain_history_journal(AsyncMock())
    await handlers.rotate_quarter_if_needed(
        AsyncMock(), {}, storage._empty_stats_all(), resync=False
    )
    snapshot = event_time_env / "quarters" / "2026-Q1.json"
    original = snapshot.read_bytes()
    _append(3, "2026-01-02T00:00:00Z", "score_removed", None, target=2)
    _append(4, "2026-01-03T00:00:00Z")
    await handlers._drain_history_journal(AsyncMock())
    cur = storage.load_stats_current(strict=True)
    assert correction_periods(cur) == ["2026-Q1"]
    assert cur["event_time"]["periods"]["2026-Q1"]["events"][0]["score"] is None
    assert (
        storage.load_stats_all()["anime"]["aggregates"]["by_quarter"]["2026-Q1"]["completed"] == 2
    )
    report = "\n".join(
        rendered_html(stats.build_quarterly_report_messages(cur, storage._empty_stats_all(), None))
    )
    assert "Корректировка за январь — март 2026" in report
    assert "без оценки" in report
    await handlers.rotate_quarter_if_needed(
        AsyncMock(), {}, storage._empty_stats_all(), resync=False
    )
    assert snapshot.read_bytes() == original
    assert correction_periods(storage.load_stats_current(strict=True)) == []


@pytest.mark.asyncio
async def test_late_change_during_frozen_delivery_survives_revision_ack(event_time_env):
    _append(2, "2026-01-01T00:00:00Z")
    await handlers._drain_history_journal(AsyncMock())
    bot = AsyncMock()
    changed = False

    async def insert_during_send(**kwargs):
        nonlocal changed
        if not changed:
            changed = True
            _append(3, "2026-01-02T00:00:00Z", "score_changed", 5, target=2)
            await handlers._drain_history_journal(AsyncMock())

    bot.send_rich_message.side_effect = insert_during_send
    await handlers.rotate_quarter_if_needed(bot, {}, storage._empty_stats_all(), resync=False)
    cur = storage.load_stats_current(strict=True)
    bucket = cur["event_time"]["periods"]["2026-Q1"]
    assert (bucket["announced_revision"], bucket["revision"]) == (1, 2)
    assert correction_periods(cur) == ["2026-Q1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["snapshot", "plan", "ack"])
async def test_failed_publication_keeps_source_projection_and_resumes(
    event_time_env, monkeypatch, boundary
):
    _append(2, "2026-01-01T00:00:00Z")
    await handlers._drain_history_journal(AsyncMock())
    original = storage.STATS_CURRENT_FILE.read_bytes()
    real_write = storage._atomic_write

    def fail(path, payload):
        candidate = json.loads(payload)
        if (
            boundary == "snapshot"
            and path.parent.name == "quarters"
            or boundary == "plan"
            and path == storage.STATS_CURRENT_FILE
            and candidate["period"] == "2026-Q2"
            or boundary == "ack"
            and path == storage.STATS_CURRENT_FILE
            and candidate.get("pending_quarter_delivery", {}).get("next_unit", 0) == 1
        ):
            raise OSError("disk failure")
        return real_write(path, payload)

    bot = AsyncMock()
    with monkeypatch.context() as patch:
        patch.setattr("storage._atomic_write", fail)
        patch.setattr("stats._atomic_write", fail)
        await handlers.rotate_quarter_if_needed(bot, {}, storage._empty_stats_all(), resync=False)
    cur = storage.load_stats_current(strict=True)
    if boundary in {"snapshot", "plan"}:
        assert storage.STATS_CURRENT_FILE.read_bytes() == original
        bot.send_rich_message.assert_not_awaited()
    else:
        assert cur["pending_quarter_delivery"]["next_unit"] == 0
        assert cur["event_time"]["periods"]["2026-Q1"]["announced_revision"] == 0
    await handlers.rotate_quarter_if_needed(
        AsyncMock(), {}, storage._empty_stats_all(), resync=False
    )
    cur = storage.load_stats_current(strict=True)
    assert cur["period"] == "2026-Q2"
    assert cur["pending_quarter_delivery"] is None
    assert len(cur["event_time"]["periods"]["2026-Q1"]["events"]) == 1
    assert cur["event_projection"]["applied_seq"] == 1


@pytest.mark.asyncio
async def test_restore_during_projection_broadcast_preserves_new_state_without_stale_ack(
    event_time_env,
):
    _append(2, "2026-01-01T00:00:00Z")
    captured = _archive()

    async def restore(*args, **kwargs):
        await backup.restore_backup_zip(captured)

    handlers.send_to_all_chats.side_effect = restore
    with pytest.raises(handlers._HistoryAttemptChanged):
        await handlers._drain_history_journal(AsyncMock())
    cur = storage.load_stats_current(strict=True)
    assert cur["event_projection"]["applied_seq"] == 0
    assert cur["event_time"]["periods"]["2026-Q1"]["events"] == []
    handlers.send_to_all_chats.side_effect = None
    await handlers._drain_history_journal(AsyncMock())
    assert len(storage.load_stats_current(strict=True)["events"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["render", "send", "backup"])
async def test_identical_restore_stops_rotation_or_ack_at_each_delivery_phase(
    event_time_env, monkeypatch, phase
):
    _append(2, "2026-01-01T00:00:00Z")
    await handlers._drain_history_journal(AsyncMock())
    bot = AsyncMock()
    if phase == "render":
        real_freeze = handlers.freeze_report

        def restore_generation(report):
            storage.mark_restorable_state_restored()
            return real_freeze(report)

        monkeypatch.setattr("handlers.freeze_report", restore_generation)
    else:

        async def restore(**kwargs):
            await backup.restore_backup_zip(_archive())
            return True

        if phase == "send":
            bot.send_rich_message.side_effect = restore
        else:

            async def restore_during_backup(*args):
                return await restore()

            handlers.send_backup.side_effect = restore_during_backup
    cur = await handlers.rotate_quarter_if_needed(bot, {}, storage._empty_stats_all(), resync=False)
    if phase == "render":
        assert cur["period"] == "2026-Q1"
        bot.send_rich_message.assert_not_awaited()
    else:
        assert cur["pending_quarter_delivery"] is not None
        if phase == "send":
            assert cur["pending_quarter_delivery"]["next_unit"] == 0
            assert cur["event_time"]["periods"]["2026-Q1"]["announced_revision"] == 0


@pytest.mark.asyncio
async def test_pending_legacy_plan_migrates_silently_without_reprojection_or_rerender(
    event_time_env, monkeypatch
):
    cur = storage.load_stats_current(strict=True)
    cur.pop("event_time")
    cur["period"] = "2026-Q2"
    cur["events"] = [{"id": "legacy", "event": "completed", "media": "anime", "score": 7}]
    cur["pending_quarter_delivery"] = storage.new_quarter_delivery(
        "2026-Q1", "2026-Q2", ["old first", "old remaining"]
    )
    cur["pending_quarter_delivery"]["next_unit"] = 1
    storage.save_stats_current(cur, strict=True)
    monkeypatch.setattr(
        "handlers.build_quarterly_report_messages", lambda *args: pytest.fail("rerender")
    )
    bot = AsyncMock()
    returned = await handlers.rotate_quarter_if_needed(bot, {}, {}, resync=False)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == ["old remaining"]
    assert returned["events"] == cur["events"]
    assert returned["event_time"]["baseline_seq"] == 0
    handlers.send_to_all_chats.assert_not_awaited()


def test_removed_source_score_never_falls_back_to_today_export():
    cur = {
        "events": [
            {
                "id": "1",
                "media": "anime",
                "event": "completed",
                "score": None,
                "title": {"name": "Source", "russian": "", "url": ""},
                "kind": "tv",
            }
        ]
    }
    stats_all = storage._empty_stats_all()
    stats_all["anime"]["titles"]["1"] = {"title": "Cached", "score": 10}
    assert stats._quarter_titles(cur, stats_all, "anime", "completed")[0]["score"] == 0


@pytest.mark.asyncio
async def test_v3_unsupported_rich_rebinds_exact_revision_map_before_html(event_time_env):
    from aiogram.exceptions import TelegramNotFound
    from aiogram.methods import SendRichMessage

    _append(2, "2026-01-01T00:00:00Z")
    await handlers._drain_history_journal(AsyncMock())
    handlers.send_backup.return_value = False
    bot = AsyncMock()

    async def unsupported(**kwargs):
        raise TelegramNotFound(method=SendRichMessage(**kwargs), message="Not Found")

    bot.send_rich_message.side_effect = unsupported
    cur = await handlers.rotate_quarter_if_needed(bot, {}, storage._empty_stats_all(), resync=False)
    pending = cur["pending_quarter_delivery"]
    assert pending["version"] == 3
    assert all(unit["transport"] == "html" for unit in pending["report_units"])
    assert cur["event_time"]["report_ack"]["plan_id"] == pending["plan_id"]
    assert cur["event_time"]["report_ack"]["revisions"] == pending["event_time_revisions"]
    assert cur["event_time"]["periods"]["2026-Q1"]["announced_revision"] == 1
    sent = bot.send_message.await_count
    handlers.send_backup.return_value = True
    await handlers.rotate_quarter_if_needed(bot, {}, {}, resync=False)
    assert bot.send_message.await_count == sent


@pytest.mark.asyncio
async def test_upgrade_after_projection_keeps_observation_legacy_without_reapplying(event_time_env):
    _append(2, "2026-04-01T00:00:00Z")
    cur = storage.load_stats_current(strict=True)
    cur.pop("event_time")
    cur["events"] = [{"id": "2", "media": "anime", "event": "completed", "score": 8}]
    cur["event_projection"]["applied_seq"] = 1
    storage.save_stats_current(cur, strict=True)
    await handlers._drain_history_journal(AsyncMock())
    upgraded = storage.load_stats_current(strict=True)
    assert upgraded["events"] == cur["events"]
    assert upgraded["event_time"]["baseline_seq"] == 1
    assert upgraded["event_time"]["periods"]["2026-Q1"]["revision"] == 0
    assert "2026-Q2" not in upgraded["event_time"]["periods"]
    assert storage.load_event_journal()["processed_seq"] == 1


@pytest.mark.asyncio
async def test_pre_migration_quarter_cache_retains_original_totals(event_time_env):
    cur = storage.load_stats_current(strict=True)
    cur["period"] = "2026-Q2"
    cur.pop("event_time")
    from event_time_stats import ensure_event_time

    ensure_event_time(cur)
    storage.save_stats_current(cur, strict=True)
    cached = storage._empty_stats_all()
    original = {"completed": 20, "avg_score": 9.0, "episodes_watched": 200}
    cached["anime"]["aggregates"]["by_quarter"] = {"2026-Q1": dict(original)}
    storage.save_stats_all(cached)
    _append(2, "2026-01-01T00:00:00Z", "score_removed", None)
    await handlers._drain_history_journal(AsyncMock())
    assert storage.load_stats_all()["anime"]["aggregates"]["by_quarter"]["2026-Q1"] == original
    _append(3, "2026-01-02T00:00:00Z")
    await handlers._drain_history_journal(AsyncMock())
    corrected = storage.load_stats_all()["anime"]["aggregates"]["by_quarter"]["2026-Q1"]
    assert {key: corrected[key] for key in original} == original
    assert corrected["event_time_partial"]["completed"] == 1
    assert corrected["history_complete"] is False
    cur = storage.load_stats_current(strict=True)
    current = storage._empty_stats_all()
    assert stats.refresh_event_time_by_quarter(current, cur)
    assert not stats.refresh_event_time_by_quarter(current, cur)
    assert "completed" not in current["anime"]["aggregates"]["by_quarter"]["2026-Q1"]


@pytest.mark.asyncio
async def test_uncertain_v3_plan_retains_corrections_through_restore_and_later_rejection(
    event_time_env, monkeypatch,
):
    from aiogram.exceptions import TelegramNotFound
    from aiogram.methods import SendRichMessage
    from aiogram.types import (
        InputRichBlockParagraph,
        InputRichMessage,
    )

    monkeypatch.setattr("telegram_delivery._sleep", AsyncMock())
    _append(2, "2026-01-01T00:00:00Z")
    await handlers._drain_history_journal(AsyncMock())
    bot = AsyncMock()
    bot.send_rich_message.side_effect = TimeoutError()
    await handlers.rotate_quarter_if_needed(bot, {}, storage._empty_stats_all(), resync=False)
    cur = storage.load_stats_current(strict=True)
    plan = cur["pending_quarter_delivery"]
    assert plan["version"] == 3 and plan["delivery_uncertain"] is True
    assert plan["next_unit"] == 0
    assert cur["event_time"]["periods"]["2026-Q1"]["announced_revision"] == 0
    handlers.send_backup.assert_not_awaited()
    captured = _archive()
    await backup.restore_backup_zip(captured)
    assert storage.load_stats_current(strict=True) == cur
    bot.send_rich_message.side_effect = TelegramNotFound(
        method=SendRichMessage(chat_id=999, rich_message=InputRichMessage(
            blocks=[InputRichBlockParagraph(text="test")], skip_entity_detection=True,
        )), message="Not Found",
    )
    await handlers._deliver_pending_quarter(bot, cur)
    assert storage.load_stats_current(strict=True) == cur
    bot.send_message.assert_not_awaited()
    bot.send_rich_message.side_effect = None
    await handlers._deliver_pending_quarter(bot, cur)
    final = storage.load_stats_current(strict=True)
    assert final["pending_quarter_delivery"] is None
    assert final["event_time"]["periods"]["2026-Q1"]["announced_revision"] == 1
    handlers.send_backup.assert_awaited_once()
