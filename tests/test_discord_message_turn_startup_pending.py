from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.integrations.discord import (
    message_turns as discord_message_turns_module,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore
from tests.discord_message_turns_support import (
    _config,
    _FakeGateway,
    _FakeOutboxManager,
    _FakeRest,
    _message_create,
)


@pytest.mark.anyio
async def test_message_create_startup_pending_keeps_placeholder_without_public_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("please continue"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _startup_pending(**kwargs: Any) -> Any:
        _ = kwargs
        raise discord_message_turns_module.DiscordTurnStartupPending(
            "Discord turn is still starting up. Please retry in a moment."
        )

    monkeypatch.setattr(
        service,
        "_run_agent_turn_for_message",
        _startup_pending,
    )

    try:
        await asyncio.wait_for(service.run_forever(), timeout=5)
        contents = [msg["payload"].get("content", "") for msg in rest.channel_messages]
        assert "Received. Preparing turn..." in contents
        assert not any("Turn failed:" in content for content in contents)
    finally:
        await store.close()
