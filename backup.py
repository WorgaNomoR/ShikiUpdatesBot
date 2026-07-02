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
import time
import zipfile

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile

from config import (
    DATA_DIR,
    OWNER_ID,
    WEEKLY_BACKUP_INTERVAL,
    log,
)
from storage import (
    _atomic_write,
    load_subscribers,
    save_stats_current,
)
from utils import (
    _utcnow,
    h,
)

# ═══════════════════════════════════════════════════════════════════
#  КОНСТАНТЫ И СОСТОЯНИЕ
# ═══════════════════════════════════════════════════════════════════

BACKUP_TAG = "#backup"

SHUTDOWN_BACKUP_DEBOUNCE = 60   # с: не дублировать shutdown-бэкап после свежего

SHUTDOWN_BACKUP_TIMEOUT  = 8    # с: жёсткий потолок отправки в окне graceful-shutdown

_last_backup_sent_at: float | None = None   # monotonic-метка последнего успешного бэкапа

_IMPORT_ALLOWED_FILES: frozenset[str] = frozenset({
    "subscribers.json", "stats_current.json",
})

_IMPORT_ALLOWED_DIR = "quarters"


def _subscriber_link(chat_id: int, name: str) -> str:
    """Имя, обёрнутое в ссылку на профиль (tg://user?id=...).
    Telegram открывает карточку пользователя по такой ссылке — владельцу
    удобно сразу перейти к тому, кто подписался/отписался."""
    return f'<a href="tg://user?id={chat_id}">{h(name)}</a>'


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
            zf.write(path, path.relative_to(DATA_DIR).as_posix())
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
    try:
        await bot.send_document(
            OWNER_ID,
            document=BufferedInputFile(data, filename=_backup_filename()),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
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
    Бел.список: subscribers.json, stats_current.json, quarters/<имя>.json.
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
    if name == "subscribers.json":
        subs = obj.get("subscribers") if isinstance(obj, dict) else None
        if not isinstance(subs, dict):
            return False
        try:                       # ключи — chat_id, должны приводиться к int
            for k in subs:
                int(k)
        except (TypeError, ValueError):
            return False
        return True
    if name == "stats_current.json":
        return (isinstance(obj, dict) and "period" in obj
                and isinstance(obj.get("events"), list))
    if name.startswith(_IMPORT_ALLOWED_DIR + "/"):
        return isinstance(obj, dict) and "period" in obj
    return False


def restore_backup_zip(raw: bytes) -> dict:
    """Восстанавливаем состояние из архива по белому списку.
    Каждый разрешённый член сперва валидируем как JSON и только потом пишем
    атомарно (_atomic_write) в DATA_DIR — частичное/битое состояние не льём.
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
    with zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            if not _is_allowed_import_member(name):
                skipped.append(name)
                continue
            try:
                payload = zf.read(name).decode("utf-8")
                obj = json.loads(payload)   # синтаксически валидный JSON?
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                log.warning("restore_backup_zip: пропускаю битый %s: %s", name, e)
                skipped.append(name)
                continue
            if not _valid_import_payload(name, obj):   # и похож на ожидаемую структуру?
                log.warning("restore_backup_zip: %s не похож на ожидаемый формат — пропускаю.", name)
                skipped.append(name)
                continue
            pending[name] = payload

    if not pending:
        raise ValueError("в архиве нет валидных файлов из белого списка")

    for name, payload in pending.items():
        _atomic_write(DATA_DIR / name, payload)
        restored.append(name)
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
        cur["last_backup_at"] = now
        save_stats_current(cur)
    return cur


