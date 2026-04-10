from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

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
async def test_message_create_startup_failure_keeps_generic_error_without_raw_detail(
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

    submit_started = asyncio.Event()

    async def _hanging_submit(self, *args, **kwargs):
        _ = args, kwargs
        submit_started.set()
        await asyncio.Future()

    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (
            SimpleNamespace(),
            SimpleNamespace(thread_target_id="thread-1"),
        ),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "DISCORD_MANAGED_THREAD_SUBMISSION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        discord_message_turns_module.ManagedThreadTurnCoordinator,
        "submit_execution",
        _hanging_submit,
    )

    try:
        await asyncio.wait_for(service.run_forever(), timeout=5)
        assert submit_started.is_set()
        assert rest.edited_channel_messages
        assert any(
            "Turn failed to start in time. Please retry."
            in item["payload"].get("content", "")
            for item in rest.edited_channel_messages
        )
        contents = [msg["payload"].get("content", "") for msg in rest.channel_messages]
        assert not any("Turn failed:" in content for content in contents)
    finally:
        await store.close()
