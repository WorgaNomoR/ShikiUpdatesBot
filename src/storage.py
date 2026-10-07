# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Файловое хранилище ShikiUpdatesBot.

Слой персистентности: атомарная запись и загрузка JSON-состояния
(подписчики, виденные события/избранное, статистика, текущий квартал)
под DATA_DIR. Зависит только от config (пути, логгер) и utils (даты);
о доменной логике статистики не знает — она зависит от него, не наоборот.
"""

import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from config import (
    BLOCKED_USERS_FILE,
    EVENT_JOURNAL_FILE,
    KNOWN_USERS_FILE,
    OWNER_ID,
    SEEN_FAVS_FILE,
    SEEN_IDS_FILE,
    SHIKI_USER,
    STATS_ALL_FILE,
    STATS_CURRENT_FILE,
    SUBS_FILE,
    UPDATE_STATE_FILE,
    USER_ALERTS_FILE,
    log,
)
from event_journal_schema import (
    JOURNAL_CHECKPOINT_RESERVE,
    JOURNAL_MAX_BYTES,
    EventJournalStateError,
    journal_json,
    validate_event_journal,
    validate_projection,
    validate_recovery_set,
)
from event_time_stats import (
    acknowledge_revisions,
    compact_source_history,
    validate_event_time,
)
from notification_outbox import (
    OutboxStateError,
    completed_seq,
    parse_subscriber_payload,
    progress_reserve,
    replace_recipients,
    retain_outbox,
    validate_memberships,
)
from notification_progress_schema import (
    PROGRESS_FILE_NAME,
    compact_json,
    history_budget_size,
    history_document,
    join_history_progress,
    parse_history_member,
    parse_progress_member,
    progress_budget_size,
    progress_document,
)
from report_plan import (
    FrozenReportPlanError,
    downgrade_rich_units,
    validate_frozen_report_units,
)
from source_history import (
    prefix_seq,
    retain_source_fingerprints,
)
from utils import (
    _parse_iso_utc,
    _utcnow,
    current_quarter,
    quarter_start,
)

_restorable_state_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)

_BACKUP_SCHEDULE_KEY = "backup_schedule"
_BACKUP_SCHEDULE_VERSION = 1


class SubscriptionBackupStateError(ValueError):
    """Состояние отложенного подписочного бэкапа повреждено."""


class SubscribersStateError(ValueError):
    """Существующее состояние подписчиков нельзя строго прочитать."""


@dataclass
class SubscriberState:
    """Подписчики вместе с атомарно публикуемым состоянием их бэкапа."""

    subscribers: dict[int, str]
    backup_schedule: dict
    schedule_missing: bool = False
    schedule_malformed: bool = False
    notification_memberships: dict[int, str] | None = None


@dataclass(frozen=True)
class SubscriptionMutation:
    """Результат идемпотентного изменения подписки."""

    changed: bool
    subscriber_count: int


def _restorable_state_lock() -> asyncio.Lock:
    """Вернуть общий lock импортируемого состояния для текущего event loop."""
    loop = asyncio.get_running_loop()
    lock = _restorable_state_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _restorable_state_locks[loop] = lock
    return lock


@asynccontextmanager
async def restorable_state_transaction():
    """Сериализовать импорт и публикацию изменений восстанавливаемых файлов."""
    async with _restorable_state_lock():
        yield

# ═══════════════════════════════════════════════════════════════════
#  АТОМАРНАЯ ЗАПИСЬ
# ═══════════════════════════════════════════════════════════════════

def _atomic_write(path: "Path | str", data: str) -> None:
    """Атомарная запись файла: пишем во временный файл, затем rename.
    Защищает от повреждения данных при аварийном завершении процесса.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp  = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)  # атомарная операция на уровне ОС


# ═══════════════════════════════════════════════════════════════════
#  seen_ids — ВИДЕННЫЕ СОБЫТИЯ ИСТОРИИ
# ═══════════════════════════════════════════════════════════════════

_SeenId = TypeVar("_SeenId", int, str)


