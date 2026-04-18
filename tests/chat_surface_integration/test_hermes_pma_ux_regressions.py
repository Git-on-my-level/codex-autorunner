from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from codex_autorunner.integrations.chat.ux_regression_contract import (
    CHAT_UX_LATENCY_BUDGETS,
)
from codex_autorunner.integrations.discord import message_turns as discord_message_turns
from codex_autorunner.integrations.telegram.adapter import TelegramUpdate

from .harness import (
    DiscordSurfaceHarness,
    FakeDiscordRest,
    HermesFixtureRuntime,
    TelegramSurfaceHarness,
    build_telegram_message,
    drain_telegram_spawned_tasks,
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


def _discord_status_interaction(interaction_id: str) -> dict[str, Any]:
    return {
        "id": interaction_id,
        "token": f"{interaction_id}-token",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": "car",
            "options": [{"type": 1, "name": "status", "options": []}],
        },
    }


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
    runtime = HermesFixtureRuntime("official_prompt_hang")
    patch_hermes_runtime(monkeypatch, runtime)

    discord = DiscordSurfaceHarness(tmp_path / "discord-queued", timeout_seconds=15.0)
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
async def test_telegram_queue_visibility_before_recovery(
    tmp_path,
) -> None:
    harness = TelegramSurfaceHarness(tmp_path / "telegram-queued", timeout_seconds=15.0)
    await harness.setup(agent="hermes", approval_mode="yolo")
    try:
        assert harness.service is not None
        assert harness.bot is not None
        topic_key = await harness.service._router.resolve_key(123, 55)
        runtime = harness.service._router.runtime_for(topic_key)
        runtime.current_turn_id = "busy-turn"

        await harness.service._dispatch_update(
            TelegramUpdate(
                update_id=2,
                message=build_telegram_message(
                    "echo queued after busy",
                    thread_id=55,
                    message_id=2,
                    update_id=2,
                ),
                callback=None,
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert any(
            "Queued (waiting for available worker...)" in str(item.get("text") or "")
            for item in harness.bot.messages
        )
        runtime.current_turn_id = None
        runtime.queue.cancel_pending()
    finally:
        await harness.close()


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
            discord.rest.edited_original_interaction_responses[0]["payload"]["content"]
            == "Stopping current turn..."
        )
        discord_final_interrupt_content = (
            discord.rest.edited_original_interaction_responses[-1]["payload"]["content"]
        )
        assert discord_final_interrupt_content != "Stopping current turn..."
        assert discord_final_interrupt_content in {
            "Interrupt succeeded.",
            "Recovered stale session after backend thread was lost.",
        }
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
        assert any(
            event.get("kind") == "callback" and event.get("text") == "Stopping..."
            for event in telegram.bot.surface_timeline
        )
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


@pytest.mark.anyio
async def test_telegram_duplicate_updates_are_deduped(
    tmp_path,
) -> None:
    harness = TelegramSurfaceHarness(tmp_path / "telegram-duplicate-updates")
    await harness.setup(agent="hermes")
    try:
        assert harness.service is not None
        assert harness.bot is not None
        update = TelegramUpdate(
            update_id=77,
            message=build_telegram_message(
                "/status",
                thread_id=55,
                message_id=77,
                update_id=77,
            ),
            callback=None,
        )
        harness.bot.enable_duplicate_update(77)
        for delivered in harness.bot.expand_update_delivery(update):
            await harness.service._dispatch_update(delivered)
        await drain_telegram_spawned_tasks(harness.service)

        status_messages = [
            item
            for item in harness.bot.messages
            if "Workspace:" in str(item.get("text") or "")
        ]
        assert len(status_messages) == 1
        duplicate = await harness.wait_for_log_event(
            "telegram.update.duplicate",
            timeout_seconds=2.0,
            predicate=lambda record: record.get("update_id") == 77,
        )
        assert duplicate.get("chat_id") == 123
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_discord_duplicate_interactions_are_deduped(
    tmp_path,
) -> None:
    harness = DiscordSurfaceHarness(tmp_path / "discord-duplicate-interactions")
    await harness.setup(agent="hermes")
    try:
        rest_client = FakeDiscordRest()
        rest_client.enable_duplicate_interaction("inter-dup-1")
        payload = _discord_status_interaction("inter-dup-1")
        events = [
            ("INTERACTION_CREATE", delivered)
            for delivered in rest_client.expand_interaction_delivery(payload)
        ]
        rest = await harness.run_gateway_events(events, rest_client=rest_client)

        response_texts: list[str] = []
        response_texts.extend(
            str(item.get("payload", {}).get("data", {}).get("content", ""))
            for item in rest.interaction_responses
        )
        response_texts.extend(
            str(item.get("payload", {}).get("content", ""))
            for item in rest.followup_messages
        )
        status_messages = [
            text for text in response_texts if "workspace:" in text.lower()
        ]
        assert len(status_messages) == 1
        duplicate_resumes = [
            record
            for record in rest.log_records
            if record.get("event") == "discord.interaction.duplicate_resuming"
            and record.get("interaction_id") == "inter-dup-1"
        ]
        assert duplicate_resumes
        assert any(
            record.get("event") == "discord.interaction.ack.reused"
            and record.get("interaction_id") == "inter-dup-1"
            for record in rest.log_records
        )
        assert any(
            event.get("kind") == "duplicate_interaction_injected"
            for event in rest.surface_timeline
        )
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_discord_timeout_lifecycle_is_explicit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official_prompt_hang")
    patch_hermes_runtime(monkeypatch, runtime)
    monkeypatch.setattr(discord_message_turns, "DISCORD_PMA_TIMEOUT_SECONDS", 0.05)
    harness = DiscordSurfaceHarness(tmp_path / "discord-timeout-lifecycle")
    await harness.setup(agent="hermes")
    try:
        rest = await harness.run_message("echo hello world")
        assert rest.execution_status == "error"
        assert rest.preview_deleted is True
        finalized = _latest_event(
            rest.log_records, "chat.managed_thread.turn_finalized"
        )
        assert finalized.get("status") == "error"
        assert "timed out" in str(finalized.get("detail") or "").lower()
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_telegram_topic_and_root_routing_behavior_is_explicit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official")
    patch_hermes_runtime(monkeypatch, runtime)
    harness = TelegramSurfaceHarness(tmp_path / "telegram-topic-root")
    await harness.setup(agent="hermes")
    try:
        topic_bot = await harness.run_message("echo topic route", thread_id=55)
        topic_message_count = len(topic_bot.messages)

        root_bot = await harness.run_message("echo root route", thread_id=None)
        assert root_bot.thread_target_id is None
        assert root_bot.surface_key == "123:root"
        assert len(root_bot.messages) == topic_message_count
        root_policy = await harness.wait_for_log_event(
            "telegram.collaboration_policy.evaluated",
            timeout_seconds=2.0,
            predicate=lambda record: (
                record.get("thread_id") is None
                and record.get("policy_outcome") == "command_only_destination"
            ),
        )
        assert root_policy.get("policy_command_allowed") is True
    finally:
        await harness.close()
        await runtime.close()
