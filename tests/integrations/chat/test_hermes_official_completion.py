from __future__ import annotations

import asyncio

import anyio
import pytest
from tests.chat_surface_harness.discord import (
    build_discord_pma_environment,
    discord_message_create,
)
from tests.chat_surface_harness.hermes import patch_hermes_registry
from tests.chat_surface_harness.telegram import (
    build_telegram_pma_environment,
    drain_spawned_tasks,
    telegram_message,
)

import codex_autorunner.integrations.discord.message_turns as discord_message_turns_module
from codex_autorunner.integrations.chat.dispatcher import build_dispatch_context
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as telegram_execution_module,
)
from codex_autorunner.integrations.telegram.handlers.commands import (
    shared as telegram_shared_module,
)


@pytest.mark.anyio
async def test_discord_pma_turn_completes_for_official_hermes_prompt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = patch_hermes_registry(
        monkeypatch,
        scenario="official",
        targets=[discord_message_turns_module],
    )
    env = await build_discord_pma_environment(
        tmp_path,
        message_content="echo hello world",
    )
    try:
        event = env.service._chat_adapter.parse_message_event(
            discord_message_create("echo hello world")
        )
        assert event is not None
        await asyncio.wait_for(
            env.service._handle_message_event(event, build_dispatch_context(event)),
            timeout=3,
        )
        with anyio.fail_after(2):
            while not any(
                "fixture reply" in message["payload"].get("content", "")
                for message in env.rest.channel_messages
            ):
                await anyio.sleep(0.05)

        assert env.rest.channel_messages[0]["payload"]["content"] == (
            "Received. Preparing turn..."
        )
        assert any(
            "working" in edit["payload"].get("content", "")
            for edit in env.rest.edited_channel_messages
        )
        assert env.rest.deleted_channel_messages
        assert any(
            "fixture reply" in message["payload"].get("content", "")
            for message in env.rest.channel_messages
        )
    finally:
        await runtime.close()
        await env.close()


@pytest.mark.anyio
async def test_telegram_pma_turn_completes_for_official_hermes_prompt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = patch_hermes_registry(
        monkeypatch,
        scenario="official",
        targets=[telegram_execution_module, telegram_shared_module],
    )
    env = await build_telegram_pma_environment(tmp_path)
    try:
        message = telegram_message("echo hello world")
        await asyncio.wait_for(env.service._handle_message_inner(message), timeout=3)
        await asyncio.wait_for(drain_spawned_tasks(env.service), timeout=3)

        assert str(env.bot.messages[0].get("text") or "") == "Working..."
        assert any(
            "fixture reply" in str(sent.get("text") or "") for sent in env.bot.messages
        )
    finally:
        await runtime.close()
        await env.close()