def _load_seen_cache(path: "Path | str", key: str, id_type: type[_SeenId]) -> set[_SeenId]:
    """Прочитать восстанавливаемый кеш, не изменяя повреждённый файл."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("кеш виденных записей должен быть объектом")
        values = data.get(key, [])
        if not isinstance(values, list) or any(type(value) is not id_type for value in values):
            raise ValueError("кеш виденных записей содержит неверные идентификаторы")
        return set(values)
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, RecursionError):
        log.warning("Не удалось прочитать %s, начинаем с нуля.", path)
        return set()


def load_seen_ids() -> set[int]:
    """Загружаем уже виденные ID из JSON-файла."""
    return _load_seen_cache(SEEN_IDS_FILE, "seen_ids", int)


def save_seen_ids(seen_ids: set[int]) -> None:
    """Сохраняем виденные ID в JSON-файл (атомарно)."""
    _atomic_write(
        SEEN_IDS_FILE,
        json.dumps({"seen_ids": list(seen_ids)}, ensure_ascii=False, indent=2),
    )


def load_legacy_seen_ids() -> set[int] | None:
    """Отличить валидный пустой baseline от отсутствующего/повреждённого."""
    try:
        data = json.loads(SEEN_IDS_FILE.read_text(encoding="utf-8"))
        values = data.get("seen_ids", []) if isinstance(data, dict) else None
        if not isinstance(values, list) or any(type(value) is not int for value in values):
            return None
        return set(values)
    except (OSError, ValueError, RecursionError):
        return None


_journal_history_cache = None
_journal_progress_cache = None

# Один bounded batch на границе consumer; мелкую историю не переписываем.
SOURCE_COMPACTION_BYTES = 512 * 1024
SOURCE_COMPACTION_EVENTS = 128


def notification_progress_file() -> Path:
    """Оба member всегда рядом, в том числе при перенаправлении DATA_DIR в тестах."""
    return EVENT_JOURNAL_FILE.with_name(PROGRESS_FILE_NAME)


def _read_journal_member(path: Path) -> bytes:
    with path.open("rb") as handle:
        # read(limit) заранее выделял 8 МиБ даже для небольшого progress.
        size = os.fstat(handle.fileno()).st_size
        return handle.read(min(size, JOURNAL_MAX_BYTES) + 1)


def _published_history() -> tuple[dict | None, int]:
    """Кеш только immutable history: stat и restore generation проверяются всегда."""
    global _journal_history_cache, _journal_progress_cache
    try:
        stat = EVENT_JOURNAL_FILE.stat()
    except FileNotFoundError:
        _journal_history_cache = None
        _journal_progress_cache = None
        if notification_progress_file().exists():
            raise EventJournalStateError("progress_orphan")
        return None, 0
    except OSError:
        raise EventJournalStateError("journal_read") from None
    revision = (
        EVENT_JOURNAL_FILE, stat.st_dev, stat.st_ino, stat.st_size,
        stat.st_mtime_ns, stat.st_ctime_ns, restorable_restore_generation(), SHIKI_USER,
    )
    if _journal_history_cache is not None and _journal_history_cache[0] == revision:
        return _journal_history_cache[1:]
    try:
        history = parse_history_member(_read_journal_member(EVENT_JOURNAL_FILE), profile=SHIKI_USER)
    except OSError:
        raise EventJournalStateError("journal_read") from None
    if history["version"] in {4, 5, 6}:
        size = history_budget_size(history)
        _journal_history_cache = (revision, history, size)
        return history, size
    # Legacy содержит mutable progress, поэтому не кешируется.
    _journal_history_cache = None
    _journal_progress_cache = None
    return history, 0


def _progress_revision() -> tuple:
    """Проверять оба member, policy и generation даже при тёплом progress."""
    path = notification_progress_file()
    try:
        stat = path.stat()
    except FileNotFoundError:
        raise EventJournalStateError("progress_missing") from None
    except OSError:
        raise EventJournalStateError("progress_read") from None
    return (
        _journal_history_cache[0], path, stat.st_dev, stat.st_ino, stat.st_size,
        stat.st_mtime_ns, stat.st_ctime_ns, restorable_restore_generation(), JOURNAL_MAX_BYTES,
    )


def _published_progress(history: dict, history_size: int) -> tuple[dict, int]:
    global _journal_progress_cache
    revision = _progress_revision()
    if _journal_progress_cache is not None and _journal_progress_cache[0] == revision:
        return _journal_progress_cache[1:]
    try:
        progress = parse_progress_member(_read_journal_member(notification_progress_file()))
    except FileNotFoundError:
        raise EventJournalStateError("progress_missing") from None
    except OSError:
        raise EventJournalStateError("progress_read") from None
    journal = join_history_progress(history, progress)
    size = history_size + progress_budget_size(progress, compact_json(progress))
    if size + progress_reserve(journal) > JOURNAL_MAX_BYTES:
        raise EventJournalStateError("journal_capacity")
    _journal_progress_cache = (revision, journal, size)
    return journal, size


def load_notification_journal() -> dict | None:
    """Внутренний snapshot: history и progress заимствованы строго read-only."""
    history, size = _published_history()
    if history is None or history["version"] not in {4, 5, 6}:
        return history
    return _published_progress(history, size)[0]


def load_event_journal() -> dict | None:
    """Общий caller получает свою копию; опубликованный кеш ему недоступен."""
    return deepcopy(load_notification_journal())


def save_event_journal(journal: dict, *, admitting: bool = False) -> int:
    """Progress атомарен отдельно; admission оставляет прежний checkpoint валидным."""
    global _journal_progress_cache
    if not isinstance(journal, dict):
        raise EventJournalStateError("journal_structure")
    limit = JOURNAL_MAX_BYTES - JOURNAL_CHECKPOINT_RESERVE if admitting else JOURNAL_MAX_BYTES
    if type(journal.get("version")) is not int or journal.get("version") != 3:
        payload = journal_json(journal)
        size = len(payload.encode("utf-8"))
        if size > limit:
            raise EventJournalStateError("journal_capacity")
        _journal_progress_cache = None
        try:
            _atomic_write(EVENT_JOURNAL_FILE, payload)
        except Exception:
            raise EventJournalStateError("journal_write") from None
        return size
    if not {"processed_seq", "outbox"} <= journal.keys():
        raise EventJournalStateError("journal_structure")
    published, history_size = _published_history()
    activated = published is not None and published["version"] in {4, 5, 6}
    progress_id = published["progress_id"] if activated else uuid.uuid4().hex
    history = history_document(journal, progress_id)
    history_changed = not activated or history != published
    if history_changed:
        validate_event_journal(journal, profile=SHIKI_USER)
        history_size = history_budget_size(history)
    progress = progress_document(journal, progress_id)
    join_history_progress(history, progress)
    payload = compact_json(progress)
    size = history_size + progress_budget_size(progress, payload)
    if size + progress_reserve(journal) > limit:
        raise EventJournalStateError("journal_capacity")
    if len(payload.encode("utf-8")) > JOURNAL_MAX_BYTES:
        raise EventJournalStateError("journal_capacity")
    history_payload = compact_json(history) if history_changed else None
    if history_payload is not None and len(history_payload.encode("utf-8")) > JOURNAL_MAX_BYTES:
        raise EventJournalStateError("journal_capacity")
    # Общий caller владеет candidate и может менять его после save: не кешируем aliases.
    _journal_progress_cache = None
    try:
        if not activated:
            # До activation старый member остаётся единственной authority.
            _atomic_write(notification_progress_file(), payload)
            _atomic_write(EVENT_JOURNAL_FILE, history_payload)
        elif history_changed:
            current, _ = _published_progress(published, history_budget_size(published))
            initializing = (
                not current["baseline_initialized"] and journal["baseline_initialized"]
                and not current["events"] and current["processed_seq"] == 0
            )
            if (
                journal["journal_id"] != current["journal_id"]
                or journal["profile"] != current["profile"]
                or not initializing and (
                    journal["baseline_ids"] != current["baseline_ids"]
                    or journal["baseline_initialized"] != current["baseline_initialized"]
                )
                or journal.get("source_base") != current.get("source_base")
                or journal["events"][:len(current["events"])] != current["events"]
                or len(journal["events"]) < len(current["events"])
                or journal["processed_seq"] != current["processed_seq"]
                or journal["outbox"] != current["outbox"]
            ):
                raise EventJournalStateError("history_changed")
            join_history_progress(history, progress_document(current, progress_id))
            _atomic_write(EVENT_JOURNAL_FILE, history_payload)
        else:
            _atomic_write(notification_progress_file(), payload)
    except EventJournalStateError:
        raise
    except Exception:
        raise EventJournalStateError("journal_write") from None
    return size


def save_notification_recipient(journal: dict, seq: int, cid: str, recipient: dict) -> dict:
    """Под state transaction: приватная дельта поверх точной свежей authority.

    Общий save полностью проверяет каждую ревизию. Только этот узкий путь
    передаёт владение своими ветвями кешу после успешного atomic replacement.
    """
    global _journal_progress_cache
    current = load_notification_journal()
    cached = _journal_progress_cache is not None and current is _journal_progress_cache[1]
    if current is not journal and (cached or current != journal):
        raise EventJournalStateError("recipient_changed")
    candidate = replace_recipients(current, {(seq, cid): recipient})
    size = save_event_journal(candidate)
    if cached:
        _journal_progress_cache = (_progress_revision(), candidate, size)
        return candidate
    # Legacy activation: заново прочитать новые members, не заимствовать caller aliases.
    return load_notification_journal()


def compact_event_journal(
    journal: dict, *, expected_generation: int, cur: dict | None = None, force_history: bool = False,
) -> dict:
    """Под общей транзакцией: ограниченное обслуживание progress и source history."""
    candidate = retain_outbox(journal)
    if candidate != journal:
        if (
            restorable_restore_generation() != expected_generation
            or load_notification_journal() != journal
        ):
            raise EventJournalStateError("compaction_changed")
        save_event_journal(candidate)
    return (
        compact_completed_history(candidate, cur, expected_generation=expected_generation, force=force_history)
        if cur is not None else candidate
    )


def compact_completed_history(
    journal: dict, cur: dict, *, expected_generation: int, force: bool = False,
) -> dict:
    """Под state lock: общий recovery proof и одна атомарная history publication."""
    if journal.get("outbox") is None or "event_time" not in cur:
        return journal
    candidate = retain_source_fingerprints(journal)
    if force or len(compact_json(journal["events"]).encode("utf-8")) >= SOURCE_COMPACTION_BYTES:
        through = min(
            completed_seq(journal["outbox"]), journal["processed_seq"],
            cur["event_projection"]["applied_seq"], prefix_seq(journal) + SOURCE_COMPACTION_EVENTS,
        )
        if through > prefix_seq(journal):
            candidate = compact_source_history(journal, cur, through)
    if candidate == journal:
        return journal
    validate_recovery_set(journal, cur)
    validate_event_journal(candidate, profile=SHIKI_USER)
    validate_recovery_set(candidate, cur)
    # Все накладные расходы базы/индекса оплачены; бесполезное сжатие инертно.
    if len(compact_json(candidate).encode("utf-8")) >= len(compact_json(journal).encode("utf-8")):
        return journal
    if (
        restorable_restore_generation() != expected_generation
        or load_notification_journal() != journal
        or load_stats_current(strict=True) != cur
    ):
        raise EventJournalStateError("compaction_changed")
    history, _ = _published_history()
    if history is None or history["version"] not in {4, 5, 6}:
        # Сначала завершить штатную split activation; payload пока не удаляется.
        save_event_journal(journal)
        history, _ = _published_history()
    replacement = history_document(candidate, history["progress_id"])
    progress = progress_document(journal, history["progress_id"])
    join_history_progress(replacement, progress)
    payload = compact_json(replacement)
    size = history_budget_size(replacement) + progress_budget_size(progress, compact_json(progress))
    if (
        len(payload.encode("utf-8")) > JOURNAL_MAX_BYTES
        or size + progress_reserve(candidate) > JOURNAL_MAX_BYTES
    ):
        raise EventJournalStateError("journal_capacity")
    try:
        _atomic_write(EVENT_JOURNAL_FILE, payload)
    except Exception:
        raise EventJournalStateError("journal_write") from None
    return candidate


# ═══════════════════════════════════════════════════════════════════
#  subscribers — ПОДПИСЧИКИ
# ═══════════════════════════════════════════════════════════════════

def subscribers_from_payload(payload: object) -> dict[int, str]:
    """Проверить и разобрать JSON-структуру подписчиков."""
    if not isinstance(payload, dict):
        raise ValueError("состояние подписчиков должно быть объектом")
    raw_subscribers = payload.get("subscribers")
    if not isinstance(raw_subscribers, dict):
        raise ValueError("поле subscribers должно быть объектом")
    try:
        return {int(key): value for key, value in raw_subscribers.items()}
    except (TypeError, ValueError) as e:
        raise ValueError("ключ подписчика должен быть числовым ID") from e


def strict_subscribers_from_payload(payload: object) -> dict[int, str]:
    """Строго разобрать подписчиков без потери chat ID или подписи."""
    if not isinstance(payload, dict):
        raise SubscribersStateError("состояние подписчиков должно быть объектом")
    raw_subscribers = payload.get("subscribers")
    if not isinstance(raw_subscribers, dict):
        raise SubscribersStateError("поле subscribers должно быть объектом")

    subscribers: dict[int, str] = {}
    for raw_chat_id, label in raw_subscribers.items():
        if not isinstance(raw_chat_id, str) or not raw_chat_id.isascii():
            raise SubscribersStateError("ключ подписчика должен быть ASCII chat ID")
        digits = raw_chat_id[1:] if raw_chat_id.startswith("-") else raw_chat_id
        if not digits or not digits.isdecimal():
            raise SubscribersStateError("ключ подписчика должен быть числовым chat ID")
        chat_id = int(raw_chat_id)
        if (
            str(chat_id) != raw_chat_id
            or chat_id == 0
            or chat_id < -(2**63)
            or chat_id > 2**63 - 1
        ):
            raise SubscribersStateError("подписчик содержит недопустимый chat ID")
        if not isinstance(label, str):
            raise SubscribersStateError("подпись подписчика должна быть строкой")
        subscribers[chat_id] = label
    return subscribers


def _empty_backup_schedule() -> dict:
    """Каноническое пустое состояние автоматических бэкапов."""
    return {
        "version": _BACKUP_SCHEDULE_VERSION,
        "last_backup_at": None,
        "weekly_started_at": None,
        "pending": None,
    }


def _valid_stored_timestamp(value: object) -> bool:
    """Допустимо ли число как сохранённая UTC-метка без оценки будущего."""
    return (
        value is None
        or (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value >= 0
        )
    )


def _pending_subscription_backup_from_payload(payload: object) -> dict | None:
    """Проверить канонический накопленный batch подписочного бэкапа."""
    if payload is None:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "subscriptions",
        "unsubscriptions",
        "counts_known",
        "token",
    }:
        raise SubscriptionBackupStateError("неожиданная структура pending")
    subscriptions = payload.get("subscriptions")
    unsubscriptions = payload.get("unsubscriptions")
    counts_known = payload.get("counts_known")
    token = payload.get("token")
    if (
        isinstance(subscriptions, bool)
        or not isinstance(subscriptions, int)
        or subscriptions < 0
        or isinstance(unsubscriptions, bool)
        or not isinstance(unsubscriptions, int)
        or unsubscriptions < 0
        or not isinstance(counts_known, bool)
        or not isinstance(token, str)
        or not token
        or len(token) > 128
        or (counts_known and subscriptions + unsubscriptions == 0)
    ):
        raise SubscriptionBackupStateError("некорректное значение pending")
    return {
        "subscriptions": subscriptions,
        "unsubscriptions": unsubscriptions,
        "counts_known": counts_known,
        "token": token,
    }


def backup_schedule_from_payload(payload: object) -> dict:
    """Строго проверить актуальную схему автоматических бэкапов."""
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "last_backup_at",
        "weekly_started_at",
        "pending",
    }:
        raise SubscriptionBackupStateError("неожиданная структура backup_schedule")
    if payload.get("version") != _BACKUP_SCHEDULE_VERSION:
        raise SubscriptionBackupStateError("неподдерживаемая версия backup_schedule")
    if not _valid_stored_timestamp(payload.get("last_backup_at")):
        raise SubscriptionBackupStateError("некорректный last_backup_at")
    if not _valid_stored_timestamp(payload.get("weekly_started_at")):
        raise SubscriptionBackupStateError("некорректный weekly_started_at")
    return {
        "version": _BACKUP_SCHEDULE_VERSION,
        "last_backup_at": payload.get("last_backup_at"),
        "weekly_started_at": payload.get("weekly_started_at"),
        "pending": _pending_subscription_backup_from_payload(payload.get("pending")),
    }


def subscriber_state_from_payload(
    payload: object,
    *,
    strict_schedule: bool = False,
) -> SubscriberState:
    """Разобрать подписчиков и совместимое состояние их бэкапа."""
    if not isinstance(payload, dict):
        raise ValueError("состояние подписчиков должно быть объектом")
    if "notification_memberships" in payload:
        try:
            subscribers = strict_subscribers_from_payload(payload)
        except ValueError:
            raise OutboxStateError("notification_subscribers") from None
        if payload["notification_memberships"] is None:
            raise OutboxStateError("notification_memberships")
    else:
        subscribers = subscribers_from_payload(payload)
    memberships = validate_memberships(payload.get("notification_memberships"), subscribers)
    if _BACKUP_SCHEDULE_KEY not in payload:
        return SubscriberState(
            subscribers,
            _empty_backup_schedule(),
            schedule_missing=True,
            notification_memberships=memberships,
        )
    try:
        schedule = backup_schedule_from_payload(payload.get(_BACKUP_SCHEDULE_KEY))
    except SubscriptionBackupStateError:
        if strict_schedule:
            raise
        log.error("Состояние подписочного бэкапа повреждено; требуется recovery backup.")
        schedule = _empty_backup_schedule()
        schedule["pending"] = {
            "subscriptions": 0,
            "unsubscriptions": 0,
            "counts_known": False,
            "token": uuid.uuid4().hex,
        }
        return SubscriberState(
            subscribers,
            schedule,
            schedule_malformed=True,
            notification_memberships=memberships,
        )
    return SubscriberState(subscribers, schedule, notification_memberships=memberships)


def load_subscriber_state(*, strict_subscribers: bool = False) -> SubscriberState:
    """Загрузить полный subscriber-state с безопасным recovery расписания."""
    path = Path(SUBS_FILE)
    if not path.exists():
        return SubscriberState(
            {},
            _empty_backup_schedule(),
            schedule_missing=True,
        )
    try:
        payload = _read_subscriber_payload(path)
        return subscriber_state_from_payload(payload)
    except (json.JSONDecodeError, OSError, ValueError):
        if strict_subscribers:
            raise
        log.warning("Не удалось прочитать %s, начинаем с пустого списка.", SUBS_FILE)
        return SubscriberState(
            {},
            _empty_backup_schedule(),
            schedule_missing=True,
        )


def load_subscription_backup_state() -> dict:
    """Вернуть независимый снимок durable-расписания автоматических бэкапов."""
    state = load_subscriber_state()
    return json.loads(json.dumps(state.backup_schedule))


def subscriber_state_json(state: SubscriberState) -> str:
    """Сериализовать единый subscriber-state без потери backup metadata."""
    schedule = backup_schedule_from_payload(state.backup_schedule)
    subscribers = {str(cid): label for cid, label in state.subscribers.items()}
    try:
        strict_subscribers_from_payload({"subscribers": subscribers})
    except ValueError:
        raise OutboxStateError("notification_subscribers") from None
    previous = state.notification_memberships or {}
    memberships = {cid: previous.get(cid) or uuid.uuid4().hex for cid in state.subscribers}
    metadata = {"version": 1, "tokens": {str(cid): token for cid, token in memberships.items()}}
    validate_memberships(metadata, state.subscribers)
    payload = json.dumps(
        {
            "subscribers": subscribers,
            _BACKUP_SCHEDULE_KEY: schedule,
            "notification_memberships": metadata,
        },
        ensure_ascii=False,
        indent=2,
    )
    if json_publication_size(payload) > JOURNAL_MAX_BYTES:
        raise OutboxStateError("subscribers_capacity")
    return payload


def save_subscriber_state(state: SubscriberState) -> None:
    """Атомарно опубликовать подписчиков и состояние их бэкапа."""
    payload = subscriber_state_json(state)
    _atomic_write(SUBS_FILE, payload)
    state.notification_memberships = validate_memberships(json.loads(payload)["notification_memberships"], state.subscribers)
    state.schedule_missing = False
    state.schedule_malformed = False


def _legacy_weekly_anchor(now: float | None = None) -> float | None:
    """Взять старую плановую метку только как начало weekly-интервала."""
    path = Path(STATS_CURRENT_FILE)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload.get("last_backup_at") if isinstance(payload, dict) else None
    except (OSError, ValueError, RecursionError):
        return None
    if (
        not _valid_stored_timestamp(value)
        or value is None
        or (now is not None and value > now)
    ):
        return None
    return float(value)


def ensure_backup_schedule(state: SubscriberState, *, now: float | None = None) -> bool:
    """Мигрировать legacy/recovery состояние в канонический durable-вид."""
    changed = state.schedule_missing or state.schedule_malformed
    if state.schedule_missing:
        anchor = _legacy_weekly_anchor(now)
        state.backup_schedule["weekly_started_at"] = anchor if anchor is not None else now
    return changed


async def mutate_subscription(
    chat_id: int,
    name: str,
    *,
    subscribed: bool,
) -> SubscriptionMutation:
    """Атомарно изменить подписчика и накопить соответствующий backup delta."""
    async with restorable_state_transaction():
        state = load_subscriber_state(strict_subscribers=True)
        ensure_backup_schedule(state, now=time.time())
        changed = (chat_id not in state.subscribers) if subscribed else (chat_id in state.subscribers)
        if not changed:
            if state.schedule_missing or state.schedule_malformed:
                save_subscriber_state(state)
            return SubscriptionMutation(False, len(state.subscribers))

        if subscribed:
            state.subscribers[chat_id] = name
        else:
            state.subscribers.pop(chat_id)
            if state.notification_memberships is not None:
                state.notification_memberships.pop(chat_id, None)

        pending = state.backup_schedule.get("pending")
        if pending is None:
            pending = {
                "subscriptions": 0,
                "unsubscriptions": 0,
                "counts_known": True,
                "token": uuid.uuid4().hex,
            }
        else:
            pending = dict(pending)
        key = "subscriptions" if subscribed else "unsubscriptions"
        pending[key] += 1
        state.backup_schedule["pending"] = pending
        save_subscriber_state(state)
        return SubscriptionMutation(True, len(state.subscribers))


def load_subscribers() -> dict[int, str]:
    """
    Загружаем подписчиков из JSON.
    Формат хранилища: {"subscribers": {"123456": "Имя", "789012": "Имя2"}}
    Возвращаем dict[chat_id: int, name: str].
    """
    return load_subscriber_state().subscribers


def load_subscribers_strict() -> dict[int, str]:
    """Строго загрузить каталог подписчиков без recovery или миграции."""
    path = Path(SUBS_FILE)
    if not path.exists():
        return {}
    try:
        payload = _read_subscriber_payload(path)
        subscribers = strict_subscribers_from_payload(payload)
        subscriber_state_from_payload(payload, strict_schedule=True)
        return subscribers
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        OSError,
        SubscriptionBackupStateError,
        SubscribersStateError,
        ValueError,
    ) as e:
        log.error(
            "load_subscribers_strict: состояние недоступно или повреждено: %s",
            e,
        )
        raise SubscribersStateError(
            "состояние подписчиков недоступно или повреждено"
        ) from e


def _read_subscriber_payload(path: Path) -> dict:
    """Общий ограниченный reader сохраняет исходные байты при ошибках."""
    with path.open("rb") as handle:
        return parse_subscriber_payload(handle.read(JOURNAL_MAX_BYTES + 1), JOURNAL_MAX_BYTES)


def notification_memberships() -> dict[int, str]:
    """Под state lock мигрировать идентичность подписок до enqueue/dispatch."""
    load_subscribers_strict()
    state = load_subscriber_state(strict_subscribers=True)
    migrated = ensure_backup_schedule(state, now=time.time())
    if state.notification_memberships is None or migrated:
        save_subscriber_state(state)
    return dict(state.notification_memberships)


def _load_subscriber_state_for_access_recovery() -> SubscriberState:
    """Строго загрузить subscriber-state для fail-safe access-сверки."""
    try:
        return load_subscriber_state(strict_subscribers=True)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.error(
            "access-control: подписчики недоступны для сверки при запуске: %s",
            e,
        )
        raise BlockedUsersStateError(
            "подписчики недоступны для безопасной сверки"
        ) from e


def _load_subscribers_for_access_recovery() -> dict[int, str]:
    """Строго загрузить подписчиков для обратной совместимости helper API."""
    return _load_subscriber_state_for_access_recovery().subscribers


def save_subscribers(subs: dict[int, str]) -> None:
    """Сохранить подписчиков, не меняя durable backup metadata."""
    state = load_subscriber_state(strict_subscribers=True)
    state.subscribers = dict(subs)
    save_subscriber_state(state)


# ═══════════════════════════════════════════════════════════════════
#  blocked_users — ГЛОБАЛЬНЫЙ СПИСОК БЛОКИРОВОК TELEGRAM USER ID
# ═══════════════════════════════════════════════════════════════════

class BlockedUsersStateError(ValueError):
    """Существующий список блокировок нельзя безопасно прочитать или проверить."""


class BlockedUsersMutationError(RuntimeError):
    """Транзакционное изменение списка блокировок не удалось применить."""


def validate_telegram_user_id(user_id: object) -> int:
    """Проверить положительный Telegram user ID в диапазоне signed int64."""
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise ValueError("Telegram user ID должен быть целым числом")
    if user_id <= 0 or user_id > 2**63 - 1:
        raise ValueError("Telegram user ID вне допустимого диапазона")
    return user_id


def blocked_users_from_payload(payload: object) -> set[int]:
    """Проверить и разобрать каноническую JSON-структуру списка блокировок."""
    if not isinstance(payload, dict) or set(payload) != {"blocked_user_ids"}:
        raise BlockedUsersStateError("неожиданная структура списка блокировок")
    raw_ids = payload["blocked_user_ids"]
    if not isinstance(raw_ids, list):
        raise BlockedUsersStateError("blocked_user_ids должен быть списком")
    try:
        blocked = {validate_telegram_user_id(user_id) for user_id in raw_ids}
    except ValueError as e:
        raise BlockedUsersStateError(
            "список блокировок содержит некорректный user ID"
        ) from e
    if len(blocked) != len(raw_ids):
        raise BlockedUsersStateError(
            "список блокировок содержит повторяющийся user ID"
        )
    if OWNER_ID in blocked:
        raise BlockedUsersStateError(
            "OWNER_ID не может находиться в списке блокировок"
        )
    return blocked


def _blocked_users_json(blocked: set[int]) -> str:
    """Сериализовать проверенный список блокировок в стабильном порядке."""
    canonical = blocked_users_from_payload({"blocked_user_ids": list(blocked)})
    return json.dumps(
        {"blocked_user_ids": sorted(canonical)},
        ensure_ascii=False,
        indent=2,
    )


def load_blocked_users() -> set[int]:
    """Загрузить список блокировок; отсутствие файла означает пустое состояние.

    Существующий повреждённый файл не превращается в пустой список: вызывающий
    access gate обязан перейти в fail-safe режим и закрыть доступ не-владельцам.
    """
    path = Path(BLOCKED_USERS_FILE)
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return blocked_users_from_payload(payload)
    except (json.JSONDecodeError, OSError, BlockedUsersStateError) as e:
        log.error(
            "load_blocked_users: список блокировок недоступен или повреждён: %s",
            e,
        )
        raise BlockedUsersStateError(
            "список блокировок недоступен или повреждён"
        ) from e


def list_blocked_users() -> set[int]:
    """Вернуть независимый снимок заблокированных Telegram user ID."""
    return set(load_blocked_users())


def is_user_blocked(user_id: int) -> bool:
    """Проверить глобальный запрет; владелец всегда остаётся доступен."""
    user_id = validate_telegram_user_id(user_id)
    if user_id == OWNER_ID:
        return False
    return user_id in load_blocked_users()


def save_blocked_users(blocked: set[int]) -> None:
    """Атомарно сохранить валидный список блокировок без владельца."""
    _atomic_write(BLOCKED_USERS_FILE, _blocked_users_json(blocked))


def _subscribers_json(
    subscribers: dict[int, str],
    backup_schedule: dict | None = None,
    memberships: dict[int, str] | None = None,
) -> str:
    """Сериализовать подписчиков для общей access-control транзакции."""
    return subscriber_state_json(
        SubscriberState(
            dict(subscribers),
            backup_schedule or _empty_backup_schedule(),
            notification_memberships=memberships,
        )
    )


def _publish_access_state(payloads: dict[Path, str]) -> None:
    """Опубликовать несколько файлов с откатом уже заменённых состояний."""
    originals: dict[Path, str | None] = {}
    published: list[Path] = []
    try:
        for path in payloads:
            originals[path] = path.read_text(encoding="utf-8") if path.is_file() else None
        for path, payload in payloads.items():
            _atomic_write(path, payload)
            published.append(path)
    except Exception as publish_error:
        rollback_errors: list[Exception] = []
        for path in reversed(published):
            try:
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, original)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            log.critical(
                "access-control: публикация и откат завершились ошибкой: %s; %s",
                publish_error,
                rollback_errors,
            )
            raise BlockedUsersMutationError(
                "не удалось изменить список блокировок и полностью вернуть исходное состояние"
            ) from publish_error
        raise BlockedUsersMutationError(
            "не удалось изменить список блокировок; исходное состояние восстановлено"
        ) from publish_error
    finally:
        for path in payloads:
            try:
                path.with_name(path.name + ".tmp").unlink(missing_ok=True)
            except OSError:
                pass


async def add_blocked_user(user_id: int) -> tuple[bool, bool]:
    """Добавить ID и в одной транзакции удалить пользователя из подписчиков.

    Возвращает ``(добавлен_в_список, удалён_из_подписчиков)``.
    """
    user_id = validate_telegram_user_id(user_id)
    if user_id == OWNER_ID:
        raise ValueError("OWNER_ID нельзя заблокировать")
    async with restorable_state_transaction():
        blocked = load_blocked_users()
        subscriber_state = _load_subscriber_state_for_access_recovery()
        subscribers = subscriber_state.subscribers
        added = user_id not in blocked
        subscriber_removed = user_id in subscribers
        if not added and not subscriber_removed:
            return False, False
        blocked.add(user_id)
        payloads = {
            Path(BLOCKED_USERS_FILE): _blocked_users_json(blocked),
        }
        if subscriber_removed:
            subscribers.pop(user_id)
            payloads[Path(SUBS_FILE)] = _subscribers_json(
                subscribers,
                subscriber_state.backup_schedule,
                subscriber_state.notification_memberships,
            )
        _publish_access_state(payloads)
        return added, subscriber_removed


async def reconcile_blocked_subscribers() -> set[int]:
    """Удалить из подписчиков ID, уже сохранённые в списке блокировок.

    Восстанавливает инвариант после завершения процесса между двумя атомарными
    заменами файлов. Повторный запуск безопасен и ничего не меняет.
    """
    async with restorable_state_transaction():
        blocked = load_blocked_users()
        subscriber_state = _load_subscriber_state_for_access_recovery()
        subscribers = subscriber_state.subscribers
        stale_ids = blocked.intersection(subscribers)
        if not stale_ids:
            return set()
        for user_id in stale_ids:
            subscribers.pop(user_id)
        subscriber_state.subscribers = subscribers
        save_subscriber_state(subscriber_state)
        log.warning(
            "access-control: при запуске удалены подписки заблокированных ID: %s",
            sorted(stale_ids),
        )
        return stale_ids


async def remove_blocked_user(user_id: int) -> bool:
    """Удалить ID из списка блокировок, не восстанавливая прежнюю подписку."""
    user_id = validate_telegram_user_id(user_id)
    if user_id == OWNER_ID:
        raise ValueError("OWNER_ID нельзя изменять через список блокировок")
    async with restorable_state_transaction():
        blocked = load_blocked_users()
        if user_id not in blocked:
            return False
        blocked.remove(user_id)
        save_blocked_users(blocked)
        return True


# ═══════════════════════════════════════════════════════════════════
#  known_users — НЕЗАВИСИМЫЙ РЕЕСТР ПОЛЬЗОВАТЕЛЕЙ БОТА
# ═══════════════════════════════════════════════════════════════════

class KnownUsersStateError(ValueError):
    """Существующий реестр пользователей нельзя безопасно прочитать."""


class UserAlertsStateError(ValueError):
    """Настройку уведомлений о новых пользователях нельзя безопасно прочитать."""


@dataclass(frozen=True)
class KnownUser:
    """Неизменяемые первоначальные сведения о пользователе бота."""

    user_id: int
    display_name: str
    username: str | None
    first_seen_at: str


@dataclass(frozen=True)
class KnownUserRegistration:
    """Результат атомарной попытки зарегистрировать пользователя."""

    user: KnownUser
    created: bool
    should_alert: bool


class UserDirectorySnapshotError(ValueError):
    """Один из обязательных источников каталога нельзя прочитать."""

    def __init__(self, source: str):
        self.source = source
        super().__init__(f"источник каталога недоступен: {source}")


@dataclass(frozen=True)
class UserDirectorySnapshot:
    """Неизменяемый согласованный снимок трёх независимых состояний."""

    known_users: tuple[KnownUser, ...]
    subscribers: tuple[tuple[int, str], ...]
    blocked_user_ids: frozenset[int]


def _validate_known_user_text(value: object, field: str) -> str:
    """Проверить обязательное непустое строковое поле пользователя."""
    if not isinstance(value, str) or not value.strip():
        raise KnownUsersStateError(f"поле {field} должно быть непустой строкой")
    return value


def _validate_known_username(value: object) -> str | None:
    """Проверить первоначальный username, который может отсутствовать."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise KnownUsersStateError("поле username должно быть непустой строкой или null")
    return value


