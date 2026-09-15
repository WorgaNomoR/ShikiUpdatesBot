# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистые контракты классификации и presentation каталога пользователей."""

import json

from report_delivery import freeze_report
from report_model import render_report
from rich_report import render_rich_report
from storage import (
    KnownUser,
    UserDirectorySnapshot,
)
from user_directory import (
    ROLE_BLOCKED_ONLY,
    ROLE_CHAT,
    ROLE_LEGACY_SUBSCRIBER,
    ROLE_OWNER_SUBSCRIPTION,
    build_user_directory,
    build_user_directory_report,
)


def _known(
    user_id: int,
    name: str,
    first_seen_at: str,
    username: str | None = None,
) -> KnownUser:
    return KnownUser(user_id, name, username, first_seen_at)


def test_directory_keeps_sources_independent_and_applies_blocked_precedence():
    known_users = (
        _known(10, "First identity", "2026-09-01T10:00:00Z", "first"),
        _known(20, "Other newer", "2026-09-03T10:00:00Z"),
        _known(30, "Blocked known", "2026-09-02T10:00:00Z"),
        _known(40, "Other same time", "2026-09-03T10:00:00Z"),
        _known(999, "Impossible owner record", "2026-09-04T10:00:00Z"),
    )
    snapshot = UserDirectorySnapshot(
        known_users=known_users,
        subscribers=(
            (-100, "News channel"),
            (10, "Changed subscription label"),
            (50, "Conflicting legacy"),
            (70, "Legacy personal"),
            (999, "Owner chat"),
        ),
        blocked_user_ids=frozenset({30, 50, 60}),
    )

    directory = build_user_directory(snapshot, owner_id=999)

    assert directory.registered_user_count == 4
    assert [entry.telegram_id for entry in directory.subscribers] == [10, 70, 999, -100]
    assert directory.subscribers[0].display_name == "First identity"
    assert directory.subscribers[1].role == ROLE_LEGACY_SUBSCRIBER
    assert directory.subscribers[2].role == ROLE_OWNER_SUBSCRIPTION
    assert directory.subscribers[3].role == ROLE_CHAT
    assert [entry.telegram_id for entry in directory.blocked_users] == [30, 50, 60]
    assert directory.blocked_users[1].role == ROLE_BLOCKED_ONLY
    assert directory.blocked_users[2].display_name is None
    assert [entry.telegram_id for entry in directory.other_users] == [20, 40]
    assert sum(
        entry.telegram_id == 50
        for section in (
            directory.subscribers,
            directory.blocked_users,
            directory.other_users,
        )
        for entry in section
    ) == 1
    assert snapshot.known_users == known_users
    assert snapshot.subscribers[1] == (10, "Changed subscription label")


def test_directory_orders_timestamps_ids_undated_entries_and_chats():
    snapshot = UserDirectorySnapshot(
        known_users=(
            _known(4, "Fourth", "2026-09-02T10:00:00Z"),
            _known(2, "Second", "2026-09-02T10:00:00Z"),
            _known(3, "Third", "2026-09-03T10:00:00Z"),
        ),
        subscribers=((9, "Nine"), (2, "Two"), (-2, "Chat B"), (-10, "Chat A")),
        blocked_user_ids=frozenset({3, 7, 8}),
    )

    directory = build_user_directory(snapshot, owner_id=999)

    assert [entry.telegram_id for entry in directory.subscribers] == [2, 9, -10, -2]
    assert [entry.telegram_id for entry in directory.blocked_users] == [3, 7, 8]
    assert [entry.telegram_id for entry in directory.other_users] == [4]


def test_empty_directory_keeps_all_three_sections_visible():
    directory = build_user_directory(
        UserDirectorySnapshot((), (), frozenset()),
        owner_id=999,
    )

    report = build_user_directory_report(directory)
    html = "\n".join(chunk.html for chunk in render_report(report))

    assert directory.registered_user_count == 0
    assert html.index("Подписчики") < html.index("Заблокированные пользователи")
    assert html.index("Заблокированные пользователи") < html.index(
        "Другие зарегистрированные пользователи"
    )
    assert html.count("В этом разделе пока никого нет.") == 3


def test_empty_legacy_label_is_preserved_without_inventing_identity():
    snapshot = UserDirectorySnapshot(
        known_users=(),
        subscribers=((10, ""),),
        blocked_user_ids=frozenset(),
    )

    directory = build_user_directory(snapshot, owner_id=999)
    html = "\n".join(
        chunk.html
        for chunk in render_report(build_user_directory_report(directory))
    )

    assert directory.subscribers[0].display_name == ""
    assert "<b>Имя: </b>" not in html
    assert '<a href="tg://user?id=10">10</a>' in html


def test_report_escapes_hostile_identity_and_keeps_links_only_in_html():
    snapshot = UserDirectorySnapshot(
        known_users=(
            _known(
                10,
                '<Neo & "Trinity">',
                "2026-09-03T10:20:30Z",
                "<the&one>",
            ),
        ),
        subscribers=((10, "Ignored"),),
        blocked_user_ids=frozenset(),
    )
    report = build_user_directory_report(build_user_directory(snapshot, owner_id=999))

    html = "\n".join(chunk.html for chunk in render_report(report))
    rich = json.dumps(
        [fragment.payload for fragment in render_rich_report(report)],
        ensure_ascii=False,
    )

    assert '&lt;Neo &amp; &quot;Trinity&quot;&gt;' in html
    assert "<the&one>" not in html
    assert "@&lt;the&amp;one&gt;" in html
    assert 'href="tg://user?id=10"' in html
    assert "tg://" not in rich
    assert '<Neo & \\"Trinity\\">' in rich
    assert "@<the&one>" in rich
    assert "03.09.2026 10:20 UTC" in html


def test_large_directory_is_lossless_in_rich_fragments_and_html_chunks():
    known_users = tuple(
        _known(
            user_id,
            f"Directory-user-{user_id:04d}",
            "2026-09-03T10:20:30Z",
        )
        for user_id in range(1, 301)
    )
    snapshot = UserDirectorySnapshot(
        known_users=known_users,
        subscribers=(),
        blocked_user_ids=frozenset(),
    )
    report = build_user_directory_report(build_user_directory(snapshot, owner_id=999))

    rich_fragments = render_rich_report(report)
    html_chunks = render_report(report)
    rich = json.dumps(
        [fragment.payload for fragment in rich_fragments],
        ensure_ascii=False,
    )
    html = "\n".join(chunk.html for chunk in html_chunks)

    assert len(rich_fragments) > 1
    assert len(html_chunks) > 1
    for user_id in range(1, 301):
        marker = f"Directory-user-{user_id:04d}"
        assert rich.count(marker) == 1
        assert html.count(marker) == 1


def test_oversized_identity_downgrades_to_complete_logical_html():
    oversized_name = "X" * 40000
    snapshot = UserDirectorySnapshot(
        known_users=(
            _known(10, oversized_name, "2026-09-03T10:20:30Z"),
        ),
        subscribers=(),
        blocked_user_ids=frozenset(),
    )
    report = build_user_directory_report(build_user_directory(snapshot, owner_id=999))

    frozen = freeze_report(report)

    assert frozen
    assert all(unit["transport"] == "html" for unit in frozen)
    assert sum(unit["content"].count("X") for unit in frozen) == len(oversized_name)
