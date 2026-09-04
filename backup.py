# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Резервное копирование ShikiUpdatesBot.

Логика бэкапа: сборка/восстановление zip-архива состояния, доставка владельцу,
авто-триггеры (подписка, ротация, еженедельно) и shutdown-хук. Тонкие aiogram-
обёртки /backup живут в handlers и зовут отсюда. Зависит от config/storage/utils.
"""

import asyncio
import io
import json
import lzma
import math
import tempfile
import time
import weakref
import zipfile
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile

from config import (
    DATA_DIR,
    OWNER_ID,
    WEEKLY_BACKUP_INTERVAL,
    log,
)
from fact_bank import (
    FactBankDocument,
    FactBankValidationError,
    activate_restored_fact_bank,
    parse_fact_bank_bytes,
    serialize_fact_bank,
)
from storage import (
    BlockedUsersStateError,
    KnownUsersStateError,
    SubscriberState,
    SubscriptionBackupStateError,
    UserAlertsStateError,
    _atomic_write,
    blocked_users_from_payload,
    ensure_backup_schedule,
    known_users_from_payload,
    load_blocked_users,
    load_subscriber_state,
    restorable_state_transaction,
    save_subscriber_state,
    subscriber_state_from_payload,
    user_alerts_from_payload,
)
from storage import subscriber_state_json as storage_subscriber_state_json
from telegram_delivery import send_with_retry
from utils import _utcnow

# ═══════════════════════════════════════════════════════════════════
#  КОНСТАНТЫ И СОСТОЯНИЕ
# ═══════════════════════════════════════════════════════════════════

BACKUP_TAG = "#backup"

IMPORT_DOCUMENT_MAX_BYTES = 20 * 1024 * 1024
_IMPORT_ARCHIVE_MAX_MEMBERS = 256
_IMPORT_MEMBER_MAX_BYTES = 8 * 1024 * 1024
_IMPORT_TOTAL_MAX_BYTES = 32 * 1024 * 1024

SHUTDOWN_BACKUP_DEBOUNCE = 60   # с: не дублировать shutdown-бэкап после свежего

SHUTDOWN_BACKUP_TIMEOUT  = 8    # с: жёсткий потолок отправки в окне graceful-shutdown

_last_backup_sent_at: float | None = None   # monotonic-метка последнего успешного бэкапа

SUBSCRIPTION_BACKUP_INTERVAL = 24 * 60 * 60

_automatic_backup_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)

_IMPORT_ALLOWED_FILES: frozenset[str] = frozenset({
    "blocked_users.json", "facts.json", "subscribers.json", "stats_current.json",
    "update_state.json", "known_users.json", "user_alerts.json",
})

_STRICT_IMPORT_FILES: frozenset[str] = frozenset({
    "facts.json",
    "known_users.json",
    "user_alerts.json",
})

_IMPORT_ALLOWED_DIR = "quarters"


def _backup_filename() -> str:
    """Имя архива с меткой времени UTC — чтобы файлы не перезатирались в чате."""
    return f"shikibot-backup-{_utcnow().strftime('%Y%m%d-%H%M%S')}.zip"


def _automatic_backup_lock() -> asyncio.Lock:
    """Вернуть общий lock автоматической доставки для текущего event loop."""
    loop = asyncio.get_running_loop()
    lock = _automatic_backup_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _automatic_backup_locks[loop] = lock
    return lock


@asynccontextmanager
async def automatic_backup_delivery():
    """Сериализовать automatic backup без удержания restorable-state lock."""
    async with _automatic_backup_lock():
        yield


def _valid_past_timestamp(value: object, now: float) -> float | None:
    """Вернуть конечную непросроченную UTC-метку либо None."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or value > now
    ):
        return None
    return float(value)