def _validate_first_seen_at(value: object) -> str:
    """Проверить каноническую UTC-метку с точностью до секунды."""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise KnownUsersStateError("поле first_seen_at должно быть UTC-меткой")
    parsed = _parse_iso_utc(value)
    if parsed is None or value != f"{parsed.isoformat(timespec='seconds')}Z":
        raise KnownUsersStateError("поле first_seen_at содержит некорректную UTC-метку")
    return value


def known_users_from_payload(payload: object) -> dict[int, KnownUser]:
    """Строго проверить и разобрать канонический реестр пользователей."""
    if not isinstance(payload, dict) or set(payload) != {"users"}:
        raise KnownUsersStateError("неожиданная структура реестра пользователей")
    raw_users = payload["users"]
    if not isinstance(raw_users, dict):
        raise KnownUsersStateError("поле users должно быть объектом")

    users: dict[int, KnownUser] = {}
    for raw_user_id, raw_user in raw_users.items():
        if not isinstance(raw_user_id, str) or not raw_user_id.isascii() or not raw_user_id.isdecimal():
            raise KnownUsersStateError("ключ пользователя должен быть каноническим Telegram ID")
        try:
            user_id = validate_telegram_user_id(int(raw_user_id))
        except ValueError as e:
            raise KnownUsersStateError("реестр содержит некорректный Telegram user ID") from e
        if str(user_id) != raw_user_id or user_id == OWNER_ID:
            raise KnownUsersStateError("реестр содержит недопустимый Telegram user ID")
        if not isinstance(raw_user, dict) or set(raw_user) != {
            "display_name",
            "username",
            "first_seen_at",
        }:
            raise KnownUsersStateError("неожиданная структура записи пользователя")
        users[user_id] = KnownUser(
            user_id=user_id,
            display_name=_validate_known_user_text(
                raw_user["display_name"],
                "display_name",
            ),
            username=_validate_known_username(raw_user["username"]),
            first_seen_at=_validate_first_seen_at(raw_user["first_seen_at"]),
        )
    return users


