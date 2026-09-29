# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Персистентность дополнительного банка фактов и runtime-снимок."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from config import (
    FACTS_FILE,
    log,
)
from storage import (
    _atomic_write,
    restorable_state_transaction,
)

FACT_BANK_SCHEMA_VERSION = 1
FACT_BANK_MAX_BYTES = 512 * 1024
FACT_BANK_MAX_FACTS = 500
FACT_ID_MAX_LENGTH = 32
FACT_TEXT_MAX_LENGTH = 1000
FACT_BANK_VERSION_MAX_LENGTH = 64

FACT_FILE_VALID = "valid"
FACT_FILE_MISSING = "missing"
FACT_FILE_INVALID = "invalid"

_FACT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
_BANK_VERSION_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")


class FactBankValidationError(ValueError):
    """Кандидат дополнительного банка не соответствует контракту."""


class StaleFactBankError(ValueError):
    """Банк изменился после показа подтверждения владельцу."""


@dataclass(frozen=True, slots=True)
class InlineFact:
    """Один факт с устойчивым идентификатором и признаком выбора владельца."""

    id: str
    text: str
    owner_pick: bool = False


@dataclass(frozen=True, slots=True)
class FactBankDocument:
    """Полностью проверенный документ только с дополнительными фактами."""

    bank_version: str | None
    facts: tuple[InlineFact, ...]


@dataclass(frozen=True, slots=True)
class FactBankSnapshot:
    """Неделимый снимок объединённого банка для всех публичных поверхностей."""

    base_facts: tuple[InlineFact, ...]
    additional_facts: tuple[InlineFact, ...]
    facts: tuple[InlineFact, ...]
    bank_version: str | None
    file_state: str
    revision: str


_base_facts: tuple[InlineFact, ...] = ()
_snapshot: FactBankSnapshot | None = None


def _revision(payload: bytes) -> str:
    """Получить короткую непрозрачную привязку callback к содержимому файла."""
    return hashlib.blake2s(payload, digest_size=8).hexdigest()


def _validate_base_facts(facts: tuple[InlineFact, ...]) -> None:
    """Защитить неизменяемую базу от случайных дубликатов при разработке."""
    if not facts:
        raise RuntimeError("Хардкодный банк фактов не может быть пустым")
    ids = [fact.id for fact in facts]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Хардкодный банк содержит повторяющиеся ID")


def configure_fact_bank(base_facts: tuple[InlineFact, ...]) -> FactBankSnapshot:
    """Один раз привязать персистентность к хардкодной базе и загрузить файл."""
    global _base_facts
    normalized = tuple(base_facts)
    _validate_base_facts(normalized)
    if _base_facts and _base_facts != normalized:
        raise RuntimeError("Хардкодная база фактов уже настроена иначе")
    _base_facts = normalized
    return reload_fact_bank()


def _require_configured() -> tuple[InlineFact, ...]:
    if not _base_facts:
        raise RuntimeError("Хардкодная база фактов ещё не настроена")
    return _base_facts


def _snapshot_for(
    document: FactBankDocument,
    *,
    file_state: str,
    revision_payload: bytes,
) -> FactBankSnapshot:
    base = _require_configured()
    additional = tuple(document.facts)
    return FactBankSnapshot(
        base_facts=base,
        additional_facts=additional,
        facts=base + additional,
        bank_version=document.bank_version,
        file_state=file_state,
        revision=_revision(revision_payload),
    )


def empty_fact_bank_document() -> FactBankDocument:
    """Вернуть канонический пустой дополнительный банк."""
    return FactBankDocument(bank_version=None, facts=())


def _validate_fact_id(value: object, *, index: int) -> str:
    if not isinstance(value, str):
        raise FactBankValidationError(f"facts[{index}].id должен быть строкой")
    if len(value) > FACT_ID_MAX_LENGTH:
        raise FactBankValidationError(
            f"facts[{index}].id длиннее {FACT_ID_MAX_LENGTH} символов"
        )
    if not _FACT_ID_RE.fullmatch(value):
        raise FactBankValidationError(
            f"facts[{index}].id должен состоять из a-z, 0-9 и внутренних дефисов"
        )
    return value


def _validate_fact_text(value: object, *, index: int) -> str:
    if not isinstance(value, str):
        raise FactBankValidationError(f"facts[{index}].text должен быть строкой")
    if not value or value != value.strip():
        raise FactBankValidationError(
            f"facts[{index}].text не должен быть пустым или окружён пробелами"
        )
    if len(value) > FACT_TEXT_MAX_LENGTH:
        raise FactBankValidationError(
            f"facts[{index}].text длиннее {FACT_TEXT_MAX_LENGTH} символов"
        )
    if any(
        (ord(char) < 32 or 0x7F <= ord(char) <= 0x9F)
        and char not in "\n\t"
        for char in value
    ):
        raise FactBankValidationError(
            f"facts[{index}].text содержит запрещённые управляющие символы"
        )
    return value