def _prepare_schedule(state: SubscriberState, now: float) -> bool:
    """Мигрировать расписание и сбросить недостоверные будущие метки."""
    changed = ensure_backup_schedule(state, now=now)
    schedule = state.backup_schedule
    last = _valid_past_timestamp(schedule.get("last_backup_at"), now)
    if schedule.get("last_backup_at") is not None and last is None:
        schedule["last_backup_at"] = None
        changed = True
    weekly_started = _valid_past_timestamp(schedule.get("weekly_started_at"), now)
    if weekly_started is None:
        schedule["weekly_started_at"] = now
        changed = True
    return changed


def prepare_backup_schedule(state: SubscriberState, now: float) -> bool:
    """Подготовить durable-расписание для внешнего automatic backup flow."""
    return _prepare_schedule(state, now)


def _subscription_backup_due(schedule: dict, now: float) -> bool:
    """Пора ли доставлять существующий pending subscription batch."""
    if schedule.get("pending") is None:
        return False
    last = _valid_past_timestamp(schedule.get("last_backup_at"), now)
    return last is None or now - last >= SUBSCRIPTION_BACKUP_INTERVAL


def _subscription_backup_caption(pending: dict, subscriber_count: int) -> str:
    """Собрать честную агрегированную подпись подписочного бэкапа."""
    if pending["counts_known"]:
        details = (
            f"➕ Подписок: <b>{pending['subscriptions']}</b>\n"
            f"➖ Отписок: <b>{pending['unsubscriptions']}</b>"
        )
    else:
        details = (
            "⚠️ Точные количества прошлых изменений недоступны: "
            "служебное состояние было повреждено."
        )
    return (
        "👥 Накопленные изменения подписок.\n"
        f"{details}\n"
        f"Сейчас подписчиков: <b>{subscriber_count}</b>\n\n"
        f"{BACKUP_TAG}"
    )


def _remaining_pending_after_success(current: dict, delivered: dict) -> dict | None:
    """Оставить только изменения, накопленные после снимка отправки."""
    if current.get("token") != delivered.get("token"):
        raise SubscriptionBackupStateError("pending batch был заменён во время отправки")
    remaining_subscriptions = current["subscriptions"] - delivered["subscriptions"]
    remaining_unsubscriptions = current["unsubscriptions"] - delivered["unsubscriptions"]
    if remaining_subscriptions < 0 or remaining_unsubscriptions < 0:
        raise SubscriptionBackupStateError("pending batch уменьшился во время отправки")
    if remaining_subscriptions + remaining_unsubscriptions == 0:
        return None
    return {
        "subscriptions": remaining_subscriptions,
        "unsubscriptions": remaining_unsubscriptions,
        "counts_known": True,
        "token": current["token"],
    }