def _known_users_json(users: dict[int, KnownUser]) -> str:
    """Сериализовать проверенный реестр в стабильном порядке."""
    for user_id, user in users.items():
        if not isinstance(user, KnownUser) or user.user_id != user_id:
            raise KnownUsersStateError(
                "ключ реестра не совпадает с Telegram ID записи"
            )
    payload = {
        "users": {
            str(user_id): {
                "display_name": user.display_name,
                "username": user.username,
                "first_seen_at": user.first_seen_at,
            }
            for user_id, user in sorted(users.items())
        }
    }
    canonical = known_users_from_payload(payload)
    return json.dumps(
        {
            "users": {
                str(user_id): {
                    "display_name": user.display_name,
                    "username": user.username,
                    "first_seen_at": user.first_seen_at,
                }
                for user_id, user in canonical.items()
            }
        },
        ensure_ascii=False,
        indent=2,
    )


def load_known_users() -> dict[int, KnownUser]:
    """Загрузить реестр; отсутствие файла означает пустое состояние."""
    path = Path(KNOWN_USERS_FILE)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return known_users_from_payload(payload)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        OSError,
        KnownUsersStateError,
    ) as e:
        log.error("load_known_users: реестр недоступен или повреждён: %s", e)
        raise KnownUsersStateError("реестр пользователей недоступен или повреждён") from e


