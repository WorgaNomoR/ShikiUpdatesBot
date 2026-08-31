# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Схема, публикация и immutable snapshot дополнительного банка фактов."""

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import fact_bank
from inline_facts import INLINE_FACTS

_FACT_BANK_EXAMPLE = Path(__file__).parent.parent / "examples" / "facts.json"


def _payload(*facts, bank_version="test-bank"):
    return {
        "schema_version": 1,
        "bank_version": bank_version,
        "facts": list(facts),
    }


def _raw(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_repository_example_is_valid_and_contains_five_facts():
    document = fact_bank.parse_fact_bank_bytes(_FACT_BANK_EXAMPLE.read_bytes())

    assert document.bank_version == "example-1"
    assert len(document.facts) == 5


def _fact(fact_id="extra-fact", text="Дополнительный факт."):
    return {"id": fact_id, "text": text}


def test_exact_schema_parses_and_serializes_canonically(fact_bank_env):
    document = fact_bank.parse_fact_bank_bytes(_raw(_payload(_fact())))

    assert document.bank_version == "test-bank"
    assert document.facts == (
        fact_bank.InlineFact("extra-fact", "Дополнительный факт."),
    )
    assert fact_bank.serialize_fact_bank(document) == (
        "{\n"
        '  "schema_version": 1,\n'
        '  "bank_version": "test-bank",\n'
        '  "facts": [\n'
        "    {\n"
        '      "id": "extra-fact",\n'
        '      "text": "Дополнительный факт."\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema_version": 1, "bank_version": "v"},
        {"schema_version": 2, "bank_version": None, "facts": []},
        {"schema_version": True, "bank_version": None, "facts": []},
        {"schema_version": 1, "bank_version": None, "facts": [_fact()]},
        {
            "schema_version": 1,
            "bank_version": "v",
            "facts": [{"id": "x", "text": "Текст", "owner_pick": True}],
        },
    ],
)
def test_schema_and_owner_marker_violations_are_rejected(fact_bank_env, payload):
    with pytest.raises(fact_bank.FactBankValidationError):
        fact_bank.parse_fact_bank_bytes(_raw(payload))


def test_duplicate_and_base_id_collisions_are_rejected(fact_bank_env):
    with pytest.raises(fact_bank.FactBankValidationError, match="повторяется"):
        fact_bank.parse_fact_bank_bytes(
            _raw(_payload(_fact("duplicate"), _fact("duplicate", "Другой текст")))
        )

    with pytest.raises(fact_bank.FactBankValidationError, match="хардкодной"):
        fact_bank.parse_fact_bank_bytes(
            _raw(_payload(_fact(INLINE_FACTS[0].id)))
        )


def test_exact_fact_count_boundary(fact_bank_env):
    exact = [_fact(f"fact-{index}") for index in range(fact_bank.FACT_BANK_MAX_FACTS)]
    assert (
        len(fact_bank.parse_fact_bank_bytes(_raw(_payload(*exact))).facts)
        == fact_bank.FACT_BANK_MAX_FACTS
    )

    with pytest.raises(
        fact_bank.FactBankValidationError,
        match=f"больше {fact_bank.FACT_BANK_MAX_FACTS}",
    ):
        fact_bank.parse_fact_bank_bytes(
            _raw(_payload(*exact, _fact("one-too-many")))
        )


def test_exact_id_and_text_boundaries(fact_bank_env):
    exact_id = "a" * fact_bank.FACT_ID_MAX_LENGTH
    exact_text = "я" * fact_bank.FACT_TEXT_MAX_LENGTH
    document = fact_bank.parse_fact_bank_bytes(
        _raw(_payload(_fact(exact_id, exact_text)))
    )
    assert document.facts[0].id == exact_id
    assert document.facts[0].text == exact_text

    with pytest.raises(fact_bank.FactBankValidationError, match="длиннее 32"):
        fact_bank.parse_fact_bank_bytes(
            _raw(_payload(_fact("a" * (fact_bank.FACT_ID_MAX_LENGTH + 1))))
        )
    with pytest.raises(fact_bank.FactBankValidationError, match="длиннее 1000"):
        fact_bank.parse_fact_bank_bytes(
            _raw(_payload(_fact(text="я" * (fact_bank.FACT_TEXT_MAX_LENGTH + 1))))
        )