def _build_backup_zip() -> bytes:
    """Зипуем весь DATA_DIR в память. Исключаем *.tmp (недописанные хвосты
    _atomic_write). arcname — путь относительно DATA_DIR, чтобы структура
    (включая quarters/) восстановилась один-в-один. Возвращаем bytes —
    готовый архив для BufferedInputFile, без временных файлов на диске."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DATA_DIR.rglob("*")):
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            relative = path.relative_to(DATA_DIR)
            if any(
                part.startswith(".restore-") and part.endswith(".tmp")
                for part in relative.parts
            ):
                continue
            zf.write(path, relative.as_posix())
    return buf.getvalue()


async def send_backup(bot: Bot, caption: str) -> bool:
    """Собрать архив DATA_DIR и отправить владельцу. caption уже содержит
    #backup. Любой сбой глушим и логируем: бэкап — фоновая страховка, он не
    должен ронять вызывающий флоу (подписку, ротацию, цикл)."""
    global _last_backup_sent_at
    try:
        data = _build_backup_zip()
    except Exception as e:
        log.error("send_backup: не удалось собрать архив: %s", e)
        return False
    filename = _backup_filename()

    async def _send_document():
        return await bot.send_document(
            OWNER_ID,
            document=BufferedInputFile(data, filename=filename),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )

    try:
        await send_with_retry(_send_document)
        log.info("send_backup: архив отправлен владельцу (%d байт).", len(data))
        _last_backup_sent_at = time.monotonic()
        return True
    except Exception as e:
        log.error("send_backup: не удалось отправить владельцу: %s", e)
        return False


async def _shutdown_backup(bot: Bot) -> None:
    """Финальный бэкап при остановке. aiogram сам ловит SIGTERM/SIGINT и эмитит
    событие shutdown, к которому мы цепляемся (dp.shutdown.register). SIGTERM от
    хостинга = плановый редеплой/рестарт. Это ДОПОЛНЕНИЕ к событийным авто-бэкапам,
    а не замена: ловит «последнюю милю» перед смертью контейнера. Дебаунс — если
    бэкап уходил только что, второй не шлём. Короткий таймаут — лучше не успеть,
    чем зависнуть и быть убитым жёстко на полпути. SIGKILL/OOM/слишком короткий
    grace этим не покрыть by design — на то и событийные бэкапы (две сети внахлёст).
    Бонус: само сообщение — сигнал владельцу «бот гасится», на проде нетипично."""
    if (_last_backup_sent_at is not None
            and time.monotonic() - _last_backup_sent_at < SHUTDOWN_BACKUP_DEBOUNCE):
        log.info("_shutdown_backup: недавний бэкап свежий, на shutdown не дублирую.")
        return
    caption = (f"🔻 Бот завершает работу (SIGTERM). Финальный снапшот состояния.\n\n"
               f"{BACKUP_TAG}")
    try:
        await asyncio.wait_for(send_backup(bot, caption), timeout=SHUTDOWN_BACKUP_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("_shutdown_backup: отправка не уложилась в %d с — выходим без бэкапа.",
                    SHUTDOWN_BACKUP_TIMEOUT)


def _is_allowed_import_member(name: str) -> bool:
    """Разрешено ли имя из архива к восстановлению?
    Бел.список: blocked_users.json, facts.json, known_users.json,
    subscribers.json, stats_current.json, update_state.json, user_alerts.json
    и кварталы.
    Глушим zip-slip: '..'-сегменты, абсолютные пути и бэкслеши отвергаем."""
    if not name or name.endswith("/"):
        return False
    if name.startswith("/") or "\\" in name or ".." in name.split("/"):
        return False
    if name in _IMPORT_ALLOWED_FILES:
        return True
    parts = name.split("/")
    return (
        len(parts) == 2
        and parts[0] == _IMPORT_ALLOWED_DIR
        and parts[1].endswith(".json")
    )


def _valid_import_payload(name: str, obj) -> bool:
    """Грубая проверка структуры восстанавливаемого файла: чтобы синтаксически
    валидный, но мусорный по смыслу JSON не затёр рабочее состояние. Проверяем
    ровно ту форму, которую ждут загрузчики (load_subscribers/load_stats_current
    и чтение снапшотов), не строже — иначе отвергли бы легитимные старые файлы."""
    if name == "blocked_users.json":
        try:
            blocked_users_from_payload(obj)
        except BlockedUsersStateError:
            return False
        return True
    if name == "subscribers.json":
        try:
            subscriber_state_from_payload(obj, strict_schedule=True)
        except SubscriptionBackupStateError:
            raise
        except ValueError:
            return False
        return True
    if name == "known_users.json":
        try:
            known_users_from_payload(obj)
        except KnownUsersStateError:
            return False
        return True
    if name == "user_alerts.json":
        try:
            user_alerts_from_payload(obj)
        except UserAlertsStateError:
            return False
        return True
    if name == "stats_current.json":
        return (isinstance(obj, dict) and "period" in obj
                and isinstance(obj.get("events"), list))
    if name == "update_state.json":
        legacy_keys = {
            "last_checked_at",
            "latest_version",
            "release_url",
            "last_notified_version",
        }
        current_keys = legacy_keys | {"latest_main_version"}
        return (
            isinstance(obj, dict)
            and set(obj) in (legacy_keys, current_keys)
            and all(
                value is None or isinstance(value, str)
                for value in obj.values()
            )
        )
    if name.startswith(_IMPORT_ALLOWED_DIR + "/"):
        return isinstance(obj, dict) and "period" in obj
    return False


def _subscriber_state_from_import_payload(payload: str) -> SubscriberState:
    """Разобрать уже проверенный candidate subscribers.json."""
    return subscriber_state_from_payload(
        json.loads(payload),
        strict_schedule=True,
    )


def _prepare_access_restore_candidate(pending: dict[str, str]) -> dict[str, str]:
    """Согласовать список блокировок и подписчиков в полном кандидате восстановления.

    Любой восстановленный список подписчиков фильтруется текущим или новым
    списком блокировок. Новый список блокировок удаляет совпавших пользователей
    из текущих подписчиков в той же транзакции восстановления.
    """
    access_names = {"blocked_users.json", "subscribers.json"}
    if not access_names.intersection(pending):
        return pending

    if "blocked_users.json" in pending:
        blocked = blocked_users_from_payload(json.loads(pending["blocked_users.json"]))
    else:
        try:
            blocked = load_blocked_users()
        except BlockedUsersStateError as e:
            raise ValueError(
                "текущий список блокировок повреждён; сначала восстанови blocked_users.json"
            ) from e

    if "subscribers.json" in pending:
        subscriber_state = _subscriber_state_from_import_payload(
            pending["subscribers.json"]
        )
        if subscriber_state.schedule_missing:
            restored_at = time.time()
            ensure_backup_schedule(subscriber_state, now=restored_at)
            if "stats_current.json" in pending:
                legacy_current = json.loads(pending["stats_current.json"])
                legacy_anchor = _valid_past_timestamp(
                    legacy_current.get("last_backup_at"),
                    restored_at,
                )
                subscriber_state.backup_schedule["weekly_started_at"] = (
                    legacy_anchor if legacy_anchor is not None else restored_at
                )
        subscribers = subscriber_state.subscribers
        publish_subscribers = True
    else:
        subscriber_state = load_subscriber_state(strict_subscribers=True)
        subscribers = subscriber_state.subscribers
        publish_subscribers = any(user_id in subscribers for user_id in blocked)
        if publish_subscribers and subscriber_state.schedule_missing:
            ensure_backup_schedule(subscriber_state, now=time.time())

    filtered = {
        chat_id: name
        for chat_id, name in subscribers.items()
        if chat_id not in blocked
    }
    if publish_subscribers:
        subscriber_state.subscribers = filtered
        pending["subscribers.json"] = storage_subscriber_state_json(subscriber_state)
    return pending


def _publish_staged_file(source: Path, target: Path) -> None:
    """Атомарно опубликовать заранее записанный файл восстановления."""
    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)


def _publish_restore_files(pending: dict[str, str]) -> list[str]:
    """Применить набор файлов с откатом при ошибке текущего процесса.

    Новые данные и снимки заменяемых файлов сначала записываются рядом с
    DATA_DIR. Поэтому публикация идёт атомарными rename, а уже опубликованные
    файлы можно вернуть без новой записи и дополнительного места на диске.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".restore-",
            suffix=".tmp",
            dir=DATA_DIR,
            ignore_cleanup_errors=True,
        ) as raw_stage:
            stage = Path(raw_stage)
            new_root = stage / "new"
            old_root = stage / "old"
            existed: dict[str, bool] = {}

            for name, payload in pending.items():
                target = DATA_DIR / name
                _atomic_write(new_root / name, payload)
                existed[name] = target.is_file()
                if existed[name]:
                    _atomic_write(old_root / name, target.read_text(encoding="utf-8"))

            published: list[str] = []
            try:
                for name in pending:
                    _publish_staged_file(new_root / name, DATA_DIR / name)
                    published.append(name)
            except Exception as publish_error:
                rollback_errors: list[Exception] = []
                for name in reversed(published):
                    target = DATA_DIR / name
                    try:
                        if existed[name]:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            (old_root / name).replace(target)
                        else:
                            target.unlink(missing_ok=True)
                    except Exception as rollback_error:
                        rollback_errors.append(rollback_error)
                if rollback_errors:
                    log.critical(
                        "restore_backup_zip: публикация и откат завершились ошибкой: %s; %s",
                        publish_error,
                        rollback_errors,
                    )
                    raise ValueError(
                        "не удалось применить архив и полностью вернуть исходное состояние"
                    ) from publish_error
                raise ValueError(
                    "не удалось применить архив; исходное состояние восстановлено"
                ) from publish_error
            return published
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"не удалось подготовить восстановление: {e}") from e