def save_known_users(users: dict[int, KnownUser]) -> None:
    """Атомарно сохранить строго проверенный реестр пользователей."""
    _atomic_write(KNOWN_USERS_FILE, _known_users_json(users))


def list_known_users() -> tuple[KnownUser, ...]:
    """Вернуть пользователей в стабильном порядке Telegram ID."""
    users = load_known_users()
    return tuple(users[user_id] for user_id in sorted(users))


def known_user_count() -> int:
    """Вернуть количество сохранённых пользователей."""
    return len(load_known_users())


def get_known_user(user_id: int) -> KnownUser | None:
    """Вернуть сохранённого пользователя по Telegram ID."""
    return load_known_users().get(validate_telegram_user_id(user_id))


async def load_user_directory_snapshot() -> UserDirectorySnapshot:
    """Скопировать три источника каталога под одним restorable-state lock."""
    async with restorable_state_transaction():
        try:
            known_users = load_known_users()
        except (KnownUsersStateError, OSError, ValueError) as e:
            raise UserDirectorySnapshotError("known_users") from e
        try:
            subscribers = load_subscribers_strict()
        except (SubscribersStateError, OSError, ValueError) as e:
            raise UserDirectorySnapshotError("subscribers") from e
        try:
            blocked_user_ids = load_blocked_users()
        except (BlockedUsersStateError, OSError, ValueError) as e:
            raise UserDirectorySnapshotError("blocked_users") from e

        return UserDirectorySnapshot(
            known_users=tuple(
                known_users[user_id]
                for user_id in sorted(known_users)
            ),
            subscribers=tuple(sorted(subscribers.items())),
            blocked_user_ids=frozenset(blocked_user_ids),
        )


