from __future__ import annotations

from typing import Any, Callable

import pytest

from codex_autorunner.integrations.chat.ux_regression_contract import (
    CHAT_UX_LATENCY_BUDGETS,
)

from .harness import (
    DiscordSurfaceHarness,
    HermesFixtureRuntime,
    TelegramSurfaceHarness,
    patch_hermes_runtime,
)

pytestmark = pytest.mark.integration

_BUDGETS = {entry.id: entry.max_ms for entry in CHAT_UX_LATENCY_BUDGETS}


def _latest_event(
    records: list[dict[str, Any]],
    event_name: str,
    *,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    for record in reversed(records):
        if record.get("event") != event_name:
            continue
        if predicate is not None and not predicate(record):
            continue
        return record
    raise AssertionError(f"missing log event: {event_name}")


def _assert_budget(record: dict[str, Any], field: str, budget_id: str) -> None:
    value = record.get(field)
    assert isinstance(value, (int, float)), (field, record)
    assert float(value) <= _BUDGETS[budget_id], (field, value, _BUDGETS[budget_id])


@pytest.mark.anyio
async def test_surfaces_emit_fast_visible_feedback_and_reuse_progress_anchor(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official")
    patch_hermes_runtime(monkeypatch, runtime)

    discord = DiscordSurfaceHarness(tmp_path / "discord-visible")
    telegram = TelegramSurfaceHarness(tmp_path / "telegram-visible")
    await discord.setup(agent="hermes")
    await telegram.setup(agent="hermes")
    try:
        discord_rest = await discord.run_message("echo hello world")
        telegram_bot = await telegram.run_message("echo hello world")

        discord_timing = _latest_event(
            discord_rest.log_records,
            "chat_ux_timing.discord.turn_delivery",
        )
        _assert_budget(
            discord_timing,
            "chat_ux_delta_first_visible_ms",
            "first_visible_feedback",
        )
        _assert_budget(
            discord_timing,
            "chat_ux_delta_first_progress_ms",
            "first_semantic_progress",
        )
        assert discord_rest.preview_message_id == "msg-1"
        assert discord_rest.preview_deleted is True
        preview_sends = [
            op
            for op in discord_rest.message_ops
            if op["op"] == "send"
            and str(op.get("message_id") or "") == discord_rest.preview_message_id
        ]
        assert len(preview_sends) == 1
        preview_edits = [
            op
            for op in discord_rest.message_ops
            if op["op"] == "edit"
            and str(op.get("message_id") or "") == discord_rest.preview_message_id
        ]
        assert len(preview_edits) >= 2
        assert any(
            "working" in str(op["payload"].get("content", "")).lower()
            for op in preview_edits
        )
        assert any(
            "done" in str(op["payload"].get("content", "")).lower()
            for op in preview_edits
        )

        telegram_timing = _latest_event(
            telegram_bot.log_records,
            "chat_ux_timing.telegram.managed_thread_turn",
        )
        _assert_budget(
            telegram_timing,
            "chat_ux_delta_first_visible_ms",
            "first_visible_feedback",
        )
        _assert_budget(
            telegram_timing,
            "chat_ux_delta_first_progress_ms",
            "first_semantic_progress",
        )
        sent_texts = [str(item.get("text") or "") for item in telegram_bot.messages]
        assert sent_texts[0] == "Working..."
        assert sent_texts.count("Working...") == 1
        assert telegram_bot.placeholder_deleted is True
        assert telegram_bot.edited_messages == []
    finally:
        await discord.close()
        await telegram.close()
        await runtime.close()


@pytest.mark.anyio
async def test_surfaces_make_busy_thread_queue_visible_before_recovery(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official")
    patch_hermes_runtime(monkeypatch, runtime)

    discord = DiscordSurfaceHarness(tmp_path / "discord-queued", timeout_seconds=8.0)
    await discord.setup(agent="hermes", approval_mode="yolo")
    try:
        discord_first = discord.start_message("cancel me")
        discord_thread_target_id, discord_execution_id = (
            await discord.wait_for_running_execution(timeout_seconds=2.0)
        )
        await discord.submit_active_message("echo queued after busy", message_id="m-2")
        queued_submission = await discord.wait_for_log_event(
            "discord.turn.managed_thread_submission",
            timeout_seconds=2.0,
            predicate=lambda record: record.get("queued") is True,
        )
        assert queued_submission.get("managed_thread_id") == discord_thread_target_id
        assert queued_submission.get("execution_id") != discord_execution_id
        await discord.orchestration_service().stop_thread(discord_thread_target_id)
        discord_result = await discord_first
        assert discord_result.execution_status == "interrupted"
        discord_timing = await discord.wait_for_log_event(
            "chat_ux_timing.discord.turn_delivery",
            timeout_seconds=8.0,
            predicate=lambda record: (
                record.get("chat_ux_delta_queue_visible_ms") is not None
            ),
        )
        _assert_budget(
            discord_timing,
            "chat_ux_delta_queue_visible_ms",
            "queue_visible",
        )
    finally:
        await discord.close()
        await runtime.close()


@pytest.mark.anyio
async def test_surfaces_acknowledge_interrupt_controls_before_final_confirmation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official")
    patch_hermes_runtime(monkeypatch, runtime)

    discord = DiscordSurfaceHarness(
        tmp_path / "discord-interrupt-ui",
        timeout_seconds=8.0,
    )
    telegram = TelegramSurfaceHarness(
        tmp_path / "telegram-interrupt-ui",
        timeout_seconds=8.0,
    )
    await discord.setup(agent="hermes", approval_mode="yolo")
    await telegram.setup(agent="hermes", approval_mode="yolo")
    try:
        discord_task = discord.start_message("cancel me")
        discord_thread_target_id, discord_execution_id = (
            await discord.wait_for_running_execution(timeout_seconds=2.0)
        )
        await discord.interrupt_active_turn_via_component(
            thread_target_id=discord_thread_target_id,
            execution_id=discord_execution_id,
        )
        assert discord.rest is not None
        assert discord.rest.interaction_responses[0]["payload"]["type"] == 6
        assert (
            discord.rest.edited_original_interaction_responses[-1]["payload"]["content"]
            == "Stopping current turn..."
        )
        discord_ack = await discord.wait_for_log_event(
            "discord.turn.cancel_acknowledged"
        )
        _assert_budget(
            discord_ack,
            "chat_ux_delta_interrupt_visible_ms",
            "interrupt_visible",
        )
        discord_confirmed = await discord.wait_for_log_event(
            "discord.interrupt.completed"
        )
        assert discord_confirmed.get("interrupt_state") == "confirmed"
        await discord_task

        telegram_task = telegram.start_message("cancel me")
        await telegram.wait_for_running_execution(timeout_seconds=2.0)
        await telegram.interrupt_active_turn_via_callback()
        assert telegram.bot is not None
        assert telegram.bot.callback_answers[-1]["text"] == "Stopping..."
        telegram_result = await telegram_task
        assert telegram_result.execution_status == "interrupted"
        telegram_finalized = _latest_event(
            telegram.bot.log_records,
            "chat.managed_thread.turn_finalized",
        )
        assert telegram_finalized.get("status") == "interrupted"
        assert "Telegram PMA turn interrupted" in str(
            telegram_finalized.get("detail") or ""
        )
    finally:
        await discord.close()
        await telegram.close()
        await runtime.close()