def _validate_import_metadata(infos: list[zipfile.ZipInfo]) -> None:
    """Проверить ресурсные границы архива до чтения его содержимого."""
    if len(infos) > _IMPORT_ARCHIVE_MAX_MEMBERS:
        raise ValueError(
            f"в архиве больше {_IMPORT_ARCHIVE_MAX_MEMBERS} файлов и каталогов"
        )

    restorable_size = 0
    for info in infos:
        if info.is_dir() or not _is_allowed_import_member(info.filename):
            continue
        if info.file_size > _IMPORT_MEMBER_MAX_BYTES:
            raise ValueError(
                f"файл {info.filename} больше {_IMPORT_MEMBER_MAX_BYTES // (1024 * 1024)} МиБ"
            )
        restorable_size += info.file_size
        if restorable_size > _IMPORT_TOTAL_MAX_BYTES:
            raise ValueError(
                "суммарный размер восстанавливаемых файлов больше "
                f"{_IMPORT_TOTAL_MAX_BYTES // (1024 * 1024)} МиБ"
            )


async def restore_backup_zip(raw: bytes) -> dict:
    """Восстанавливаем состояние из архива по белому списку.
    Все разрешённые члены сперва валидируем как JSON, затем публикуем из
    staging-каталога под общим lock; ошибка публикации запускает откат уже
    заменённых файлов.
    Возвращаем {'restored': [...], 'skipped': [...]}.
    Бросаем ValueError, если архив битый или в нём нет ни одного валидного
    файла из белого списка."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise ValueError(f"битый zip-архив: {e}") from e

    restored: list[str] = []
    skipped: list[str] = []
    pending: dict[str, str] = {}
    invalid_access_members: set[str] = set()
    restored_fact_document: FactBankDocument | None = None
    with zf:
        infos = zf.infolist()
        _validate_import_metadata(infos)
        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            if not _is_allowed_import_member(name):
                skipped.append(name)
                continue
            if name == "facts.json":
                try:
                    fact_raw = zf.read(info)
                except (
                    zipfile.BadZipFile,
                    RuntimeError,
                    NotImplementedError,
                    OSError,
                    EOFError,
                    zlib.error,
                    lzma.LZMAError,
                ) as e:
                    raise ValueError(
                        "facts.json в архиве повреждён; восстановление отменено"
                    ) from e
                try:
                    restored_fact_document = parse_fact_bank_bytes(fact_raw)
                except FactBankValidationError as e:
                    raise ValueError(
                        "facts.json в архиве повреждён; восстановление отменено"
                    ) from e
                pending[name] = serialize_fact_bank(restored_fact_document)
                continue
            try:
                payload = zf.read(info).decode("utf-8")
                obj = json.loads(payload)   # синтаксически валидный JSON?
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                zipfile.BadZipFile,
                RuntimeError,
                NotImplementedError,
                OSError,
                EOFError,
                zlib.error,
                lzma.LZMAError,
            ) as e:
                if name in _STRICT_IMPORT_FILES:
                    raise ValueError(
                        f"{name} в архиве повреждён; восстановление отменено"
                    ) from e
                log.warning("restore_backup_zip: пропускаю битый %s: %s", name, e)
                skipped.append(name)
                if name in {"blocked_users.json", "subscribers.json"}:
                    invalid_access_members.add(name)
                continue
            if not _valid_import_payload(name, obj):   # и похож на ожидаемую структуру?
                if name in _STRICT_IMPORT_FILES:
                    raise ValueError(
                        f"{name} в архиве повреждён; восстановление отменено"
                    )
                log.warning("restore_backup_zip: %s не похож на ожидаемый формат — пропускаю.", name)
                skipped.append(name)
                if name in {"blocked_users.json", "subscribers.json"}:
                    invalid_access_members.add(name)
                continue
            pending[name] = payload

    if not pending:
        raise ValueError("в архиве нет валидных файлов из белого списка")
    if (
        "blocked_users.json" in invalid_access_members
        and "subscribers.json" in pending
    ):
        raise ValueError(
            "список блокировок в архиве повреждён; подписчики не восстановлены"
        )

    async with restorable_state_transaction():
        pending = _prepare_access_restore_candidate(pending)
        restored = _publish_restore_files(pending)
        if restored_fact_document is not None:
            activate_restored_fact_bank(restored_fact_document)
    log.info("restore_backup_zip: восстановлено %d, пропущено %d.",
             len(restored), len(skipped))
    return {"restored": restored, "skipped": skipped}


async def _backup_after_subscription(bot: Bot) -> bool:
    """Если rolling-окно открыто, доставить накопленный subscription batch."""
    async with automatic_backup_delivery():
        now = time.time()
        async with restorable_state_transaction():
            state = load_subscriber_state(strict_subscribers=True)
            if _prepare_schedule(state, now):
                save_subscriber_state(state)
            pending = state.backup_schedule.get("pending")
            if pending is None or not _subscription_backup_due(
                state.backup_schedule,
                now,
            ):
                return False
            delivered = dict(pending)
            subscriber_count = len(state.subscribers)

        if not await send_backup(
            bot,
            _subscription_backup_caption(delivered, subscriber_count),
        ):
            return False

        completed_at = time.time()
        async with restorable_state_transaction():
            state = load_subscriber_state(strict_subscribers=True)
            current = state.backup_schedule.get("pending")
            if current is None:
                log.warning(
                    "subscription backup: pending исчез во время отправки; "
                    "успех не подтверждаю в durable state."
                )
                return False
            try:
                remaining = _remaining_pending_after_success(current, delivered)
            except SubscriptionBackupStateError as e:
                log.warning(
                    "subscription backup: состояние заменено во время отправки: %s",
                    e,
                )
                return False
            state.backup_schedule["last_backup_at"] = completed_at
            state.backup_schedule["pending"] = remaining
            save_subscriber_state(state)
        return True


async def _weekly_backup_if_due(bot: Bot, cur: dict) -> dict:
    """Доставить weekly fallback только после полного семидневного интервала."""
    async with automatic_backup_delivery():
        now = time.time()
        async with restorable_state_transaction():
            state = load_subscriber_state(strict_subscribers=True)
            if _prepare_schedule(state, now):
                save_subscriber_state(state)
            schedule = state.backup_schedule
            if _subscription_backup_due(schedule, now):
                return cur
            last = _valid_past_timestamp(schedule.get("last_backup_at"), now)
            weekly_started = _valid_past_timestamp(
                schedule.get("weekly_started_at"),
                now,
            )
            anchor = last if last is not None else weekly_started
            if anchor is None or now - anchor < WEEKLY_BACKUP_INTERVAL:
                return cur
            expected_state = storage_subscriber_state_json(state)
            subscriber_count = len(state.subscribers)

        caption = (
            "🗓️ Еженедельный бэкап состояния.\n"
            f"Подписчиков: <b>{subscriber_count}</b>\n\n{BACKUP_TAG}"
        )
        if not await send_backup(bot, caption):
            return cur

        completed_at = time.time()
        async with restorable_state_transaction():
            state = load_subscriber_state(strict_subscribers=True)
            if storage_subscriber_state_json(state) != expected_state:
                log.warning(
                    "weekly backup: subscriber-state изменился во время отправки; "
                    "успех не подтверждаю в durable state."
                )
                return cur
            state.backup_schedule["last_backup_at"] = completed_at
            save_subscriber_state(state)
    return cur


