# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Чистая классификация и typed presentation каталога пользователей."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from report_model import (
    TELEGRAM_TEXT_LIMIT,
    Bold,
    Italic,
    Link,
    Report,
    Table,
    TableCell,
    TableGroup,
    TableRow,
    Text,
    heading,
    line,
    section,
    telegram_text_length,
    unit,
)

ROLE_REGISTERED = "registered"
ROLE_LEGACY_SUBSCRIBER = "legacy_subscriber"
ROLE_OWNER_SUBSCRIPTION = "owner_subscription"
ROLE_CHAT = "chat"
ROLE_BLOCKED_ONLY = "blocked_only"


class KnownUserSource(Protocol):
    """Минимальная форма неизменяемой записи known-users snapshot."""

    user_id: int
    display_name: str
    username: str | None
    first_seen_at: str


class UserDirectorySource(Protocol):
    """Минимальная форма согласованного снимка storage-источников."""

    known_users: tuple[KnownUserSource, ...]
    subscribers: tuple[tuple[int, str], ...]
    blocked_user_ids: frozenset[int]


@dataclass(frozen=True)
class DirectoryEntry:
    """Одна классифицированная сущность без transport-разметки."""

    telegram_id: int
    role: str
    display_name: str | None = None
    username: str | None = None
    first_seen_at: str | None = None


@dataclass(frozen=True)
class UserDirectory:
    """Взаимоисключающие разделы и честный исторический счётчик."""

    registered_user_count: int
    subscribers: tuple[DirectoryEntry, ...]
    blocked_users: tuple[DirectoryEntry, ...]
    other_users: tuple[DirectoryEntry, ...]


def _known_entry(user: KnownUserSource) -> DirectoryEntry:
    """Скопировать зарегистрированную личность в чистую directory-запись."""
    return DirectoryEntry(
        telegram_id=user.user_id,
        role=ROLE_REGISTERED,
        display_name=user.display_name,
        username=user.username,
        first_seen_at=user.first_seen_at,
    )


def _entry_sort_key(entry: DirectoryEntry) -> tuple[str, int]:
    """Стабильный ключ; основной descending timestamp применяется отдельно."""
    return entry.first_seen_at or "", entry.telegram_id


def _ordered_entries(entries: list[DirectoryEntry]) -> tuple[DirectoryEntry, ...]:
    """Датированные записи newest-first, затем недатированные по ID."""
    ordered = sorted(entries, key=lambda entry: entry.telegram_id)
    ordered.sort(key=lambda entry: _entry_sort_key(entry)[0], reverse=True)
    return tuple(ordered)


def build_user_directory(
    snapshot: UserDirectorySource,
    *,
    owner_id: int,
) -> UserDirectory:
    """Классифицировать независимые source states без I/O и мутаций."""
    known_by_id = {
        user.user_id: user
        for user in snapshot.known_users
        if user.user_id != owner_id
    }
    subscribers_by_id = dict(snapshot.subscribers)
    blocked_ids = set(snapshot.blocked_user_ids)

    personal_subscribers: list[DirectoryEntry] = []
    chat_subscribers: list[DirectoryEntry] = []
    for telegram_id, stored_label in subscribers_by_id.items():
        if telegram_id > 0:
            if telegram_id in blocked_ids:
                continue
            known = known_by_id.get(telegram_id)
            if known is not None:
                personal_subscribers.append(_known_entry(known))
            else:
                personal_subscribers.append(DirectoryEntry(
                    telegram_id=telegram_id,
                    role=(
                        ROLE_OWNER_SUBSCRIPTION
                        if telegram_id == owner_id
                        else ROLE_LEGACY_SUBSCRIBER
                    ),
                    display_name=stored_label,
                ))
            continue
        chat_subscribers.append(DirectoryEntry(
            telegram_id=telegram_id,
            role=ROLE_CHAT,
            display_name=stored_label,
        ))

    blocked_users = [
        (
            _known_entry(known_by_id[telegram_id])
            if telegram_id in known_by_id
            else DirectoryEntry(telegram_id, ROLE_BLOCKED_ONLY)
        )
        for telegram_id in blocked_ids
    ]
    personal_ids = {
        telegram_id
        for telegram_id in subscribers_by_id
        if telegram_id > 0
    }
    other_users = [
        _known_entry(user)
        for telegram_id, user in known_by_id.items()
        if telegram_id not in blocked_ids and telegram_id not in personal_ids
    ]

    return UserDirectory(
        registered_user_count=len(known_by_id),
        subscribers=(
            *_ordered_entries(personal_subscribers),
            *tuple(sorted(chat_subscribers, key=lambda entry: entry.telegram_id)),
        ),
        blocked_users=_ordered_entries(blocked_users),
        other_users=_ordered_entries(other_users),
    )


