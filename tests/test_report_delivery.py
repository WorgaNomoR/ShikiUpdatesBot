# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Контракты общей последовательной доставки типизированных отчётов."""

from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramNotFound,
    TelegramServerError,
)
from aiogram.methods import (
    SendMessage,
    SendRichMessage,
)
from aiogram.types import (
    BufferedInputFile,
    InputRichBlockCollage,
    InputRichBlockParagraph,
    InputRichMessage,
)

import report_delivery
import telegram_delivery
from report_assets import ReportAssetError
from report_delivery import (
    FAILED_REPORT_NOTICE,
    PARTIAL_REPORT_NOTICE,
    deliver_frozen_report,
    deliver_rendered_report,
    deliver_report,
    freeze_report,
    is_local_rich_asset_error,
)
from report_model import (
    TELEGRAM_TEXT_LIMIT,
    Bold,
    Poster,
    Report,
    Title,
    heading,
    line,
    section,
    unit,
)
from report_plan import FrozenReportPlanError
from rich_message_schema import RICH_TEXT_LIMIT

_METHOD = SendMessage(chat_id=1, text="test")
_RICH_METHOD = SendRichMessage(
    chat_id=1,
    rich_message=InputRichMessage(
        blocks=[InputRichBlockParagraph(text="test")],
        skip_entity_detection=True,
    ),
)


def _three_unit_report() -> Report:
    return Report(tuple(unit(section(line(f"unit-{index}"))) for index in range(3)))


def _three_unit_rich_report() -> Report:
    return Report(tuple(
        unit(section(heading(Bold(f"unit-{index}"), level=1)))
        for index in range(3)
    ))


def _poster_report() -> Report:
    return Report((unit(section(
        heading("Топ", collapsible=True),
        line(
            "  1. ",
            Title("first", None, Poster("https://cdn.example.test/first.jpg")),
        ),
        line("  2. ", Title("second", None, Poster(None))),
    )),))


@pytest.mark.asyncio
async def test_transient_retry_succeeds_and_delivery_continues(monkeypatch):
    transient = TelegramServerError(method=_METHOD, message="temporary")
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[transient, object(), object(), object()])
    retry_sleep = AsyncMock()
    gap_sleep = AsyncMock()
    monkeypatch.setattr(telegram_delivery, "_sleep", retry_sleep)

    result = await deliver_report(bot, 7, _three_unit_report(), sleep=gap_sleep)

    assert result.delivered is True
    assert result.delivered_units == result.total_units == 3
    assert bot.send_message.await_count == 4
    assert retry_sleep.await_count == 1
    assert gap_sleep.await_count == 2


@pytest.mark.asyncio
async def test_permanent_failure_stops_later_units_and_reports_partial_delivery():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[object(), RuntimeError("permanent"), object()])

    result = await deliver_report(
        bot,
        7,
        _three_unit_report(),
        notify_partial=True,
        sleep=AsyncMock(),
    )

    assert result.delivered is False
    assert result.delivered_units == 1
    assert result.total_units == 3
    assert result.partial_notice_delivered is True
    assert bot.send_message.await_count == 3
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == [
        "unit-0",
        "unit-1",
        PARTIAL_REPORT_NOTICE,
    ]


@pytest.mark.asyncio
async def test_exhausted_transient_failure_stops_before_next_unit(monkeypatch):
    transient = TelegramServerError(method=_METHOD, message="temporary")
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[transient, transient, transient, object()])
    monkeypatch.setattr(telegram_delivery, "_sleep", AsyncMock())

    result = await deliver_report(
        bot,
        7,
        _three_unit_report(),
        notify_partial=True,
        sleep=AsyncMock(),
    )

    assert result.delivered is False
    assert result.delivered_units == 0
    assert bot.send_message.await_count == 4
    texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert texts == ["unit-0", "unit-0", "unit-0", FAILED_REPORT_NOTICE]


@pytest.mark.asyncio
async def test_renderer_failure_returns_explicit_result_and_notice():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(bot, 7, object(), notify_partial=True)

    assert result.delivered is False
    assert result.delivered_units == result.total_units == 0
    assert isinstance(result.error, AttributeError)
    assert result.partial_notice_delivered is True
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == FAILED_REPORT_NOTICE


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_preview", [False, True])
async def test_preview_policy_is_applied_to_every_report_unit(disable_preview):
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(
        bot,
        7,
        _three_unit_report(),
        disable_preview=disable_preview,
        sleep=AsyncMock(),
    )

    assert result.delivered is True
    assert bot.send_message.await_count == 3
    assert {
        call.kwargs["disable_web_page_preview"]
        for call in bot.send_message.await_args_list
    } == {disable_preview}


