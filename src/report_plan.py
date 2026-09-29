# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Pure-data контракт замороженных Telegram transport units отчёта."""

from copy import deepcopy

from rich_message_schema import RichMessageValidationError, validate_rich_payload


class FrozenReportPlanError(ValueError):
    """Замороженный transport plan нельзя безопасно продолжить."""


def html_transport_unit(content: str, *, disable_preview: bool) -> dict:
    """Собрать одну ordinary HTML transport unit."""
    unit = {
        "transport": "html",
        "content": content,
        "disable_preview": disable_preview,
    }
    validate_frozen_report_units([unit])
    return unit


def rich_transport_unit(
    payload: dict,
    fallback_html: list[str],
    *,
    fallback_disable_preview: bool,
) -> dict:
    """Собрать одну rich unit с заранее замороженным ordinary fallback."""
    unit = {
        "transport": "rich",
        "content": deepcopy(payload),
        "fallback_html": list(fallback_html),
        "fallback_disable_preview": fallback_disable_preview,
    }
    validate_frozen_report_units([unit])
    return unit


def _valid_html(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def validate_frozen_report_units(units: object) -> list[dict]:
    """Строго проверить transport/content без Telegram или файловой системы."""
    if not isinstance(units, list):
        raise FrozenReportPlanError("units_type")
    for unit in units:
        if not isinstance(unit, dict):
            raise FrozenReportPlanError("unit_type")
        transport = unit.get("transport")
        if transport == "html":
            if (
                set(unit) != {"transport", "content", "disable_preview"}
                or not _valid_html(unit.get("content"))
                or type(unit.get("disable_preview")) is not bool
            ):
                raise FrozenReportPlanError("html_unit")
            continue
        if transport == "rich":
            fallbacks = unit.get("fallback_html")
            if (
                set(unit) != {
                    "transport",
                    "content",
                    "fallback_html",
                    "fallback_disable_preview",
                }
                or not isinstance(fallbacks, list)
                or not fallbacks
                or any(not _valid_html(message) for message in fallbacks)
                or type(unit.get("fallback_disable_preview")) is not bool
            ):
                raise FrozenReportPlanError("rich_unit")
            try:
                validate_rich_payload(unit.get("content"))
            except RichMessageValidationError as exc:
                raise FrozenReportPlanError("rich_payload") from exc
            continue
        raise FrozenReportPlanError("transport")
    return units


def downgrade_rich_units(units: list[dict], start_unit: int) -> list[dict]:
    """Заменить текущую и оставшиеся rich units их frozen HTML fallback."""
    validate_frozen_report_units(units)
    if type(start_unit) is not int or not 0 <= start_unit < len(units):
        raise FrozenReportPlanError("progress_index")
    if units[start_unit]["transport"] != "rich":
        raise FrozenReportPlanError("unsupported_not_rich")
    downgraded = deepcopy(units[:start_unit])
    for unit in units[start_unit:]:
        if unit["transport"] == "html":
            downgraded.append(deepcopy(unit))
            continue
        downgraded.extend(
            html_transport_unit(
                message,
                disable_preview=unit["fallback_disable_preview"],
            )
            for message in unit["fallback_html"]
        )
    validate_frozen_report_units(downgraded)
    return downgraded
