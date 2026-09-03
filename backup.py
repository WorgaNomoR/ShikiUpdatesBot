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
import tempfile
import time
import zipfile
import zlib
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
    UserAlertsStateError,
    _atomic_write,
    blocked_users_from_payload,
    known_users_from_payload,
    load_blocked_users,
    load_stats_current,
    load_subscribers,
    restorable_state_transaction,
    save_stats_current,
    subscribers_from_payload,
    user_alerts_from_payload,
)
from telegram_delivery import send_with_retry
from utils import (
    _subscriber_link,
    _utcnow,
)

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
            subscribers_from_payload(obj)
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


def _subscribers_from_import_payload(payload: str) -> dict[int, str]:
    """Разобрать уже проверенный candidate subscribers.json."""
    return subscribers_from_payload(json.loads(payload))


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
        subscribers = _subscribers_from_import_payload(pending["subscribers.json"])
        publish_subscribers = True
    else:
        subscribers = load_subscribers()
        publish_subscribers = any(user_id in subscribers for user_id in blocked)

    filtered = {
        chat_id: name
        for chat_id, name in subscribers.items()
        if chat_id not in blocked
    }
    if publish_subscribers:
        pending["subscribers.json"] = json.dumps(
            {"subscribers": {str(key): value for key, value in filtered.items()}},
            ensure_ascii=False,
            indent=2,
        )
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


async def _backup_after_subscription(
    bot: Bot, chat_id: int, name: str, subscribed: bool,
) -> None:
    """Авто-бэкап на (от)подписку: владельцу уходит свежий архив состояния,
    в подписи — кто и что сделал (имя кликабельно, ведёт в профиль) и сколько
    подписчиков осталось. «Два в одном»: индикация события + страховка списка."""
    subs = load_subscribers()
    head = (f"➕ Новый подписчик: {_subscriber_link(chat_id, name)}"
            if subscribed else
            f"➖ Отписался: {_subscriber_link(chat_id, name)}")
    caption = f"{head}\nВсего подписчиков: <b>{len(subs)}</b>\n\n{BACKUP_TAG}"
    await send_backup(bot, caption)


async def _weekly_backup_if_due(bot: Bot, cur: dict) -> dict:
    """Еженедельный авто-бэкап состояния по метке last_backup_at в stats_current.
    Первый раз (метки нет) — только проставляем время, не шлём: иначе на каждом
    рестарте эфемерного хоста улетал бы бэкап. Первый плановый уйдёт через
    WEEKLY_BACKUP_INTERVAL аптайма; под/отписки и ротация бэкапят независимо."""
    now = time.time()
    async with restorable_state_transaction():
        cur = load_stats_current()
        last = cur.get("last_backup_at")
        if last is None:
            cur["last_backup_at"] = now
            save_stats_current(cur)
            return cur
    if (now - last) < WEEKLY_BACKUP_INTERVAL:
        return cur
    caption = (f"🗓️ Еженедельный бэкап состояния.\n"
               f"Подписчиков: <b>{len(load_subscribers())}</b>\n\n{BACKUP_TAG}")
    if await send_backup(bot, caption):
        async with restorable_state_transaction():
            cur = load_stats_current()
            cur["last_backup_at"] = now
            save_stats_current(cur)
    return cur