@pytest.mark.parametrize(
    "fact_id,text",
    [
        ("Uppercase", "Текст"),
        ("-leading", "Текст"),
        ("trailing-", "Текст"),
        ("under_score", "Текст"),
        ("valid-id", ""),
        ("valid-id", " текст"),
        ("valid-id", "текст\x00"),
        ("valid-id", "текст\x7f"),
        ("valid-id", "текст\x80"),
        ("valid-id", "текст\x9f"),
    ],
)
def test_invalid_ids_and_text_are_rejected(fact_bank_env, fact_id, text):
    with pytest.raises(fact_bank.FactBankValidationError):
        fact_bank.parse_fact_bank_bytes(_raw(_payload(_fact(fact_id, text))))


def test_exact_bank_version_boundary(fact_bank_env):
    exact = "v" * fact_bank.FACT_BANK_VERSION_MAX_LENGTH
    document = fact_bank.parse_fact_bank_bytes(
        _raw(_payload(_fact(), bank_version=exact))
    )
    assert document.bank_version == exact

    with pytest.raises(fact_bank.FactBankValidationError, match="длиннее 64"):
        fact_bank.parse_fact_bank_bytes(
            _raw(_payload(_fact(), bank_version=exact + "x"))
        )


def test_exact_file_size_boundary(fact_bank_env):
    canonical = _raw(_payload(_fact()))
    exact = canonical + b" " * (fact_bank.FACT_BANK_MAX_BYTES - len(canonical))

    assert fact_bank.parse_fact_bank_bytes(exact).facts[0].id == "extra-fact"
    with pytest.raises(fact_bank.FactBankValidationError, match="больше"):
        fact_bank.parse_fact_bank_bytes(exact + b" ")


@pytest.mark.parametrize("raw", [b"", b"\xff", b"{broken"])
def test_empty_non_utf8_and_malformed_files_are_rejected(fact_bank_env, raw):
    with pytest.raises(fact_bank.FactBankValidationError):
        fact_bank.parse_fact_bank_bytes(raw)


def test_missing_malformed_and_unreadable_file_fall_back_to_base(fact_bank_env):
    missing = fact_bank.reload_fact_bank()
    assert missing.file_state == fact_bank.FACT_FILE_MISSING
    assert missing.facts == INLINE_FACTS

    fact_bank_env.write_bytes(b"{broken")
    malformed = fact_bank.reload_fact_bank()
    assert malformed.file_state == fact_bank.FACT_FILE_INVALID
    assert malformed.facts == INLINE_FACTS

    fact_bank_env.unlink()
    fact_bank_env.mkdir()
    unreadable = fact_bank.reload_fact_bank()
    assert unreadable.file_state == fact_bank.FACT_FILE_INVALID
    assert unreadable.facts == INLINE_FACTS


def test_oversized_external_file_falls_back_without_parsing(
    fact_bank_env,
    monkeypatch,
):
    fact_bank_env.write_bytes(b"x" * (fact_bank.FACT_BANK_MAX_BYTES + 1))
    parse = MagicMock(side_effect=AssertionError("oversized файл дошёл до parser"))
    monkeypatch.setattr(fact_bank, "parse_fact_bank_bytes", parse)

    snapshot = fact_bank.reload_fact_bank()

    assert snapshot.file_state == fact_bank.FACT_FILE_INVALID
    assert snapshot.facts == INLINE_FACTS
    assert snapshot.additional_facts == ()
    parse.assert_not_called()


