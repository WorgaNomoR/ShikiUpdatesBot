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
import json
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from config import (
    BLOCKED_USERS_FILE,
    KNOWN_USERS_FILE,
    OWNER_ID,
    SEEN_FAVS_FILE,
    SEEN_IDS_FILE,
    STATS_ALL_FILE,
    STATS_CURRENT_FILE,
    SUBS_FILE,
    UPDATE_STATE_FILE,
    USER_ALERTS_FILE,
    log,
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

def load_seen_ids() -> set[int]:
    """Загружаем уже виденные ID из JSON-файла."""
    path = Path(SEEN_IDS_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("seen_ids", []))
        except (json.JSONDecodeError, KeyError):
            log.warning("Не удалось прочитать %s, начинаем с нуля.", SEEN_IDS_FILE)
    return set()


def save_seen_ids(seen_ids: set[int]) -> None:
    """Сохраняем виденные ID в JSON-файл (атомарно)."""
    _atomic_write(
        SEEN_IDS_FILE,
        json.dumps({"seen_ids": list(seen_ids)}, ensure_ascii=False, indent=2),
    )


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


def load_subscribers() -> dict[int, str]:
    """
    Загружаем подписчиков из JSON.
    Формат хранилища: {"subscribers": {"123456": "Имя", "789012": "Имя2"}}
    Возвращаем dict[chat_id: int, name: str].
    """
    path = Path(SUBS_FILE)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return subscribers_from_payload(payload)
        except (json.JSONDecodeError, OSError, ValueError):
            log.warning("Не удалось прочитать %s, начинаем с пустого списка.", SUBS_FILE)
    return {}


def _load_subscribers_for_access_recovery() -> dict[int, str]:
    """Строго загрузить подписчиков для fail-safe сверки при запуске."""
    path = Path(SUBS_FILE)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return subscribers_from_payload(payload)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.error(
            "access-control: подписчики недоступны для сверки при запуске: %s",
            e,
        )
        raise BlockedUsersStateError(
            "подписчики недоступны для безопасной сверки"
        ) from e


def save_subscribers(subs: dict[int, str]) -> None:
    """Сохраняем подписчиков в JSON (атомарно)."""
    _atomic_write(
        SUBS_FILE,
        json.dumps({"subscribers": {str(k): v for k, v in subs.items()}}, ensure_ascii=False, indent=2),
    )


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


def _subscribers_json(subscribers: dict[int, str]) -> str:
    """Сериализовать подписчиков для общей access-control транзакции."""
    return json.dumps(
        {"subscribers": {str(k): v for k, v in subscribers.items()}},
        ensure_ascii=False,
        indent=2,
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
        subscribers = _load_subscribers_for_access_recovery()
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
            payloads[Path(SUBS_FILE)] = _subscribers_json(subscribers)
        _publish_access_state(payloads)
        return added, subscriber_removed


async def reconcile_blocked_subscribers() -> set[int]:
    """Удалить из подписчиков ID, уже сохранённые в списке блокировок.

    Восстанавливает инвариант после завершения процесса между двумя атомарными
    заменами файлов. Повторный запуск безопасен и ничего не меняет.
    """
    async with restorable_state_transaction():
        blocked = load_blocked_users()
        subscribers = _load_subscribers_for_access_recovery()
        stale_ids = blocked.intersection(subscribers)
        if not stale_ids:
            return set()
        for user_id in stale_ids:
            subscribers.pop(user_id)
        save_subscribers(subscribers)
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
    except (json.JSONDecodeError, OSError, KnownUsersStateError) as e:
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
    except (json.JSONDecodeError, OSError, UserAlertsStateError) as e:
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
    path = Path(SEEN_FAVS_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("seen_favourites", []))
        except (json.JSONDecodeError, KeyError):
            log.warning("Не удалось прочитать %s, начинаем с нуля.", SEEN_FAVS_FILE)
    return set()


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
        "last_backup_at": None,   # время последнего авто-бэкапа (для еженедельной отправки)
        "pending_quarter_delivery": None,
        "events": [],   # [{id, media, event, score, recorded_at}]
    }


def load_stats_current() -> dict:
    """
    Загружаем события текущего квартала. При ошибке/отсутствии — пустой квартал.

    Если файла ещё нет (истинно первый запуск), фиксируем tracking_since = max(
    начало квартала, сейчас). Это даёт честную дату «статистика собирается с …»,
    когда бота впервые запустили в середине квартала. Дата сразу сохраняется,
    чтобы не сбрасывалась при последующих перезапусках.
    """
    try:
        if STATS_CURRENT_FILE.exists():
            data = json.loads(STATS_CURRENT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "period" in data and "events" in data:
                # Бэкофилл для файлов, созданных до появления поля tracking_since
                if "tracking_since" not in data:
                    data["tracking_since"] = data.get("period_start") or quarter_start().isoformat()
                # Бэкофилл для файлов до появления last_backup_at (еженедельный авто-бэкап)
                data.setdefault("last_backup_at", None)
                data.setdefault("pending_quarter_delivery", None)
                return data
            log.warning("load_stats_current: неожиданная структура, сбрасываем.")
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("load_stats_current: %s", e)

    # Истинно первый запуск (или сброс) — фиксируем фактическую дату старта
    now = _utcnow()
    qs = quarter_start(now)
    tracking_since = (now if now > qs else qs).isoformat()
    fresh = _empty_stats_current(current_quarter(now), tracking_since=tracking_since)
    save_stats_current(fresh)
    log.info("load_stats_current: создан новый stats_current, отслеживание с %s.", tracking_since)
    return fresh


def save_stats_current(data: dict) -> None:
    try:
        _atomic_write(STATS_CURRENT_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
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
