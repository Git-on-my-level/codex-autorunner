from __future__ import annotations

import pytest

from tests.chat_surface_lab.transcript_models import TranscriptEventKind

from .harness import (
    DiscordSurfaceHarness,
    FakeDiscordRest,
    FakeTelegramBot,
    HermesFixtureRuntime,
    TelegramSurfaceHarness,
    patch_hermes_runtime,
)

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_discord_hermes_pma_uses_official_placeholder_lifecycle(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official")
    patch_hermes_runtime(monkeypatch, runtime)
    harness = DiscordSurfaceHarness(tmp_path / "discord")
    await harness.setup(agent="hermes")
    try:
        rest = await harness.run_message("echo hello world")

        assert rest.execution_status == "ok"
        assert rest.execution_id
        assert rest.preview_message_id
        assert rest.preview_deleted is True
        assert rest.terminal_progress_label == "done"
        assert any(event.get("kind") == "send" for event in rest.surface_timeline)
        transcript = rest.to_normalized_transcript(
            scenario_id="discord-placeholder-lifecycle"
        )
        assert any(
            event.kind == TranscriptEventKind.SEND for event in transcript.events
        )
        assert rest.message_ops[0]["op"] == "send"
        assert (
            rest.message_ops[0]["payload"]["content"] == "Received. Preparing turn..."
        )

        progress_message_id = str(rest.message_ops[0]["message_id"])
        working_edits = [
            op
            for op in rest.message_ops
            if op["op"] == "edit"
            and str(op["message_id"]) == progress_message_id
            and "working" in str(op["payload"].get("content", "")).lower()
        ]
        assert working_edits

        done_edit = next(
            op
            for op in rest.message_ops
            if op["op"] == "edit"
            and str(op["message_id"]) == progress_message_id
            and "done" in str(op["payload"].get("content", "")).lower()
        )
        assert done_edit["payload"]["components"] == []

        delete_index = next(
            index
            for index, op in enumerate(rest.message_ops)
            if op["op"] == "delete" and str(op["message_id"]) == progress_message_id
        )
        reply_index = next(
            index
            for index, op in enumerate(rest.message_ops)
            if op["op"] == "send"
            and str(op["message_id"]) != progress_message_id
            and "fixture reply" in str(op["payload"].get("content", ""))
        )

        # Durable delivery may publish the terminal reply just before the progress
        # placeholder deletion lands; the user-visible invariant is that both happen.
        assert delete_index != reply_index
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_telegram_hermes_pma_uses_official_turn_delivery_flow(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official")
    patch_hermes_runtime(monkeypatch, runtime)
    harness = TelegramSurfaceHarness(tmp_path / "telegram")
    await harness.setup(agent="hermes")
    try:
        bot = await harness.run_message("echo hello world")
        assert bot.execution_status == "ok"
        assert bot.execution_id
        assert bot.placeholder_deleted is True
        sent_texts = [str(item["text"]) for item in bot.messages]
        assert sent_texts[0] == "Working..."
        assert any("fixture reply" in text for text in sent_texts[1:])
        assert bot.edited_messages == []
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_discord_hermes_pma_handles_official_session_update_content_parts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official_content_parts")
    patch_hermes_runtime(monkeypatch, runtime)
    harness = DiscordSurfaceHarness(tmp_path / "discord")
    await harness.setup(agent="hermes")
    try:
        rest = await harness.run_message("echo hello world")

        assert rest.execution_status == "ok"
        assert rest.preview_deleted is True
        assert rest.terminal_progress_label == "done"
        assert any(
            op["op"] == "send"
            and "fixture reply" in str(op["payload"].get("content", ""))
            for op in rest.message_ops
        )
        assert any(
            op["op"] == "edit"
            and "done" in str(op["payload"].get("content", "")).lower()
            for op in rest.message_ops
        )
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_telegram_hermes_pma_handles_official_session_update_content_parts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official_content_parts")
    patch_hermes_runtime(monkeypatch, runtime)
    harness = TelegramSurfaceHarness(tmp_path / "telegram")
    await harness.setup(agent="hermes")
    try:
        bot = await harness.run_message("echo hello world")

        assert bot.execution_status == "ok"
        assert bot.placeholder_deleted is True
        sent_texts = [str(item["text"]) for item in bot.messages]
        assert sent_texts[0] == "Working..."
        assert any("fixture reply" in text for text in sent_texts[1:])
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_discord_hermes_pma_handles_terminal_event_before_official_return(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime(
        "official_terminal_before_return",
        logger_name="test.chat_surface_integration.discord",
    )
    patch_hermes_runtime(monkeypatch, runtime)
    harness = DiscordSurfaceHarness(tmp_path / "discord")
    await harness.setup(agent="hermes")
    try:
        rest = await harness.run_message("echo hello world")

        assert rest.execution_status == "ok"
        assert rest.preview_deleted is True
        assert any(
            record.get("event") == "acp.prompt.terminal_recorded"
            and record.get("completion_source") == "terminal_event"
            for record in rest.log_records
        )
        assert any(
            record.get("event") == "chat.managed_thread.turn_finalized"
            and record.get("last_runtime_method") == "prompt/completed"
            for record in rest.log_records
        )
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_telegram_hermes_pma_handles_terminal_event_before_official_return(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime(
        "official_terminal_before_return",
        logger_name="test.chat_surface_integration.telegram",
    )
    patch_hermes_runtime(monkeypatch, runtime)
    harness = TelegramSurfaceHarness(tmp_path / "telegram")
    await harness.setup(agent="hermes")
    try:
        bot = await harness.run_message("echo hello world")

        assert bot.execution_status == "ok"
        assert bot.placeholder_deleted is True
        assert any(
            record.get("event") == "acp.prompt.terminal_recorded"
            and record.get("completion_source") == "terminal_event"
            for record in bot.log_records
        )
        assert any(
            record.get("event") == "chat.managed_thread.turn_finalized"
            and record.get("last_runtime_method") == "prompt/completed"
            for record in bot.log_records
        )
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_discord_hermes_pma_handles_terminal_event_without_official_return(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime(
        "official_terminal_without_request_return",
        logger_name="test.chat_surface_integration.discord",
    )
    patch_hermes_runtime(monkeypatch, runtime)
    harness = DiscordSurfaceHarness(tmp_path / "discord")
    await harness.setup(agent="hermes")
    try:
        rest = await harness.run_message("echo hello world")

        assert rest.execution_status == "ok"
        assert rest.preview_deleted is True
        assert any(
            op["op"] == "send"
            and "fixture reply" in str(op["payload"].get("content", ""))
            for op in rest.message_ops
        )
        assert any(
            record.get("event") == "acp.prompt.terminal_recorded"
            and record.get("completion_source") == "terminal_event"
            for record in rest.log_records
        )
        assert not any(
            record.get("event") == "acp.prompt.request_returned"
            for record in rest.log_records
        )
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_telegram_hermes_pma_handles_terminal_event_without_official_return(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime(
        "official_terminal_without_request_return",
        logger_name="test.chat_surface_integration.telegram",
    )
    patch_hermes_runtime(monkeypatch, runtime)
    harness = TelegramSurfaceHarness(tmp_path / "telegram")
    await harness.setup(agent="hermes")
    try:
        bot = await harness.run_message("echo hello world")

        assert bot.execution_status == "ok"
        assert bot.placeholder_deleted is True
        sent_texts = [str(item["text"]) for item in bot.messages]
        assert sent_texts[0] == "Working..."
        assert any("fixture reply" in text for text in sent_texts[1:])
        assert any(
            record.get("event") == "acp.prompt.terminal_recorded"
            and record.get("completion_source") == "terminal_event"
            for record in bot.log_records
        )
        assert not any(
            record.get("event") == "acp.prompt.request_returned"
            for record in bot.log_records
        )
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_discord_hermes_pma_characterizes_stale_preview_when_delivery_cleanup_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official")
    patch_hermes_runtime(monkeypatch, runtime)
    harness = DiscordSurfaceHarness(tmp_path / "discord")
    await harness.setup(agent="hermes")
    try:
        rest = await harness.run_message(
            "echo hello world",
            rest_client=FakeDiscordRest(fail_delete_message_ids={"msg-1"}),
        )

        assert rest.execution_status == "ok"
        assert rest.background_tasks_drained is True
        assert rest.preview_message_id == "msg-1"
        assert rest.preview_deleted is False
        assert rest.terminal_progress_label == "done"
        assert any(
            record.get("event") == "discord.channel_message.delete_failed"
            and record.get("message_id") == "msg-1"
            for record in rest.log_records
        )
        assert any(
            record.get("event") == "discord.turn.delivery_finished"
            and record.get("background_task_owner") == "discord.turn.delivery"
            and record.get("preview_message_id") == "msg-1"
            and record.get("execution_id") == rest.execution_id
            and record.get("preview_message_deleted") is False
            for record in rest.log_records
        )
        assert any(
            record.get("event") == "discord.turn.managed_thread_finalized"
            and record.get("background_task_owner")
            == "discord.turn.background_delivery"
            and record.get("preview_message_id") == "msg-1"
            and record.get("execution_id") == rest.execution_id
            for record in rest.log_records
        )
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_telegram_hermes_pma_characterizes_stale_preview_when_delivery_cleanup_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official")
    patch_hermes_runtime(monkeypatch, runtime)
    harness = TelegramSurfaceHarness(tmp_path / "telegram")
    await harness.setup(agent="hermes")
    try:
        bot = await harness.run_message(
            "echo hello world",
            bot_client=FakeTelegramBot(fail_delete_message_ids={1}),
        )

        assert bot.execution_status == "ok"
        assert bot.background_tasks_drained is True
        assert bot.placeholder_message_id == 1
        assert bot.placeholder_deleted is False
        sent_texts = [str(item["text"]) for item in bot.messages]
        assert sent_texts[0] == "Working..."
        assert any("fixture reply" in text for text in sent_texts[1:])
        assert not any(item.get("message_id") == 1 for item in bot.deleted_messages)
    finally:
        await harness.close()
        await runtime.close()
