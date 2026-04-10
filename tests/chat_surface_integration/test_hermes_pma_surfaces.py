from __future__ import annotations

import pytest

from .harness import (
    DiscordSurfaceHarness,
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

        assert delete_index < reply_index
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
        sent_texts = [str(item["text"]) for item in bot.messages]
        assert sent_texts[0] == "Working..."
        assert any("fixture reply" in text for text in sent_texts[1:])
        assert bot.edited_messages == []
    finally:
        await harness.close()
        await runtime.close()
