from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.integrations.discord.state import DiscordStateStore, OutboxRecord


@pytest.mark.anyio
async def test_channel_binding_crud(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    try:
        await store.initialize()
        await store.upsert_binding(
            channel_id="123",
            guild_id="456",
            workspace_path="/tmp/workspace",
            repo_id="repo-1",
        )

        binding = await store.get_binding(channel_id="123")
        assert binding is not None
        assert binding["channel_id"] == "123"
        assert binding["guild_id"] == "456"
        assert binding["workspace_path"] == "/tmp/workspace"
        assert binding["repo_id"] == "repo-1"

        await store.upsert_binding(
            channel_id="123",
            guild_id="789",
            workspace_path="/tmp/new-workspace",
            repo_id=None,
        )
        binding = await store.get_binding(channel_id="123")
        assert binding is not None
        assert binding["guild_id"] == "789"
        assert binding["workspace_path"] == "/tmp/new-workspace"
        assert binding["repo_id"] is None

        all_bindings = await store.list_bindings()
        assert len(all_bindings) == 1
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_enqueue_list_get_and_deliver(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    try:
        await store.initialize()
        record = OutboxRecord(
            record_id="rec-1",
            channel_id="channel-1",
            message_id=None,
            operation="send",
            payload_json={"content": "hello"},
            created_at="2026-01-01T00:00:00Z",
        )
        await store.enqueue_outbox(record)

        loaded = await store.get_outbox("rec-1")
        assert loaded is not None
        assert loaded.channel_id == "channel-1"
        assert loaded.operation == "send"
        assert loaded.payload_json == {"content": "hello"}

        records = await store.list_outbox()
        assert len(records) == 1
        assert records[0].record_id == "rec-1"

        await store.mark_outbox_delivered("rec-1")
        assert await store.get_outbox("rec-1") is None
    finally:
        await store.close()