def fact_bank_document_from_payload(payload: object) -> FactBankDocument:
    """Строго разобрать уже декодированный JSON без публикации состояния."""
    if not isinstance(payload, dict):
        raise FactBankValidationError("корнем facts.json должен быть объект")
    expected_keys = {"schema_version", "bank_version", "facts"}
    if set(payload) != expected_keys:
        raise FactBankValidationError(
            "facts.json должен содержать только schema_version, bank_version и facts"
        )
    schema_version = payload["schema_version"]
    if type(schema_version) is not int or schema_version != FACT_BANK_SCHEMA_VERSION:
        raise FactBankValidationError("версия схемы facts.json не поддерживается")

    raw_facts = payload["facts"]
    if not isinstance(raw_facts, list):
        raise FactBankValidationError("facts должен быть массивом")
    if len(raw_facts) > FACT_BANK_MAX_FACTS:
        raise FactBankValidationError(
            f"facts содержит больше {FACT_BANK_MAX_FACTS} записей"
        )

    bank_version = payload["bank_version"]
    if bank_version is not None:
        if not isinstance(bank_version, str):
            raise FactBankValidationError("bank_version должен быть строкой или null")
        if len(bank_version) > FACT_BANK_VERSION_MAX_LENGTH:
            raise FactBankValidationError(
                f"bank_version длиннее {FACT_BANK_VERSION_MAX_LENGTH} символов"
            )
        if not _BANK_VERSION_RE.fullmatch(bank_version):
            raise FactBankValidationError(
                "bank_version должен состоять из букв, цифр, точек, дефисов и подчёркиваний"
            )
    elif raw_facts:
        raise FactBankValidationError("непустому банку нужен bank_version")

    base_ids = {fact.id for fact in _require_configured()}
    seen_ids: set[str] = set()
    facts: list[InlineFact] = []
    for index, item in enumerate(raw_facts):
        if not isinstance(item, dict) or set(item) != {"id", "text"}:
            raise FactBankValidationError(
                f"facts[{index}] должен содержать только id и text"
            )
        fact_id = _validate_fact_id(item["id"], index=index)
        if fact_id in base_ids:
            raise FactBankValidationError(
                f"facts[{index}].id пересекается с хардкодной базой"
            )
        if fact_id in seen_ids:
            raise FactBankValidationError(f"facts[{index}].id повторяется")
        seen_ids.add(fact_id)
        facts.append(
            InlineFact(
                id=fact_id,
                text=_validate_fact_text(item["text"], index=index),
            )
        )
    return FactBankDocument(bank_version=bank_version, facts=tuple(facts))


def parse_fact_bank_bytes(raw: bytes) -> FactBankDocument:
    """Проверить точный размер, UTF-8, JSON-синтаксис и доменную схему."""
    if not isinstance(raw, bytes):
        raise FactBankValidationError("facts.json должен быть передан как bytes")
    if not raw:
        raise FactBankValidationError("facts.json пуст")
    if len(raw) > FACT_BANK_MAX_BYTES:
        raise FactBankValidationError(
            f"facts.json больше {FACT_BANK_MAX_BYTES} байт"
        )
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except UnicodeDecodeError as e:
        raise FactBankValidationError("facts.json должен быть в UTF-8") from e
    except json.JSONDecodeError as e:
        raise FactBankValidationError("facts.json содержит некорректный JSON") from e
    return fact_bank_document_from_payload(payload)


def serialize_fact_bank(document: FactBankDocument) -> str:
    """Сериализовать проверенный документ в единственной канонической форме."""
    if not isinstance(document, FactBankDocument):
        raise FactBankValidationError("ожидался проверенный документ банка фактов")
    if any(fact.owner_pick for fact in document.facts):
        raise FactBankValidationError(
            "дополнительный банк не может назначать отметку выбора владельца"
        )
    payload = {
        "schema_version": FACT_BANK_SCHEMA_VERSION,
        "bank_version": document.bank_version,
        "facts": [
            {"id": fact.id, "text": fact.text}
            for fact in document.facts
        ],
    }
    fact_bank_document_from_payload(payload)
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _base_only_snapshot(*, file_state: str, revision_payload: bytes) -> FactBankSnapshot:
    return _snapshot_for(
        empty_fact_bank_document(),
        file_state=file_state,
        revision_payload=revision_payload,
    )