@pytest.mark.asyncio
async def test_rendered_report_propagates_preview_policy():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_rendered_report(
        bot,
        7,
        ("first", "second"),
        disable_preview=True,
        sleep=AsyncMock(),
    )

    assert result.delivered is True
    assert result.delivered_units == result.total_units == 2
    assert bot.send_message.await_count == 2
    assert [
        call.kwargs["text"]
        for call in bot.send_message.await_args_list
    ] == ["first", "second"]
    assert all(
        call.kwargs["disable_web_page_preview"] is True
        for call in bot.send_message.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("messages,start", [([""], 0), ([None], 0), (["x"], -1), (["x"], True), (["x"], 0.5), (["x"], 2)])
async def test_rendered_plan_rejects_invalid_units_without_reindexing(messages, start):
    bot = MagicMock(send_message=AsyncMock())
    result = await deliver_rendered_report(bot, 7, messages, start_unit=start)
    assert result.delivered is False
    assert isinstance(result.error, ValueError)
    assert str(result.error) == "invalid_rendered_plan"
    assert result.next_unit == 0
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_frozen_plan_preserves_resume_position():
    units = [
        {
            "transport": "html",
            "content": "valid",
            "disable_preview": False,
        },
        {
            "transport": "html",
            "content": "",
            "disable_preview": False,
        },
    ]
    bot = MagicMock(send_message=AsyncMock())

    result = await deliver_frozen_report(bot, 7, units, start_unit=1)

    assert result.delivered is False
    assert isinstance(result.error, FrozenReportPlanError)
    assert str(result.error) == "html_unit"
    assert result.next_unit == 1
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_acknowledgement_failure_keeps_telegram_success_separate():
    bot = MagicMock(send_message=AsyncMock())
    before = AsyncMock()
    ack = AsyncMock(side_effect=OSError("disk"))
    result = await deliver_rendered_report(
        bot, 7, ["zero", "one", "two"], start_unit=1,
        before_send=before, acknowledge=ack, sleep=AsyncMock(),
    )
    assert result.delivered is False
    assert result.delivered_units == 1
    assert result.next_unit == 1
    assert result.total_units == 3
    assert isinstance(result.error, OSError)
    before.assert_awaited_once_with(1)
    ack.assert_awaited_once_with(1)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == "one"


@pytest.mark.asyncio
async def test_guard_runs_again_after_transport_retry_sleep(monkeypatch):
    transient = TelegramServerError(method=_METHOD, message="temporary")
    bot = MagicMock(send_message=AsyncMock(side_effect=transient))
    guard = AsyncMock(side_effect=[None, RuntimeError("changed")])
    ack = AsyncMock()
    monkeypatch.setattr(telegram_delivery, "_sleep", AsyncMock())
    result = await deliver_rendered_report(
        bot, 7, ["one", "two"], before_send=guard, acknowledge=ack,
    )
    assert result.delivered is False
    assert result.next_unit == result.delivered_units == 0
    assert guard.await_count == 2
    bot.send_message.assert_awaited_once()
    ack.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("messages,start", [([], 0), (["done"], 1)])
async def test_empty_or_completed_rendered_plan_has_no_send(messages, start):
    bot = MagicMock(send_message=AsyncMock())
    result = await deliver_rendered_report(bot, 7, messages, start_unit=start)
    assert result.delivered is True
    assert result.delivered_units == 0
    assert result.next_unit == start
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_report_uses_rich_transport_successfully():
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(return_value=object())
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(
        bot,
        7,
        _three_unit_rich_report(),
        sleep=AsyncMock(),
    )

    assert result.delivered is True
    assert result.delivered_units == result.total_units == 3
    assert bot.send_rich_message.await_count == 3
    bot.send_message.assert_not_awaited()
    assert all(
        call.kwargs["rich_message"].skip_entity_detection is True
        for call in bot.send_rich_message.await_args_list
    )


@pytest.mark.asyncio
async def test_local_placeholder_is_materialized_before_rich_send():
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(return_value=object())
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(bot, 7, _poster_report())

    assert result.delivered is True
    message = bot.send_rich_message.await_args.kwargs["rich_message"]
    details = message.blocks[1]
    collage = details.blocks[1]
    assert isinstance(collage, InputRichBlockCollage)
    assert collage.blocks[0].photo.media == "https://cdn.example.test/first.jpg"
    assert isinstance(collage.blocks[1].photo.media, BufferedInputFile)
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_asset_failure_selects_html_before_any_telegram_await(monkeypatch):
    monkeypatch.setattr(
        report_delivery,
        "materialize_rich_message",
        MagicMock(side_effect=ReportAssetError("asset_hash")),
    )
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(return_value=object())
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(bot, 7, _poster_report())

    assert result.delivered is True
    assert result.delivered_units == 1
    bot.send_rich_message.assert_not_awaited()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_asset_disappearing_after_freeze_uses_existing_html_fallback(monkeypatch):
    materialize = MagicMock(side_effect=[
        object(),
        object(),
        ReportAssetError("asset_unavailable"),
    ])
    monkeypatch.setattr(report_delivery, "materialize_rich_message", materialize)
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(return_value=object())
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(bot, 7, _poster_report())

    assert result.delivered is True
    assert result.delivered_units == 1
    assert materialize.call_count == 3
    bot.send_rich_message.assert_not_awaited()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_frozen_asset_failure_stops_before_any_telegram_await(monkeypatch):
    frozen_unit = freeze_report(_poster_report())[0]
    frozen = [frozen_unit, frozen_unit]
    materialize = MagicMock(side_effect=[object(), ReportAssetError("asset_hash")])
    monkeypatch.setattr(
        report_delivery,
        "materialize_rich_message",
        materialize,
    )
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(return_value=object())
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_frozen_report(bot, 7, frozen)

    assert result.delivered is False
    assert result.delivered_units == 0
    assert str(result.error) == "asset_hash"
    assert is_local_rich_asset_error(result.error) is True
    assert materialize.call_count == 2
    bot.send_rich_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_later_frozen_asset_failure_reports_its_exact_resume_position(monkeypatch):
    frozen_unit = freeze_report(_poster_report())[0]
    frozen = [frozen_unit, frozen_unit]
    materialize = MagicMock(side_effect=[
        object(),
        object(),
        ReportAssetError("asset_hash"),
    ])
    monkeypatch.setattr(report_delivery, "materialize_rich_message", materialize)
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(return_value=object())
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_frozen_report(bot, 7, frozen, sleep=AsyncMock())

    assert result.delivered is False
    assert result.delivered_units == 1
    assert result.next_unit == 1
    assert isinstance(result.error, ReportAssetError)
    assert materialize.call_count == 3
    bot.send_rich_message.assert_awaited_once()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_rich_asset_is_materialized_again_for_every_retry(monkeypatch):
    frozen = [freeze_report(_poster_report())[0]]
    preflight = object()
    first_attempt = object()
    second_attempt = object()
    materialize = MagicMock(side_effect=[
        preflight,
        first_attempt,
        second_attempt,
    ])
    monkeypatch.setattr(report_delivery, "materialize_rich_message", materialize)
    transient = TelegramServerError(method=_RICH_METHOD, message="temporary")
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(side_effect=[transient, object()])
    bot.send_message = AsyncMock()
    monkeypatch.setattr(telegram_delivery, "_sleep", AsyncMock())

    result = await deliver_frozen_report(bot, 7, frozen)

    assert result.delivered is True
    assert materialize.call_count == 3
    assert [
        call.kwargs["rich_message"]
        for call in bot.send_rich_message.await_args_list
    ] == [first_attempt, second_attempt]
    bot.send_message.assert_not_awaited()


def test_rich_unit_without_html_fallback_is_rejected_before_freezing():
    report = _three_unit_rich_report()
    ordinary = report_delivery.render_report(report)[1:]
    rich = report_delivery.render_rich_report(report)[:1]

    with pytest.raises(ValueError, match="^rich_unit_without_html_fallback$"):
        report_delivery._freeze_rich_plan(
            report,
            ordinary,
            rich,
            disable_preview=True,
        )


def test_rich_unit_outside_report_is_rejected_before_freezing():
    report = _three_unit_rich_report()
    ordinary = report_delivery.render_report(report)
    rich = report_delivery.render_rich_report(report)
    out_of_range = type(rich[0])(
        rich[0].message,
        rich[0].payload,
        len(report.units),
    )

    with pytest.raises(ValueError, match="^rich_unit_index_out_of_range$"):
        report_delivery._freeze_rich_plan(
            report,
            ordinary,
            (*rich[:-1], out_of_range),
            disable_preview=False,
        )


def test_duplicate_rich_unit_index_is_rejected_before_freezing():
    report = _three_unit_rich_report()
    ordinary = report_delivery.render_report(report)
    rich = report_delivery.render_rich_report(report)

    with pytest.raises(ValueError, match="^rich_unit_index_duplicate$"):
        report_delivery._freeze_rich_plan(
            report,
            ordinary,
            (*rich, rich[0]),
            disable_preview=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_preview", [False, True])
async def test_local_rich_render_failure_preserves_preview_policy_before_send(
    disable_preview,
):
    report = Report((unit(
        section(heading("Rich", level=1)),
        section(line("x" * (RICH_TEXT_LIMIT + 1))),
    ),))
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(return_value=object())
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(
        bot,
        7,
        report,
        disable_preview=disable_preview,
        sleep=AsyncMock(),
    )

    assert result.delivered is True
    bot.send_rich_message.assert_not_awaited()
    expected_html_messages = 1 + (
        RICH_TEXT_LIMIT + TELEGRAM_TEXT_LIMIT
    ) // TELEGRAM_TEXT_LIMIT
    assert bot.send_message.await_count == expected_html_messages
    assert all(
        call.kwargs["disable_web_page_preview"] is disable_preview
        for call in bot.send_message.await_args_list
    )


@pytest.mark.asyncio
async def test_exact_unsupported_rich_method_falls_back_without_duplication():
    unsupported = TelegramNotFound(
        method=_RICH_METHOD,
        message="Not Found",
    )
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(side_effect=unsupported)
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(
        bot,
        7,
        _three_unit_rich_report(),
        sleep=AsyncMock(),
    )

    assert result.delivered is True
    bot.send_rich_message.assert_awaited_once()
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == [
        "<b>unit-0</b>",
        "<b>unit-1</b>",
        "<b>unit-2</b>",
    ]
    assert all(
        call.kwargs["disable_web_page_preview"] is False
        for call in bot.send_message.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("ambiguous timeout"),
        TelegramNotFound(method=_RICH_METHOD, message="message not found"),
        TelegramServerError(method=_RICH_METHOD, message="ambiguous server failure"),
        TelegramNetworkError(method=_RICH_METHOD, message="ambiguous transport failure"),
    ],
)
async def test_nonexact_or_ambiguous_rich_failure_never_sends_html_fallback(
    error,
    monkeypatch,
):
    monkeypatch.setattr(telegram_delivery, "_sleep", AsyncMock())
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(side_effect=error)
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(bot, 7, _three_unit_rich_report())

    assert result.delivered is False
    assert result.delivered_units == 0
    bot.send_message.assert_not_awaited()
    expected_attempts = 3 if isinstance(
        error,
        (TelegramServerError, TelegramNetworkError),
    ) else 1
    assert bot.send_rich_message.await_count == expected_attempts


@pytest.mark.asyncio
async def test_rich_partial_delivery_stops_at_first_permanent_failure():
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(
        side_effect=[object(), RuntimeError("permanent"), object()]
    )
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(
        bot,
        7,
        _three_unit_rich_report(),
        notify_partial=True,
        sleep=AsyncMock(),
    )

    assert result.delivered is False
    assert result.delivered_units == 1
    assert bot.send_rich_message.await_count == 2
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == PARTIAL_REPORT_NOTICE


@pytest.mark.asyncio
async def test_disable_preview_keeps_rich_transport_and_freezes_fallback_policy():
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(return_value=object())
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(
        bot,
        7,
        _three_unit_rich_report(),
        disable_preview=True,
        sleep=AsyncMock(),
    )

    assert result.delivered is True
    assert bot.send_rich_message.await_count == 3
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_disable_preview_survives_exact_unsupported_rich_fallback():
    unsupported = TelegramNotFound(
        method=_RICH_METHOD,
        message="Not Found",
    )
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(side_effect=unsupported)
    bot.send_message = AsyncMock(return_value=object())

    result = await deliver_report(
        bot,
        7,
        _three_unit_rich_report(),
        disable_preview=True,
        sleep=AsyncMock(),
    )

    assert result.delivered is True
    bot.send_rich_message.assert_awaited_once()
    assert bot.send_message.await_count == 3
    assert all(
        call.kwargs["disable_web_page_preview"] is True
        for call in bot.send_message.await_args_list
    )
