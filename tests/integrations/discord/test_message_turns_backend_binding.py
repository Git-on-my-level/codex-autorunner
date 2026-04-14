from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.discord_message_turns_support import (
    _config,
    _FakeGateway,
    _FakeOutboxManager,
    _FakeRest,
)

import codex_autorunner.integrations.discord.message_turns as discord_message_turns_module
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


@pytest.mark.anyio
async def test_resolve_discord_thread_target_keeps_existing_backend_binding_for_pma_followup(
    tmp_path: Path,
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
    service = DiscordBotService(
        _config(tmp_path, max_message_length=120),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    binding = SimpleNamespace(thread_target_id="thread-1", mode="pma")
    thread = SimpleNamespace(
        thread_target_id="thread-1",
        agent_id="hermes",
        agent_profile="m4-pma",
        workspace_root=str(workspace.resolve()),
        lifecycle_status="active",
        backend_thread_id="backend-existing",
        backend_runtime_instance_id="runtime-1",
    )
    resume_calls: list[dict[str, Any]] = []
    upserts: list[dict[str, Any]] = []

    class _FakeThreadService:
        def get_binding(self, *, surface_kind: str, surface_key: str) -> Any:
            assert surface_kind == "discord"
            assert surface_key == "channel-1"
            return binding

        def get_thread_target(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return thread

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            _ = thread_target_id
            resume_calls.append(dict(kwargs))
            raise AssertionError("resume_thread_target should not be called")

        def create_thread_target(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("create_thread_target should not be called")

        def upsert_binding(self, **kwargs: Any) -> None:
            upserts.append(kwargs)

    service._discord_managed_thread_orchestration_service = _FakeThreadService()

    try:
        _orch, resolved = discord_message_turns_module.resolve_discord_thread_target(
            service,
            channel_id="channel-1",
            workspace_root=workspace.resolve(),
            agent="hermes",
            agent_profile="m4-pma",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            mode="pma",
            pma_enabled=True,
        )

        assert resolved is thread
        assert resume_calls == []
        assert upserts == [
            {
                "surface_kind": "discord",
                "surface_key": "channel-1",
                "thread_target_id": "thread-1",
                "agent_id": "hermes",
                "repo_id": "repo-1",
                "resource_kind": "repo",
                "resource_id": "repo-1",
                "mode": "pma",
                "metadata": {"channel_id": "channel-1", "pma_enabled": True},
            }
        ]
    finally:
        await store.close()