def user_alerts_from_payload(payload: object) -> bool:
    """Строго проверить настройку уведомлений о новых пользователях."""
    if not isinstance(payload, dict) or set(payload) != {"enabled"}:
        raise UserAlertsStateError("неожиданная структура настройки уведомлений")
    enabled = payload["enabled"]
    if not isinstance(enabled, bool):
        raise UserAlertsStateError("поле enabled должно быть bool")
    return enabled


def load_user_alerts_enabled() -> bool:
    """Прочитать настройку; отсутствие файла означает включённые уведомления."""
    path = Path(USER_ALERTS_FILE)
    if not path.exists():
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return user_alerts_from_payload(payload)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        OSError,
        UserAlertsStateError,
    ) as e:
        log.error("load_user_alerts_enabled: настройка недоступна или повреждена: %s", e)
        raise UserAlertsStateError("настройка уведомлений недоступна или повреждена") from e


def _save_user_alerts_enabled(enabled: bool) -> None:
    """Атомарно сохранить проверенную настройку уведомлений."""
    canonical = user_alerts_from_payload({"enabled": enabled})
    _atomic_write(
        USER_ALERTS_FILE,
        json.dumps({"enabled": canonical}, ensure_ascii=False, indent=2),
    )


async def set_user_alerts_enabled(enabled: bool) -> bool:
    """Установить настройку и вернуть, изменилась ли она."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled должен быть bool")
    async with restorable_state_transaction():
        current = load_user_alerts_enabled()
        if current == enabled:
            return False
        _save_user_alerts_enabled(enabled)
        return True


async def register_known_user(
    user_id: int,
    display_name: str,
    username: str | None,
    *,
    first_seen_at: str | None = None,
) -> KnownUserRegistration:
    """Атомарно создать пользователя и решить, нужно ли отправлять alert."""
    user_id = validate_telegram_user_id(user_id)
    if user_id == OWNER_ID:
        raise ValueError("OWNER_ID не регистрируется как пользователь")
    display_name = _validate_known_user_text(display_name, "display_name")
    username = _validate_known_username(username)
    if first_seen_at is None:
        first_seen_at = f"{_utcnow().isoformat(timespec='seconds')}Z"
    first_seen_at = _validate_first_seen_at(first_seen_at)

    async with restorable_state_transaction():
        users = load_known_users()
        existing = users.get(user_id)
        if existing is not None:
            return KnownUserRegistration(existing, created=False, should_alert=False)
        try:
            alerts_enabled = load_user_alerts_enabled()
        except UserAlertsStateError:
            alerts_enabled = False
        user = KnownUser(
            user_id=user_id,
            display_name=display_name,
            username=username,
            first_seen_at=first_seen_at,
        )
        users[user_id] = user
        save_known_users(users)
        return KnownUserRegistration(
            user,
            created=True,
            should_alert=alerts_enabled,
        )


# ═══════════════════════════════════════════════════════════════════
#  seen_favourites — ВИДЕННОЕ ИЗБРАННОЕ
# ═══════════════════════════════════════════════════════════════════

def load_seen_favourites() -> set[str]:
    """
    Загружаем ID уже виденных записей избранного.
    Ключи хранятся как строки вида "anime_123" — категория + ID,
    чтобы избежать коллизий между разными категориями с одинаковыми ID.
    """
    return _load_seen_cache(SEEN_FAVS_FILE, "seen_favourites", str)


def save_seen_favourites(seen: set[str]) -> None:
    """Сохраняем виденные ID избранного в JSON (атомарно)."""
    _atomic_write(
        SEEN_FAVS_FILE,
        json.dumps({"seen_favourites": list(seen)}, ensure_ascii=False, indent=2),
    )


# ═══════════════════════════════════════════════════════════════════
#  stats_all.json — ЗАГРУЗКА / СОХРАНЕНИЕ (+ in-memory кэш)
# ═══════════════════════════════════════════════════════════════════

_stats_all_cache: dict | None = None
_stats_all_cache_ts: float = 0.0
_stats_all_cache_state: str = "missing"
_STATS_ALL_CACHE_TTL: int = 300  # секунд

STATS_ALL_VALID = "valid"
STATS_ALL_MISSING = "missing"
STATS_ALL_INVALID = "invalid"


@dataclass(frozen=True)
class StatsAllSnapshot:
    """Локальный stats_all вместе с различимым состоянием файла."""

    data: dict
    state: str


def _empty_stats_all() -> dict:
    """Пустая структура stats_all.json."""
    return {
        "updated_at": None,
        "anime": {"titles": {}, "aggregates": {}},
        "manga": {"titles": {}, "aggregates": {}},
        "favourites": {"anime": [], "manga": [], "ranobe": [],
                       "characters": [], "people": []},
    }


def load_stats_all_snapshot(use_cache: bool = True) -> StatsAllSnapshot:
    """
    Загружаем stats_all.json и сохраняем причину пустого результата.

    Обычные потребители продолжают использовать load_stats_all(), а локальные
    интерактивные сценарии могут отличить первый запуск от повреждения файла.
    """
    global _stats_all_cache, _stats_all_cache_state, _stats_all_cache_ts

    if use_cache and _stats_all_cache is not None:
        age = _utcnow().timestamp() - _stats_all_cache_ts
        if age < _STATS_ALL_CACHE_TTL:
            return StatsAllSnapshot(_stats_all_cache, _stats_all_cache_state)

    data = _empty_stats_all()
    state = STATS_ALL_MISSING
    try:
        if STATS_ALL_FILE.exists():
            raw = json.loads(STATS_ALL_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "anime" in raw and "manga" in raw:
                data = raw
                state = STATS_ALL_VALID
            else:
                state = STATS_ALL_INVALID
                log.warning("load_stats_all: неожиданная структура, сбрасываем.")
    except (json.JSONDecodeError, OSError, ValueError) as e:
        state = STATS_ALL_INVALID
        log.warning("load_stats_all: не удалось прочитать файл: %s", e)

    _stats_all_cache = data
    _stats_all_cache_state = state
    _stats_all_cache_ts = _utcnow().timestamp()
    return StatsAllSnapshot(data, state)


def load_stats_all(use_cache: bool = True) -> dict:
    """Загружаем stats_all.json, сохраняя прежний dict-контракт."""
    return load_stats_all_snapshot(use_cache=use_cache).data


def save_stats_all(data: dict) -> None:
    """Сохраняем stats_all.json атомарно + обновляем кэш."""
    global _stats_all_cache, _stats_all_cache_state, _stats_all_cache_ts
    try:
        data["updated_at"] = _utcnow().isoformat()
        _atomic_write(STATS_ALL_FILE, json.dumps(data, ensure_ascii=False, indent=2))
        _stats_all_cache = data
        _stats_all_cache_state = STATS_ALL_VALID
        _stats_all_cache_ts = _utcnow().timestamp()
    except Exception as e:
        log.error("save_stats_all: не удалось записать файл: %s", e)


# ═══════════════════════════════════════════════════════════════════
#  stats_current.json — ТЕКУЩИЙ КВАРТАЛ
# ═══════════════════════════════════════════════════════════════════


class QuarterDeliveryStateError(ValueError):
    """Квартальная доставка не может безопасно прочитать или сохранить состояние."""


_restorable_restore_generation = 0


def restorable_restore_generation() -> int:
    """Поколение восстановления: любой успешный импорт отменяет старую попытку."""
    return _restorable_restore_generation


def mark_restorable_state_restored() -> None:
    """Отметить успешную публикацию любого restorable-кандидата под общим lock."""
    global _restorable_restore_generation
    _restorable_restore_generation += 1


def _quarter_plan_hash(pending: dict) -> str:
    """Связать неизменяемые поля плана, исключив только acknowledgement."""
    identity = {key: value for key, value in pending.items()
                if key not in {"next_unit", "plan_hash", "delivery_uncertain"}}
    raw = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def new_quarter_delivery(old_period: str, new_period: str, messages: list[str]) -> dict:
    """Заморозить legacy-compatible version-1 HTML plan."""
    pending = {
        "version": 1,
        "plan_id": uuid.uuid4().hex,
        "old_period": old_period,
        "new_period": new_period,
        "report_messages": list(messages),
        "next_unit": 0,
    }
    pending["plan_hash"] = _quarter_plan_hash(pending)
    validate_pending_quarter_delivery({"period": new_period, "pending_quarter_delivery": pending})
    return pending


def new_quarter_delivery_plan(
    old_period: str,
    new_period: str,
    units: list[dict],
    *,
    event_time_revisions: dict[str, int] | None = None,
) -> dict:
    """Заморозить v2 transport/content или v3 с ревизиями корректировок."""
    try:
        validate_frozen_report_units(units)
    except FrozenReportPlanError:
        raise QuarterDeliveryStateError("report_units") from None
    pending = {
        "version": 2 if event_time_revisions is None else 3,
        "plan_id": uuid.uuid4().hex,
        "old_period": old_period,
        "new_period": new_period,
        "report_units": json.loads(json.dumps(units, ensure_ascii=False)),
        "next_unit": 0,
    }
    if event_time_revisions is not None:
        pending["event_time_revisions"] = dict(event_time_revisions)
    pending["plan_hash"] = _quarter_plan_hash(pending)
    validate_pending_quarter_delivery({
        "period": new_period,
        "pending_quarter_delivery": pending,
    })
    return pending


def downgrade_quarter_delivery(pending: dict, start_unit: int) -> dict:
    """После точного unsupported-ответа заморозить remaining HTML plan."""
    if pending.get("delivery_uncertain"):
        raise QuarterDeliveryStateError("uncertain_downgrade")
    if pending.get("version") not in {2, 3}:
        raise QuarterDeliveryStateError("unsupported_downgrade")
    try:
        units = downgrade_rich_units(pending["report_units"], start_unit)
        old_period = pending["old_period"]
        new_period = pending["new_period"]
    except (KeyError, FrozenReportPlanError):
        raise QuarterDeliveryStateError("unsupported_downgrade") from None
    downgraded = {
        "version": pending["version"],
        "plan_id": uuid.uuid4().hex,
        "old_period": old_period,
        "new_period": new_period,
        "report_units": units,
        "next_unit": start_unit,
    }
    if pending["version"] == 3:
        downgraded["event_time_revisions"] = dict(pending["event_time_revisions"])
    downgraded["plan_hash"] = _quarter_plan_hash(downgraded)
    validate_pending_quarter_delivery({
        "period": downgraded["new_period"],
        "pending_quarter_delivery": downgraded,
    })
    return downgraded


def validate_quarter_period(period: object) -> None:
    """Проверить календарный период до чтения снапшотов или публикации состояния."""
    if (
        not isinstance(period, str)
        or re.fullmatch(r"[0-9]{4}-Q[1-4]", period) is None
        or period.startswith("0000")
    ):
        raise QuarterDeliveryStateError("period_format")


def validate_pending_quarter_delivery(cur: dict) -> dict | None:
    """Единый строгий контракт legacy/current pending для runtime и импорта.

    Ошибки содержат только фиксированную причину, никогда значения отчёта.
    Legacy здесь не мигрируется: новая идентичность должна сначала сохраниться.
    """
    pending = cur.get("pending_quarter_delivery")
    if pending is None:
        return None
    if not isinstance(pending, dict):
        raise QuarterDeliveryStateError("pending_type")
    legacy_keys = {"old_period", "new_period", "report_messages", "report_sent"}
    version1_keys = (
        (legacy_keys - {"report_sent"})
        | {"version", "plan_id", "plan_hash", "next_unit"}
    )
    version2_keys = {
        "version",
        "plan_id",
        "plan_hash",
        "old_period",
        "new_period",
        "report_units",
        "next_unit",
    }
    legacy = "version" not in pending
    if not legacy and (
        type(pending.get("version")) is not int
        or pending["version"] not in {1, 2, 3}
    ):
        raise QuarterDeliveryStateError("unsupported_version")
    expected_keys = (
        legacy_keys
        if legacy
        else version1_keys if pending["version"] == 1 else version2_keys | ({"event_time_revisions"} if pending["version"] == 3 else set())
    )
    if not legacy and "delivery_uncertain" in pending:
        expected_keys = expected_keys | {"delivery_uncertain"}
        if type(pending["delivery_uncertain"]) is not bool:
            raise QuarterDeliveryStateError("delivery_uncertainty")
    if set(pending) != expected_keys:
        raise QuarterDeliveryStateError("pending_fields")
    old, new = pending["old_period"], pending["new_period"]
    validate_quarter_period(old)
    validate_quarter_period(new)
    if old >= new or new != cur.get("period"):
        raise QuarterDeliveryStateError("period_lineage")
    if pending.get("version") == 3:
        revisions = pending["event_time_revisions"]
        if not isinstance(revisions, dict) or old not in revisions:
            raise QuarterDeliveryStateError("correction_revisions")
        for period, revision in revisions.items():
            validate_quarter_period(period)
            if period > old or type(revision) is not int or revision < 0:
                raise QuarterDeliveryStateError("correction_revisions")
    messages = pending.get("report_messages")
    units = pending.get("report_units")
    if legacy or pending.get("version") == 1:
        if not isinstance(messages, list) or any(
            not isinstance(message, str) or not message.strip()
            for message in messages
        ):
            raise QuarterDeliveryStateError("report_messages")
        try:
            for message in messages:
                message.encode("utf-8")
        except UnicodeError:
            raise QuarterDeliveryStateError("report_encoding") from None
        total_units = len(messages)
    else:
        try:
            validate_frozen_report_units(units)
        except FrozenReportPlanError:
            raise QuarterDeliveryStateError("report_units") from None
        total_units = len(units)
    if legacy:
        if type(pending["report_sent"]) is not bool:
            raise QuarterDeliveryStateError("legacy_completion")
        if messages and not pending["report_sent"] and cur.get("last_report_sent") == new:
            raise QuarterDeliveryStateError("premature_completion")
    else:
        if (
            type(pending["next_unit"]) is not int
            or not 0 <= pending["next_unit"] <= total_units
        ):
            raise QuarterDeliveryStateError("progress_index")
        if cur.get("last_report_sent") == new and pending["next_unit"] < total_units:
            raise QuarterDeliveryStateError("premature_completion")
        if pending.get("delivery_uncertain") and pending["next_unit"] == total_units:
            raise QuarterDeliveryStateError("completed_uncertainty")
        if not isinstance(pending["plan_id"], str) or re.fullmatch(r"[0-9a-f]{32}", pending["plan_id"]) is None:
            raise QuarterDeliveryStateError("plan_identity")
        if pending["plan_hash"] != _quarter_plan_hash(pending):
            raise QuarterDeliveryStateError("plan_integrity")
    return pending


def migrate_quarter_delivery(pending: dict) -> dict:
    """Перенести проверенный legacy-план без рендеринга и выдуманного прогресса."""
    if "version" in pending:
        return pending
    migrated = new_quarter_delivery(
        pending["old_period"], pending["new_period"], pending["report_messages"],
    )
    if pending["report_sent"]:
        migrated["next_unit"] = len(migrated["report_messages"])
    return migrated


def _empty_stats_current(period: str, tracking_since: str | None = None) -> dict:
    """
    Пустая структура текущего квартала.
    period_start — календарное начало квартала (для метки периода).
    tracking_since — реальная дата, с которой бот начал собирать события.
      При ротации = начало квартала (полные данные).
      При первом запуске в середине квартала = дата запуска (данные неполные).
      Если None — берётся календарное начало квартала.
    """
    qs = quarter_start().isoformat()
    return {
        "period": period,
        "period_start": qs,
        "tracking_since": tracking_since or qs,
        "last_report_sent": None,
        "pending_quarter_delivery": None,
        "events": [],   # [{id, media, event, score, recorded_at}]
    }


def _backfill_stats_current(data: dict) -> None:
    """Добавить только прежние defaults чтения, не мигрируя frozen-план."""
    if "tracking_since" not in data:
        data["tracking_since"] = data.get("period_start") or quarter_start().isoformat()
    data.setdefault("pending_quarter_delivery", None)


def load_stats_current(*, strict: bool = False, initialize_missing: bool = False) -> dict:
    """
    Загружаем события текущего квартала. При ошибке/отсутствии — пустой квартал.

    strict=True сохраняет повреждённое или недоступное состояние и поднимает
    безопасную ошибку. initialize_missing разрешает создать только отсутствующий
    файл; вызывающий код должен удерживать restorable-state lock.

    Если файла ещё нет (истинно первый запуск), фиксируем tracking_since = max(
    начало квартала, сейчас). Это даёт честную дату «статистика собирается с …»,
    когда бота впервые запустили в середине квартала. Дата сразу сохраняется,
    чтобы не сбрасывалась при последующих перезапусках.
    """
    try:
        with STATS_CURRENT_FILE.open("rb") as stream:
            raw = stream.read(JOURNAL_MAX_BYTES + 1 if strict else -1)
        if strict and len(raw) > JOURNAL_MAX_BYTES:
            raise QuarterDeliveryStateError("current_size")
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict) and "period" in data and "events" in data:
            if strict and (not isinstance(data["period"], str) or not isinstance(data["events"], list)):
                raise QuarterDeliveryStateError("current_structure")
            if strict:
                validate_quarter_period(data["period"])
                validate_pending_quarter_delivery(data)
                if "event_projection" in data:
                    validate_projection(data["event_projection"])
                validate_event_time(data)
            # Бэкофилл для файлов, созданных до появления поля tracking_since
            _backfill_stats_current(data)
            if strict:
                stats_current_json(data)
            return data
        if strict:
            raise QuarterDeliveryStateError("current_structure")
        log.warning("load_stats_current: неожиданная структура, сбрасываем.")
    except FileNotFoundError:
        if strict and (not initialize_missing or EVENT_JOURNAL_FILE.exists()):
            raise QuarterDeliveryStateError("current_missing") from None
    except QuarterDeliveryStateError:
        raise
    except (OSError, ValueError, RecursionError) as e:
        if strict:
            raise QuarterDeliveryStateError("current_read") from None
        log.warning("load_stats_current: %s", e)

    # Истинно первый запуск (или сброс) — фиксируем фактическую дату старта
    now = _utcnow()
    qs = quarter_start(now)
    tracking_since = (now if now > qs else qs).isoformat()
    fresh = _empty_stats_current(current_quarter(now), tracking_since=tracking_since)
    save_stats_current(fresh, strict=strict)
    log.info("load_stats_current: создан новый stats_current, отслеживание с %s.", tracking_since)
    return fresh


def json_publication_size(payload: str) -> int:
    """Path.write_text переводит JSON-переносы в системный EOL на Windows."""
    return len(payload.replace("\n", os.linesep).encode("utf-8"))


def stats_current_json(data: dict, *, strict: bool = True) -> str:
    """Компактная публикация с резервом всех штатных frozen-переходов."""
    def encode(value: dict) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=not strict)

    payload = encode(data)
    if strict:
        sizes = [json_publication_size(payload)]
        completed = deepcopy(data)
        # Импорт и следующая запись учитывают те же defaults, что строгий reader.
        _backfill_stats_current(completed)
        sizes.append(json_publication_size(encode(completed)))
        pending = completed.get("pending_quarter_delivery")
        if isinstance(pending, dict):
            pending = migrate_quarter_delivery(pending)
            completed["pending_quarter_delivery"] = pending
            sizes.append(json_publication_size(encode(completed)))
            key = "report_messages" if pending["version"] == 1 else "report_units"
            if pending["next_unit"] < len(pending[key]):
                # Наибольший ещё не подтверждённый индекс; false на байт длиннее true.
                pending["next_unit"] = len(pending[key]) - 1
                pending["delivery_uncertain"] = False
                sizes.append(json_publication_size(encode(completed)))
            pending["next_unit"] = len(pending[key])
            pending.pop("delivery_uncertain", None)
            completed["last_report_sent"] = pending["new_period"]
            acknowledge_revisions(completed)
            sizes.append(json_publication_size(encode(completed)))
        if max(sizes) > JOURNAL_MAX_BYTES:
            raise QuarterDeliveryStateError("current_capacity")
    return payload


def save_stats_current(data: dict, *, strict: bool = False) -> None:
    """Атомарно записать состояние; strict не скрывает ошибку acknowledgement."""
    try:
        if strict and "event_projection" in data:
            validate_projection(data["event_projection"])
        if strict:
            if "event_time" in data:
                validate_quarter_period(data.get("period"))
                validate_pending_quarter_delivery(data)
            validate_event_time(data)
        payload = stats_current_json(data, strict=strict)
        _atomic_write(STATS_CURRENT_FILE, payload)
    except Exception as e:
        if strict:
            log.error("save_stats_current: strict-запись не удалась: %s", type(e).__name__)
            raise QuarterDeliveryStateError("current_write") from None
        log.error("save_stats_current: %s", e)


# ═══════════════════════════════════════════════════════════════════
#  update_state.json — КЕШ ВЕРСИЙ MAIN И WINDOWS-РЕЛИЗА
# ═══════════════════════════════════════════════════════════════════

def _empty_update_state() -> dict:
    return {
        "last_checked_at": None,
        "latest_main_version": None,
        "latest_version": None,
        "release_url": None,
        "last_notified_version": None,
    }


def load_update_state() -> dict:
    """Загрузить состояние обновлений; повреждённые данные безопасно сбросить."""
    state = _empty_update_state()
    try:
        if UPDATE_STATE_FILE.exists():
            raw = json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("expected an object")
            for key in state:
                value = raw.get(key)
                if value is None or isinstance(value, str):
                    state[key] = value
            return state
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("load_update_state: %s", e)
    return state


def save_update_state(data: dict) -> None:
    """Атомарно сохранить только стабильную схему проверки обновлений."""
    state = _empty_update_state()
    for key in state:
        value = data.get(key)
        if value is None or isinstance(value, str):
            state[key] = value
    try:
        _atomic_write(UPDATE_STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as e:
        log.error("save_update_state: %s", e)
