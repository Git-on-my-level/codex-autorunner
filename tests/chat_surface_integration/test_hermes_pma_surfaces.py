from __future__ import annotations

import pytest

from .harness import (
    DiscordSurfaceHarness,
    HermesFixtureRuntime,
    TelegramSurfaceHarness,
    patch_hermes_runtime,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "scenario",
    ("terminal_missing_turn_id", "session_status_idle_completion_gap"),
)
@pytest.mark.anyio
async def test_discord_hermes_pma_completes_for_hermes_terminal_completion_signals(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    runtime = HermesFixtureRuntime(scenario)
    patch_hermes_runtime(monkeypatch, runtime)
    harness = DiscordSurfaceHarness(tmp_path / "discord")
    await harness.setup(agent="hermes")
    try:
        rest = await harness.run_message("echo hello world")
        progress_send = next(
            op
            for op in rest.message_ops
            if op["op"] == "send"
            and "working" in str(op["payload"].get("content", "")).lower()
        )
        progress_message_id = str(progress_send["message_id"])
        assert any(
            op["op"] == "edit" and str(op["message_id"]) == progress_message_id
            for op in rest.message_ops
        )
        delete_index = next(
            index
            for index, op in enumerate(rest.message_ops)
            if op["op"] == "delete" and str(op["message_id"]) == progress_message_id
        )
        reply_index = next(
            index
            for index, op in enumerate(rest.message_ops)
            if op["op"] == "send"
            and "fixture reply" in str(op["payload"].get("content", ""))
        )
        assert delete_index < reply_index
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.parametrize(
    "scenario",
    ("terminal_missing_turn_id", "session_status_idle_completion_gap"),
)
@pytest.mark.anyio
async def test_telegram_hermes_pma_completes_for_hermes_terminal_completion_signals(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    runtime = HermesFixtureRuntime(scenario)
    patch_hermes_runtime(monkeypatch, runtime)
    harness = TelegramSurfaceHarness(tmp_path / "telegram")
    await harness.setup(agent="hermes")
    try:
        bot = await harness.run_message("echo hello world")
        sent_texts = [str(item["text"]) for item in bot.messages]
        edited_texts = [str(item["text"]) for item in bot.edited_messages]
        assert any("working" in text.lower() for text in sent_texts + edited_texts)
        assert any("fixture reply" in text for text in sent_texts + edited_texts)
    finally:
        await harness.close()
        await runtime.close()
