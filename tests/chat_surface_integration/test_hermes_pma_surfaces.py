from __future__ import annotations

import pytest

from tests.chat_surface_lab.transcript_models import TranscriptEventKind

from .harness import (
    DiscordSurfaceHarness,
    FakeDiscordRest,
    FakeTelegramBot,
    HermesFixtureRuntime,
    TelegramSurfaceHarness,
    build_discord_message_create,
    patch_hermes_runtime,
)

pytestmark = pytest.mark.integration


def _latest_execution_text(
    harness: DiscordSurfaceHarness | TelegramSurfaceHarness,
) -> str:
    surface_client = getattr(harness, "rest", None) or getattr(harness, "bot", None)
    thread_id = str(getattr(surface_client, "thread_target_id", "") or "")
    service = harness.orchestration_service()
    row = service.get_latest_execution(thread_id)
    return str(
        getattr(row, "output_text", None) or getattr(row, "assistant_text", None) or ""
    )


def _discord_final_texts(rest: FakeDiscordRest) -> list[str]:
    return [
        str(op["payload"].get("content") or "")
        for op in rest.message_ops
        if op.get("op") == "send"
        and str(op["payload"].get("content") or "") != "Received. Preparing turn..."
    ]


def _telegram_final_texts(bot: FakeTelegramBot) -> list[str]:
    return [
        str(item.get("text") or "")
        for item in bot.messages
        if str(item.get("text") or "") != "Working..."
    ]


@pytest.mark.anyio
async def test_discord_hermes_pma_delivers_three_turn_cumulative_outputs_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official_hermes_cumulative_session_update")
    patch_hermes_runtime(monkeypatch, runtime)
    harness = DiscordSurfaceHarness(tmp_path / "discord", timeout_seconds=12.0)
    await harness.setup(agent="hermes")
    rest = FakeDiscordRest()
    persisted_texts: list[str] = []
    try:
        for index, prompt in enumerate(("first", "second", "third"), start=1):
            await harness.run_gateway_events(
                [
                    (
                        "MESSAGE_CREATE",
                        build_discord_message_create(prompt, message_id=f"m-{index}"),
                    )
                ],
                rest_client=rest,
            )
            persisted_texts.append(_latest_execution_text(harness))

        expected = ["first answer", "second answer", "third answer"]
        assert persisted_texts == expected
        assert _discord_final_texts(rest)[-3:] == expected
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_telegram_hermes_pma_delivers_three_turn_cumulative_outputs_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official_hermes_cumulative_session_update")
    patch_hermes_runtime(monkeypatch, runtime)
    harness = TelegramSurfaceHarness(tmp_path / "telegram", timeout_seconds=5.0)
    await harness.setup(agent="hermes")
    persisted_texts: list[str] = []
    try:
        for prompt in ("first", "second", "third"):
            await harness.run_message(prompt)
            persisted_texts.append(_latest_execution_text(harness))

        expected = ["first answer", "second answer", "third answer"]
        assert persisted_texts == expected
        assert harness.bot is not None
        assert _telegram_final_texts(harness.bot)[-3:] == expected
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_discord_hermes_pma_delivers_current_segment_from_transcript_output(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official_hermes_transcript_session_update")
    patch_hermes_runtime(monkeypatch, runtime)
    harness = DiscordSurfaceHarness(tmp_path / "discord")
    await harness.setup(agent="hermes")
    rest = FakeDiscordRest()
    persisted_texts: list[str] = []
    try:
        for index, prompt in enumerate(("first", "second", "third"), start=1):
            await harness.run_gateway_events(
                [
                    (
                        "MESSAGE_CREATE",
                        build_discord_message_create(prompt, message_id=f"tx-{index}"),
                    )
                ],
                rest_client=rest,
            )
            persisted_texts.append(_latest_execution_text(harness))

        expected = ["first answer", "second answer", "third answer"]
        assert persisted_texts == expected
        assert _discord_final_texts(rest)[-3:] == expected
    finally:
        await harness.close()
        await runtime.close()


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
        finalized_record = next(
            record
            for record in rest.log_records
            if record.get("event") == "discord.turn.managed_thread_finalized"
        )
        assert finalized_record.get("preview_message_id") == progress_message_id

        delivery_started = next(
            record
            for record in rest.log_records
            if record.get("event") == "discord.turn.delivery_started"
        )
        assert delivery_started.get("preview_message_id") == progress_message_id

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

        # Durable delivery may publish the terminal reply just before the progress
        # placeholder deletion lands; the user-visible invariant is that both happen.
        assert any(
            op["op"] == "delete" and str(op["message_id"]) == progress_message_id
            for op in rest.message_ops
        )
        assert any(
            op["op"] == "send"
            and str(op["message_id"]) != progress_message_id
            and "fixture reply" in str(op["payload"].get("content", ""))
            for op in rest.message_ops
        )
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
