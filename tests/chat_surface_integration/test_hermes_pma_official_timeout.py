from __future__ import annotations

import pytest

from codex_autorunner.integrations.discord import message_turns as discord_message_turns
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as telegram_execution,
)

from .harness import (
    DiscordSurfaceHarness,
    HermesFixtureRuntime,
    TelegramSurfaceHarness,
    patch_hermes_runtime,
)

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_discord_hermes_pma_times_out_for_missing_terminal_and_missing_return(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official_prompt_hang")
    patch_hermes_runtime(monkeypatch, runtime)
    monkeypatch.setattr(discord_message_turns, "DISCORD_PMA_TIMEOUT_SECONDS", 0.05)
    harness = DiscordSurfaceHarness(tmp_path / "discord")
    await harness.setup(agent="hermes")
    try:
        rest = await harness.run_message("echo hello world")

        assert rest.execution_status == "error"
        assert rest.preview_deleted is True
        assert rest.terminal_progress_label == "failed"
        assert any(
            op["op"] == "edit"
            and "failed" in str(op["payload"].get("content", "")).lower()
            for op in rest.message_ops
        )
        assert any(
            op["op"] == "send"
            and "discord pma turn timed out"
            in str(op["payload"].get("content", "")).lower()
            for op in rest.message_ops
        )
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
async def test_telegram_hermes_pma_times_out_for_missing_terminal_and_missing_return(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesFixtureRuntime("official_prompt_hang")
    patch_hermes_runtime(monkeypatch, runtime)
    monkeypatch.setattr(telegram_execution, "TELEGRAM_PMA_TIMEOUT_SECONDS", 0.05)
    harness = TelegramSurfaceHarness(tmp_path / "telegram", timeout_seconds=8.0)
    await harness.setup(agent="hermes")
    try:
        bot = await harness.run_message("echo hello world")

        assert bot.execution_status == "error"
        assert bot.placeholder_deleted is True
        assert bot.messages[0]["text"] == "Working..."
        assert "telegram pma turn timed out" in bot.messages[1]["text"].lower()
        assert bot.deleted_messages == [
            {
                "chat_id": bot.messages[0]["chat_id"],
                "thread_id": bot.messages[0]["thread_id"],
                "message_id": 1,
            }
        ]
        assert bot.edited_messages == []
    finally:
        await harness.close()
        await runtime.close()
