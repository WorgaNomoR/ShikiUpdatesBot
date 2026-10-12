# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Точное HTML-покрытие, доверенные даты и порядок presentation parts."""

import re
from copy import deepcopy
from html import unescape

import pytest

from catchup_digest import (
    html_length,
    render_digest,
    render_ordinary_entries,
    split_html,
)
from messages import (
    build_history_digest_heading,
    build_message,
    history_entry_from_event,
)
from notification_outbox import notification_entries


def _render(events):
    return render_digest(events, ordinary=lambda event: build_message(history_entry_from_event(event), normalized=event), heading=build_history_digest_heading())


def _visible(text):
    return unescape(re.sub(r"<[^>]*>", "", text))


def test_known_events_reuse_playful_messages_and_existing_title_suffix_once(journal_factory):
    events = journal_factory(count=10)["events"]
    rendered = []
    rows = {
        event["seq"]: f"📋 <b>Тайтл {event['seq']} (аниме)</b> — очередь движется, честно! &lt;😀&gt;"
        for event in events
    }

    def ordinary(event):
        rendered.append(event["seq"])
        return rows[event["seq"]]

    parts = render_digest(events, ordinary=ordinary, heading="📬 <b>Что нового у Сергея:</b>")
    assert rendered == list(range(1, 11))
    assert len(parts) == 1
    assert _visible(parts[0]["text"]).endswith("\n\n".join(_visible(rows[seq]) for seq in range(1, 11)))
    assert "Аниме ·" not in parts[0]["text"]


def test_mixed_types_media_scores_and_unknown_order(journal_factory, monkeypatch):
    monkeypatch.setattr("messages.random.choice", lambda bank: bank[0])
    events = journal_factory(count=13)["events"]
    events[1].update(media="manga", kind="manga", event_type="watching", score=None)
    events[2].update(media="manga", kind="novel", event_type="score_changed", score=9, score_change=[7, 9])
    events[5].update(event_type="unknown")
    events[0]["title"].update(name='<Title & "😀">', url="/animes/11")
    before = deepcopy(events)
    parts = _render(events)
    assert [part["kind"] for part in parts] == ["digest"]
    assert [ref for part in parts for ref in part["events"]] == [[event["seq"], event["history_id"]] for event in events]
    text = "\n".join(part["text"] for part in parts)
    assert "(аниме)" in text and "(манга)" in text and "(ранобэ)" in text
    assert "7 → 9" in text and "8/10" in text
    assert _visible(build_message(history_entry_from_event(events[1]), normalized=events[1])) in _visible(text)
    assert '&lt;Title &amp; &quot;😀&quot;&gt;' in text
    assert 'href="https://shikimori.io/animes/11"' in text
    assert "31.03.2026 23:00 UTC" in text
    assert events == before


@pytest.mark.parametrize("quality", ["missing", "naive", "invalid", "future"])
def test_period_omitted_when_any_source_time_is_untrusted(journal_factory, quality):
    events = journal_factory(count=10)["events"]
    if quality == "future":
        events[0]["event_at"] = "2099-01-01T00:00:00+00:00"
    else:
        events[0].update(time_quality=quality, event_at=None)
    assert "UTC" not in _render(events)[0]["text"]
    assert "02.04.2026" not in _render(events)[0]["text"]


def test_long_title_emoji_links_and_every_character_survive(journal_factory):
    events = journal_factory(count=10)["events"]
    title = "<&😀>" * 3000
    events[0]["title"].update(name=title, url="/animes/11")
    parts = _render(events)
    assert all(0 < html_length(part["text"]) <= 4096 for part in parts)
    first_parts = [part for part in parts if part["events"][0][0] == 1]
    assert len(first_parts) > 1
    assert all('href="https://shikimori.io/animes/11"' in part["text"] for part in first_parts)
    recovered = "".join(unescape(value) for part in parts for value in re.findall(r'<a href="https://shikimori.io/animes/11">(.*?)</a>', part["text"], re.S))
    assert recovered == title
    refs = [ref[0] for part in parts for ref in part["events"]]
    assert sorted(set(refs)) == list(range(1, 11))
    assert refs == sorted(refs)


@pytest.mark.parametrize("value", ['<a href="https://example.com?a=&amp;b=1">😀&lt;&amp;</a>', '<b>A<i>😀</i>B</b>'])
def test_split_html_retains_exact_visible_text_and_styles(value):
    chunks = split_html(value, limit=2)
    assert "".join(_visible(chunk) for chunk in chunks) == _visible(value)
    assert all(html_length(chunk) <= 2 for chunk in chunks)


@pytest.mark.parametrize("value", ["<b>open", "<script>x</script>", '<a href="javascript:x">x</a>', '<b onclick="x">x</b>', '<!--x-->hello'])
def test_invalid_html_rejects_before_plan(value):
    with pytest.raises(ValueError):
        split_html(value)


def test_empty_batch_needs_no_rendering():
    assert render_digest([], ordinary=lambda event: pytest.fail("empty batch"), heading="unused") == []


def test_oversized_heading_rejects_before_rendering(journal_factory):
    with pytest.raises(ValueError, match="digest_heading_limit"):
        render_digest(journal_factory(count=1)["events"], ordinary=lambda event: pytest.fail("heading too long"), heading="😀" * 2048)


@pytest.mark.parametrize("summary", [False, True])
def test_coalesced_completion_continuation_covers_both_ids_losslessly(coalescing_journal_factory, summary):
    events = coalescing_journal_factory(count=3 if summary else 2)["events"]
    entries = notification_entries(events)
    title = "<&😀>" * 3000
    calls = []

    def ordinary(event):
        calls.append(event["seq"])
        return '<a href="https://example.com/title">' + title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') + '</a>' if event["seq"] == 2 else "третье событие"

    parts = render_digest(events, ordinary=ordinary, heading="📬 Сводка", entries=entries) if summary else render_ordinary_entries(entries, ordinary=ordinary)
    assert calls == ([2, 3] if summary else [2])
    pair_parts = [part for part in parts if 0 in part["entries"]]
    assert len(pair_parts) > 1
    assert all(part["events"][:2] == [[1, 2], [2, 3]] for part in pair_parts)
    recovered = "".join(unescape(value) for part in pair_parts for value in re.findall(r'<a href="https://example.com/title">(.*?)</a>', part["text"], re.S))
    assert recovered == title
    assert all(0 < html_length(part["text"]) <= 4096 for part in parts)