def reload_fact_bank(path: Path | None = None) -> FactBankSnapshot:
    """Перечитать внешний файл; при любой ошибке оставить доступной базу."""
    global _snapshot
    target = Path(path or FACTS_FILE)
    try:
        size = target.stat().st_size
        if size > FACT_BANK_MAX_BYTES:
            log.warning(
                "facts: дополнительный банк больше %d байт (%d)",
                FACT_BANK_MAX_BYTES,
                size,
            )
            _snapshot = _base_only_snapshot(
                file_state=FACT_FILE_INVALID,
                revision_payload=f"oversize:{size}".encode("ascii"),
            )
            return _snapshot
        with target.open("rb") as stream:
            raw = stream.read(FACT_BANK_MAX_BYTES + 1)
    except FileNotFoundError:
        _snapshot = _base_only_snapshot(
            file_state=FACT_FILE_MISSING,
            revision_payload=b"missing",
        )
        return _snapshot
    except OSError as e:
        log.warning("facts: не удалось прочитать дополнительный банк: %s", e)
        _snapshot = _base_only_snapshot(
            file_state=FACT_FILE_INVALID,
            revision_payload=f"unreadable:{type(e).__name__}".encode("ascii"),
        )
        return _snapshot

    if len(raw) > FACT_BANK_MAX_BYTES:
        log.warning(
            "facts: дополнительный банк вырос больше %d байт при чтении",
            FACT_BANK_MAX_BYTES,
        )
        _snapshot = _base_only_snapshot(
            file_state=FACT_FILE_INVALID,
            revision_payload=b"oversize-during-read",
        )
        return _snapshot

    try:
        document = parse_fact_bank_bytes(raw)
    except FactBankValidationError as e:
        log.warning("facts: дополнительный банк повреждён: %s", e)
        _snapshot = _base_only_snapshot(
            file_state=FACT_FILE_INVALID,
            revision_payload=b"invalid:" + hashlib.blake2s(raw).digest(),
        )
        return _snapshot
    canonical = serialize_fact_bank(document).encode("utf-8")
    _snapshot = _snapshot_for(
        document,
        file_state=FACT_FILE_VALID,
        revision_payload=canonical,
    )
    return _snapshot


def get_fact_bank_snapshot() -> FactBankSnapshot:
    """Вернуть текущий immutable snapshot без дискового ввода-вывода."""
    if _snapshot is None:
        raise RuntimeError("Банк фактов ещё не настроен")
    return _snapshot


def fact_bank_document_from_snapshot(
    snapshot: FactBankSnapshot | None = None,
) -> FactBankDocument:
    """Получить дополнительную часть снимка как проверенный документ."""
    current = snapshot or get_fact_bank_snapshot()
    return FactBankDocument(
        bank_version=current.bank_version,
        facts=current.additional_facts,
    )


def canonical_active_fact_bank() -> str:
    """Вернуть загружаемый канонический файл, включая пустой fallback."""
    return serialize_fact_bank(fact_bank_document_from_snapshot())


def fact_bank_candidate_revision(document: FactBankDocument) -> str:
    """Получить hash кандидата для привязки preview и apply."""
    return _revision(serialize_fact_bank(document).encode("utf-8"))


def fact_bank_delta(
    document: FactBankDocument,
    snapshot: FactBankSnapshot | None = None,
) -> tuple[int, int, int]:
    """Посчитать added, changed и removed относительно активного дополнения."""
    current = snapshot or get_fact_bank_snapshot()
    old = {fact.id: fact.text for fact in current.additional_facts}
    new = {fact.id: fact.text for fact in document.facts}
    added = len(new.keys() - old.keys())
    removed = len(old.keys() - new.keys())
    changed = sum(old[fact_id] != new[fact_id] for fact_id in old.keys() & new.keys())
    return added, changed, removed


def activate_restored_fact_bank(document: FactBankDocument) -> FactBankSnapshot:
    """Активировать уже опубликованный и проверенный restore-кандидат."""
    global _snapshot
    canonical = serialize_fact_bank(document).encode("utf-8")
    _snapshot = _snapshot_for(
        document,
        file_state=FACT_FILE_VALID,
        revision_payload=canonical,
    )
    return _snapshot


async def publish_fact_bank(
    document: FactBankDocument,
    *,
    expected_revision: str,
) -> FactBankSnapshot:
    """Атомарно заменить файл и снимок, отклонив устаревшее подтверждение."""
    global _snapshot
    canonical = serialize_fact_bank(document)
    async with restorable_state_transaction():
        current = reload_fact_bank()
        if current.revision != expected_revision:
            raise StaleFactBankError("дополнительный банк уже изменился")
        _atomic_write(FACTS_FILE, canonical)
        _snapshot = _snapshot_for(
            document,
            file_state=FACT_FILE_VALID,
            revision_payload=canonical.encode("utf-8"),
        )
        return _snapshot


async def clear_fact_bank(*, expected_revision: str) -> FactBankSnapshot:
    """Опубликовать пустой дополнительный банк с той же stale-защитой."""
    return await publish_fact_bank(
        empty_fact_bank_document(),
        expected_revision=expected_revision,
    )
