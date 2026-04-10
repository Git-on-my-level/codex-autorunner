from __future__ import annotations

import pytest

from codex_autorunner.integrations.discord import message_turns as discord_message_turns

from .harness import DiscordSurfaceHarness, HermesFixtureRuntime, patch_hermes_runtime

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_discord_hermes_pma_times_out_for_official_prompt_hang(
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