def _cell(
    *parts: str | Text | Bold | Italic,
    colspan: int = 1,
) -> TableCell:
    """Собрать безопасную ячейку без transport-specific ссылок."""
    return TableCell(
        tuple(Text(part) if isinstance(part, str) else part for part in parts),
        colspan=colspan,
    )


def _role_label(role: str) -> str:
    """Вернуть читательское описание происхождения сущности."""
    return {
        ROLE_REGISTERED: "Зарегистрированный пользователь",
        ROLE_LEGACY_SUBSCRIBER: "Подписчик без записи в реестре",
        ROLE_OWNER_SUBSCRIPTION: "Подписка владельца",
        ROLE_CHAT: "Группа или канал",
        ROLE_BLOCKED_ONLY: "Заблокированный ID без записи в реестре",
    }[role]


def _formatted_first_seen(value: str) -> str:
    """Показать каноническую UTC-метку в согласованном формате каталога."""
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return parsed.strftime("%d.%m.%Y %H:%M UTC")


def _entry_group(entry: DirectoryEntry, number: int) -> TableGroup:
    """Сохранить одну сущность одной логической Rich/HTML группой."""
    role_label = _role_label(entry.role)
    rows = [TableRow((_cell("Тип"), _cell(role_label)))]
    fallback = [line(f"{number}. ", Bold(role_label))]

    has_display_name = (
        entry.display_name is not None
        and bool(entry.display_name.strip())
    )
    if has_display_name:
        field = "Название" if entry.role == ROLE_CHAT else "Имя"
        rows.append(TableRow((_cell(field), _cell(entry.display_name))))
        linked_name = (
            entry.telegram_id > 0
            and telegram_text_length(entry.display_name) <= TELEGRAM_TEXT_LIMIT
        )
        fallback_value = (
            Link(
                entry.display_name,
                f"tg://user?id={entry.telegram_id}",
            )
            if linked_name
            else Text(entry.display_name)
        )
        fallback.append(line(Bold(f"{field}: "), fallback_value))
    if entry.username is not None:
        username = f"@{entry.username}"
        rows.append(TableRow((_cell("Username"), _cell(username))))
        fallback.append(line(Bold("Username: "), username))

    rows.append(TableRow((
        _cell("Telegram ID"),
        _cell(str(entry.telegram_id)),
    )))
    fallback_id = (
        Link(
            str(entry.telegram_id),
            f"tg://user?id={entry.telegram_id}",
        )
        if entry.telegram_id > 0 and (
            not has_display_name
            or telegram_text_length(entry.display_name) > TELEGRAM_TEXT_LIMIT
        )
        else Text(str(entry.telegram_id))
    )
    fallback.append(line(Bold("Telegram ID: "), fallback_id))

    if entry.first_seen_at is not None:
        first_seen = _formatted_first_seen(entry.first_seen_at)
        rows.append(TableRow((_cell("Первое обращение"), _cell(first_seen))))
        fallback.append(line(Bold("Первое обращение: "), first_seen))

    return TableGroup(tuple(rows), tuple(fallback))


def _directory_section(
    title: str,
    emoji: str,
    entries: tuple[DirectoryEntry, ...],
):
    """Собрать один collapsible table-backed раздел каталога."""
    items = [heading(
        f"{emoji} ",
        Bold(f"{title} · {len(entries)}"),
        level=2,
        collapsible=True,
        is_open=bool(entries),
    )]
    empty = Italic("В этом разделе пока никого нет.")
    groups = (
        tuple(
            _entry_group(entry, number)
            for number, entry in enumerate(entries, 1)
        )
        if entries
        else (
            TableGroup(
                rows=(TableRow((_cell(empty, colspan=2),)),),
                fallback=(line(empty),),
            ),
        )
    )
    items.append(Table(
        columns=2,
        groups=groups,
        header=TableRow((_cell("Поле"), _cell("Значение"))),
        separate_groups=True,
    ))
    return section(*items)


def build_user_directory_report(directory: UserDirectory) -> Report:
    """Построить единый typed Report для Rich и ordinary HTML delivery."""
    header = section(
        heading("👥 ", Bold("ПОЛЬЗОВАТЕЛИ БОТА"), level=1),
        line(
            "Зарегистрировано с момента включения учёта: ",
            Bold(str(directory.registered_user_count)),
        ),
        line(Italic(
            "Учёт охватывает только период после включения регистрации новых пользователей."
        )),
        line(
            "Управление: /block ID  ·  /unblock ID  ·  /useralerts on|off"
        ),
    )
    return Report((unit(
        header,
        _directory_section("Подписчики", "🔔", directory.subscribers),
        _directory_section(
            "Заблокированные пользователи",
            "🚫",
            directory.blocked_users,
        ),
        _directory_section(
            "Другие зарегистрированные пользователи",
            "👤",
            directory.other_users,
        ),
    ),))