def test_snapshot_is_frozen_and_additional_facts_cannot_gain_owner_marker(
    fact_bank_env,
):
    document = fact_bank.parse_fact_bank_bytes(_raw(_payload(_fact())))
    snapshot = fact_bank.activate_restored_fact_bank(document)

    assert isinstance(snapshot.facts, tuple)
    assert snapshot.facts[:len(INLINE_FACTS)] == INLINE_FACTS
    assert snapshot.additional_facts[0].owner_pick is False
    with pytest.raises(FrozenInstanceError):
        snapshot.bank_version = "changed"


@pytest.mark.asyncio
async def test_publish_replaces_whole_bank_and_updates_snapshot(fact_bank_env):
    initial = fact_bank.get_fact_bank_snapshot()
    first = fact_bank.parse_fact_bank_bytes(
        _raw(_payload(_fact("first"), _fact("removed")))
    )
    first_snapshot = await fact_bank.publish_fact_bank(
        first,
        expected_revision=initial.revision,
    )
    second = fact_bank.parse_fact_bank_bytes(
        _raw(_payload(_fact("first", "Изменён."), _fact("added"), bank_version="v2"))
    )
    second_snapshot = await fact_bank.publish_fact_bank(
        second,
        expected_revision=first_snapshot.revision,
    )

    assert [fact.id for fact in second_snapshot.additional_facts] == ["first", "added"]
    assert "removed" not in fact_bank_env.read_text(encoding="utf-8")
    assert fact_bank.fact_bank_delta(second, first_snapshot) == (1, 1, 1)


@pytest.mark.asyncio
async def test_atomic_write_failure_preserves_file_and_runtime_snapshot(
    fact_bank_env,
    monkeypatch,
):
    initial = fact_bank.get_fact_bank_snapshot()
    old = fact_bank.parse_fact_bank_bytes(_raw(_payload(_fact("old"))))
    old_snapshot = await fact_bank.publish_fact_bank(
        old,
        expected_revision=initial.revision,
    )
    old_payload = fact_bank_env.read_bytes()
    atomic_write = MagicMock(side_effect=OSError("disk full"))
    monkeypatch.setattr(fact_bank, "_atomic_write", atomic_write)

    new = fact_bank.parse_fact_bank_bytes(_raw(_payload(_fact("new"))))
    with pytest.raises(OSError, match="disk full"):
        await fact_bank.publish_fact_bank(
            new,
            expected_revision=old_snapshot.revision,
        )

    assert fact_bank_env.read_bytes() == old_payload
    assert fact_bank.get_fact_bank_snapshot() == old_snapshot
    atomic_write.assert_called_once()


@pytest.mark.asyncio
async def test_stale_revision_reloads_external_change_and_rejects_publish(fact_bank_env):
    initial = fact_bank.get_fact_bank_snapshot()
    old = fact_bank.parse_fact_bank_bytes(_raw(_payload(_fact("old"))))
    old_snapshot = await fact_bank.publish_fact_bank(
        old,
        expected_revision=initial.revision,
    )
    external = fact_bank.parse_fact_bank_bytes(
        _raw(_payload(_fact("external"), bank_version="external"))
    )
    fact_bank_env.write_text(fact_bank.serialize_fact_bank(external), encoding="utf-8")
    candidate = fact_bank.parse_fact_bank_bytes(_raw(_payload(_fact("candidate"))))

    with pytest.raises(fact_bank.StaleFactBankError):
        await fact_bank.publish_fact_bank(
            candidate,
            expected_revision=old_snapshot.revision,
        )

    snapshot = fact_bank.get_fact_bank_snapshot()
    assert [fact.id for fact in snapshot.additional_facts] == ["external"]
    assert "candidate" not in fact_bank_env.read_text(encoding="utf-8")


def test_canonical_download_uses_empty_bank_for_invalid_external_file(fact_bank_env):
    fact_bank_env.write_bytes(b"{broken")
    fact_bank.reload_fact_bank()

    assert json.loads(fact_bank.canonical_active_fact_bank()) == {
        "schema_version": 1,
        "bank_version": None,
        "facts": [],
    }
