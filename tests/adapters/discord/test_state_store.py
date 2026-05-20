from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.adapters.discord.state import (
    DISCORD_STATE_SCHEMA_VERSION,
    DiscordStateStore,
    OutboxRecord,
)
from codex_autorunner.adapters.discord.workspace_commands import (
    _bind_to_workspace_candidate,
    _list_bind_workspace_candidates,
    _resource_owner_for_workspace,
)
from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import (
    OrchestrationBindingStore,
    initialize_orchestration_sqlite,
)
from codex_autorunner.core.orchestration.chat_surface_events import (
    SQLiteChatSurfaceEventJournal,
)


@pytest.mark.anyio
async def test_channel_binding_crud(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    try:
        await store.initialize()
        await store.upsert_binding(
            channel_id="123",
            guild_id="456",
            workspace_path=str(tmp_path / "workspace"),
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
        )

        binding = await store.get_binding(channel_id="123")
        assert binding is not None
        assert binding["channel_id"] == "123"
        assert binding["guild_id"] == "456"
        assert binding["workspace_path"] == str(tmp_path / "workspace")
        assert binding["repo_id"] == "repo-1"
        assert binding["resource_kind"] == "repo"
        assert binding["resource_id"] == "repo-1"

        await store.upsert_binding(
            channel_id="123",
            guild_id="789",
            workspace_path=str(tmp_path / "new-workspace"),
            repo_id=None,
            resource_kind="worktree",
            resource_id="hub-wt-1",
        )
        binding = await store.get_binding(channel_id="123")
        assert binding is not None
        assert binding["guild_id"] == "789"
        assert binding["workspace_path"] == str(tmp_path / "new-workspace")
        assert binding["repo_id"] is None
        assert binding["resource_kind"] == "worktree"
        assert binding["resource_id"] == "hub-wt-1"

        all_bindings = await store.list_bindings()
        assert len(all_bindings) == 1
    finally:
        await store.close()


def test_workspace_scope_resolution_preserves_manifest_worktrees(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    base = hub_root / "base"
    worktree = hub_root / "worktrees" / "base--feature"
    base.mkdir(parents=True)
    worktree.mkdir(parents=True)
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        "\n".join(
            [
                "version: 3",
                "repos:",
                "  - id: base",
                "    path: base",
                "    kind: base",
                "  - id: base--feature",
                "    path: worktrees/base--feature",
                "    kind: worktree",
                "    worktree_of: base",
                "",
            ]
        ),
        encoding="utf-8",
    )
    service = SimpleNamespace(
        _config=SimpleNamespace(root=hub_root),
        _manifest_path=manifest_path,
    )

    assert _resource_owner_for_workspace(service, worktree) == (
        "worktree",
        "base--feature",
        "base",
    )
    assert _resource_owner_for_workspace(
        service,
        worktree,
        repo_id="base--feature",
    ) == ("worktree", "base--feature", "base")
    assert _list_bind_workspace_candidates(service)[0] == (
        "worktree",
        "base--feature",
        str(worktree.resolve()),
    )


@pytest.mark.anyio
async def test_stale_hermes_profile_does_not_override_explicit_codex_agent(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    try:
        await store.initialize()
        await store.upsert_binding(
            channel_id="123",
            guild_id="456",
            workspace_path=str(tmp_path / "workspace"),
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
        )
        await store.update_agent_state(
            channel_id="123",
            agent="hermes",
            agent_profile="m4-pma",
        )

        def contaminate_agent_profile() -> None:
            conn = store._connection_sync()
            with conn:
                conn.execute(
                    "UPDATE channel_bindings SET agent = ?, agent_profile = ? WHERE channel_id = ?",
                    ("codex", "m4-pma", "123"),
                )

        await store._run(contaminate_agent_profile)

        binding = await store.get_binding(channel_id="123")

        assert binding is not None
        assert binding["agent"] == "codex"
        assert binding["agent_profile"] is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_discord_workspace_bind_command_emits_chat_surface_events(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    first_workspace = hub_root / "repo-1"
    second_workspace = hub_root / "repo-2"
    first_workspace.mkdir(parents=True)
    second_workspace.mkdir(parents=True)
    store = DiscordStateStore(hub_root / ".codex-autorunner" / "discord_state.sqlite3")

    class FakeService:
        def __init__(self) -> None:
            self._config = SimpleNamespace(root=hub_root)
            self._store = store
            self.messages: list[str] = []

        def interaction_is_deferred(self, _interaction_token: str) -> bool:
            return False

        def interaction_has_initial_response(self, _interaction_token: str) -> bool:
            return False

        async def respond_ephemeral(
            self, _interaction_id: str, _interaction_token: str, text: str
        ) -> None:
            self.messages.append(text)

    service = FakeService()
    try:
        await _bind_to_workspace_candidate(
            service,
            "interaction-1",
            "token-1",
            channel_id="channel-1",
            guild_id="guild-1",
            selected_resource_kind="repo",
            selected_resource_id="repo-1",
            workspace_path=str(first_workspace),
        )
        await _bind_to_workspace_candidate(
            service,
            "interaction-2",
            "token-2",
            channel_id="channel-1",
            guild_id="guild-1",
            selected_resource_kind="repo",
            selected_resource_id="repo-2",
            workspace_path=str(second_workspace),
        )

        events = [
            event
            for event in SQLiteChatSurfaceEventJournal(hub_root).read_events_since(0)
            if event.source_kind == "discord.adapter.binding"
        ]
        assert [
            (event.event_type, event.surface_kind, event.surface_key, event.repo_id)
            for event in events
        ] == [
            ("surface.bound", "discord", "channel-1", "repo-1"),
            ("surface.rebound", "discord", "channel-1", "repo-2"),
        ]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pending_compact_seed_round_trip(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    try:
        await store.initialize()
        await store.upsert_binding(
            channel_id="123",
            guild_id="456",
            workspace_path=str(tmp_path / "workspace"),
            repo_id="repo-1",
        )

        await store.set_pending_compact_seed(
            channel_id="123",
            seed_text="summary text",
            session_key="session-key-1",
        )

        binding = await store.get_binding(channel_id="123")
        assert binding is not None
        assert binding["pending_compact_seed"] == "summary text"
        assert binding["pending_compact_session_key"] == "session-key-1"

        await store.clear_pending_compact_seed(channel_id="123")
        binding = await store.get_binding(channel_id="123")
        assert binding is not None
        assert binding["pending_compact_seed"] is None
        assert binding["pending_compact_session_key"] is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_recovery_notification_cursor_round_trip(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    try:
        await store.initialize()
        await store.upsert_binding(
            channel_id="123",
            guild_id="456",
            workspace_path=str(tmp_path / "workspace"),
            repo_id="repo-1",
        )

        await store.mark_recovery_notification_seen(
            channel_id="123",
            fingerprint="ticket_flow_recovery:abc",
            notified_at="2026-05-14T12:00:00+00:00",
        )

        binding = await store.get_binding(channel_id="123")
        assert binding is not None
        assert binding["last_recovery_fingerprint"] == "ticket_flow_recovery:abc"
        assert binding["last_recovery_notified_at"] == "2026-05-14T12:00:00+00:00"
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


@pytest.mark.anyio
async def test_discord_transport_store_remains_metadata_only_for_binding_identity(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = (hub_root / "worktrees" / "repo-1").resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    initialize_orchestration_sqlite(hub_root)
    thread = ManagedThreadStore(hub_root).create_thread(
        "codex",
        workspace_root,
        repo_id="repo-1",
        name="Discord thread",
    )
    binding_store = OrchestrationBindingStore(hub_root)
    binding_store.upsert_binding(
        surface_kind="discord",
        surface_key="channel-1",
        thread_target_id=str(thread["managed_thread_id"]),
        agent_id="codex",
        repo_id="repo-1",
    )

    store = DiscordStateStore(hub_root / ".codex-autorunner" / "discord_state.sqlite3")
    try:
        await store.initialize()
        await store.upsert_binding(
            channel_id="channel-1",
            guild_id="guild-1",
            workspace_path=str(workspace_root),
            repo_id="repo-1",
        )
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert "active_thread_id" not in binding
        assert binding_store.get_active_thread_for_binding(
            surface_kind="discord",
            surface_key="channel-1",
        ) == str(thread["managed_thread_id"])
    finally:
        await store.close()


@pytest.mark.anyio
async def test_interaction_ledger_round_trip_persists_ack_and_delivery(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "discord_state.sqlite3"
    store = DiscordStateStore(db_path)
    try:
        await store.initialize()
        registration = await store.register_interaction(
            interaction_id="inter-1",
            interaction_token="token-1",
            interaction_kind="slash_command",
            channel_id="chan-1",
            guild_id="guild-1",
            user_id="user-1",
            metadata_json={"command_path": ["car", "status"]},
        )
        assert registration.inserted is True

        await store.persist_interaction_runtime(
            "inter-1",
            route_key="slash:car/status",
            handler_id="car.status",
            conversation_id="discord:chan-1:guild-1",
            scheduler_state="dispatch_ready",
            resource_keys=("conversation:discord:chan-1:guild-1", "workspace:/tmp/ws"),
            payload_json={"id": "inter-1", "token": "token-1"},
            envelope_json={
                "interaction_id": "inter-1",
                "interaction_token": "token-1",
                "channel_id": "chan-1",
                "guild_id": "guild-1",
                "user_id": "user-1",
                "kind": "slash_command",
                "resource_keys": [
                    "conversation:discord:chan-1:guild-1",
                    "workspace:/tmp/ws",
                ],
                "dispatch_ack_policy": "defer_ephemeral",
                "queue_wait_ack_policy": None,
            },
        )
        await store.mark_interaction_acknowledged(
            "inter-1",
            ack_mode="defer_ephemeral",
        )
        await store.update_interaction_delivery_cursor(
            "inter-1",
            delivery_cursor_json={
                "state": "pending",
                "operation": "send_followup",
                "payload": {"content": "queued"},
            },
            scheduler_state="delivery_pending",
            increment_attempt_count=True,
        )
        await store.record_interaction_delivery(
            "inter-1",
            delivery_status="ack_deferred_ephemeral",
        )
    finally:
        await store.close()

    reopened = DiscordStateStore(db_path)
    try:
        await reopened.initialize()
        record = await reopened.get_interaction("inter-1")
        assert record is not None
        assert record.ack_mode == "defer_ephemeral"
        assert record.ack_completed_at is not None
        assert record.execution_status == "acknowledged"
        assert record.final_delivery_status == "ack_deferred_ephemeral"
        assert record.route_key == "slash:car/status"
        assert record.handler_id == "car.status"
        assert record.conversation_id == "discord:chan-1:guild-1"
        assert record.resource_keys == (
            "conversation:discord:chan-1:guild-1",
            "workspace:/tmp/ws",
        )
        assert record.payload_json == {"id": "inter-1", "token": "token-1"}
        assert record.envelope_json is not None
        assert record.envelope_json["dispatch_ack_policy"] == "defer_ephemeral"
        assert record.delivery_cursor_json == {
            "state": "pending",
            "operation": "send_followup",
            "payload": {"content": "queued"},
        }
        assert record.attempt_count == 1

        assert await reopened.claim_interaction_execution("inter-1") is True
        await reopened.mark_interaction_execution(
            "inter-1",
            execution_status="completed",
        )
        await reopened.record_interaction_delivery(
            "inter-1",
            delivery_status="followup_sent",
            original_response_message_id="msg-1",
        )

        completed = await reopened.get_interaction("inter-1")
        assert completed is not None
        assert completed.execution_status == "completed"
        assert completed.execution_finished_at is not None
        assert completed.original_response_message_id == "msg-1"
    finally:
        await reopened.close()


@pytest.mark.anyio
async def test_interaction_ledger_schema_migrates_from_v9(tmp_path: Path) -> None:
    db_path = tmp_path / "discord_state.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_info(version) VALUES (9)")
        conn.execute(
            """
            CREATE TABLE channel_bindings (
                channel_id TEXT PRIMARY KEY,
                guild_id TEXT,
                workspace_path TEXT NOT NULL,
                repo_id TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE outbox (
                record_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                message_id TEXT,
                operation TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                created_at TEXT NOT NULL,
                last_error TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    store = DiscordStateStore(db_path)
    try:
        await store.initialize()
    finally:
        await store.close()

    migrated = sqlite3.connect(db_path)
    try:
        version = migrated.execute(
            "SELECT version FROM schema_info ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert version is not None
        assert version[0] == DISCORD_STATE_SCHEMA_VERSION

        table_names = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "interaction_ledger" in table_names

        ledger_columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(interaction_ledger)")
        }
        assert {
            "interaction_id",
            "ack_mode",
            "execution_status",
            "final_delivery_status",
            "original_response_message_id",
        }.issubset(ledger_columns)
    finally:
        migrated.close()


@pytest.mark.anyio
async def test_interaction_ledger_prunes_stale_records_on_initialize(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "discord_state.sqlite3"
    store = DiscordStateStore(db_path)
    try:
        await store.initialize()
        registration = await store.register_interaction(
            interaction_id="stale-1",
            interaction_token="token-stale",
            interaction_kind="slash_command",
            channel_id="chan-1",
            guild_id="guild-1",
            user_id="user-1",
            metadata_json={"command_path": ["car", "status"]},
        )
        assert registration.inserted is True
    finally:
        await store.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE interaction_ledger
            SET last_seen_at = '2000-01-01T00:00:00Z',
                updated_at = '2000-01-01T00:00:00Z'
            WHERE interaction_id = 'stale-1'
            """
        )
        conn.commit()
    finally:
        conn.close()

    reopened = DiscordStateStore(db_path)
    try:
        await reopened.initialize()
        assert await reopened.get_interaction("stale-1") is None
    finally:
        await reopened.close()


@pytest.mark.anyio
async def test_interaction_ledger_rejects_unknown_scheduler_state(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    try:
        await store.initialize()
        await store.register_interaction(
            interaction_id="invalid-scheduler-1",
            interaction_token="token-invalid-scheduler-1",
            interaction_kind="slash_command",
            channel_id="chan-1",
            guild_id="guild-1",
            user_id="user-1",
            metadata_json={"command_path": ["car", "status"]},
        )

        with pytest.raises(
            ValueError, match="unknown interaction scheduler state: not_a_state"
        ):
            await store.mark_interaction_scheduler_state(
                "invalid-scheduler-1",
                scheduler_state="not_a_state",
            )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_interaction_ledger_rejects_unknown_execution_status(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    try:
        await store.initialize()
        await store.register_interaction(
            interaction_id="invalid-execution-1",
            interaction_token="token-invalid-execution-1",
            interaction_kind="slash_command",
            channel_id="chan-1",
            guild_id="guild-1",
            user_id="user-1",
            metadata_json={"command_path": ["car", "status"]},
        )

        with pytest.raises(
            ValueError, match="unknown interaction execution status: not_a_status"
        ):
            await store.mark_interaction_execution(
                "invalid-execution-1",
                execution_status="not_a_status",
            )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_interaction_ledger_rejects_scheduler_transition_from_terminal(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    try:
        await store.initialize()
        await store.register_interaction(
            interaction_id="terminal-scheduler-1",
            interaction_token="token-terminal-scheduler-1",
            interaction_kind="slash_command",
            channel_id="chan-1",
            guild_id="guild-1",
            user_id="user-1",
            metadata_json={"command_path": ["car", "status"]},
        )
        await store.mark_interaction_scheduler_state(
            "terminal-scheduler-1",
            scheduler_state="abandoned",
        )

        with pytest.raises(
            ValueError,
            match="illegal interaction scheduler transition: abandoned -> executing",
        ):
            await store.mark_interaction_scheduler_state(
                "terminal-scheduler-1",
                scheduler_state="executing",
            )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_interaction_ledger_rejects_execution_transition_from_terminal(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    try:
        await store.initialize()
        await store.register_interaction(
            interaction_id="terminal-execution-1",
            interaction_token="token-terminal-execution-1",
            interaction_kind="slash_command",
            channel_id="chan-1",
            guild_id="guild-1",
            user_id="user-1",
            metadata_json={"command_path": ["car", "status"]},
        )
        assert await store.claim_interaction_execution("terminal-execution-1") is True
        await store.mark_interaction_execution(
            "terminal-execution-1",
            execution_status="completed",
        )

        with pytest.raises(
            ValueError,
            match="illegal interaction execution transition: completed -> running",
        ):
            await store.mark_interaction_execution(
                "terminal-execution-1",
                execution_status="running",
            )
    finally:
        await store.close()
