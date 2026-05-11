from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.adapters.chat.channel_directory import ChannelDirectoryStore
from codex_autorunner.core.orchestration import (
    ChatSurfaceReadService,
    OrchestrationBindingStore,
    SQLiteChatSurfaceEventJournal,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_notification_store import PmaNotificationStore

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "chat_surface"
    / "lifecycle_contract.json"
)


def _seed_thread(
    hub_root: Path,
    *,
    thread_id: str,
    repo_id: str = "repo-1",
    lifecycle_status: str = "active",
    runtime_status: str = "idle",
) -> None:
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute(
            """
            INSERT INTO orch_thread_targets (
                thread_target_id,
                agent_id,
                repo_id,
                resource_kind,
                resource_id,
                display_name,
                lifecycle_status,
                runtime_status,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                "codex",
                repo_id,
                "repo",
                repo_id,
                f"Thread {thread_id}",
                lifecycle_status,
                runtime_status,
                "2026-05-11T00:00:00Z",
                "2026-05-11T00:00:10Z",
            ),
        )


def _seed_execution(hub_root: Path, *, thread_id: str, status: str) -> None:
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute(
            """
            INSERT INTO orch_thread_executions (
                execution_id,
                thread_target_id,
                request_kind,
                status,
                created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                f"exec-{thread_id}-{status}",
                thread_id,
                "message",
                status,
                "2026-05-11T00:01:00Z",
            ),
        )


def test_chat_surface_read_model_projects_contract_fixture_surface_inventory(
    tmp_path: Path,
) -> None:
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    expected_surface_kinds = {
        scenario["surface_kind"] for scenario in fixture["scenarios"]
    }
    hub_root = tmp_path / "hub"

    _seed_thread(hub_root, thread_id="thread-pma")
    _seed_thread(hub_root, thread_id="thread-discord")
    _seed_thread(hub_root, thread_id="thread-telegram")
    _seed_execution(hub_root, thread_id="thread-telegram", status="running")
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="thread-discord",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Discord channel"},
    )
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="telegram",
        surface_key="-1001:77",
        thread_target_id="thread-telegram",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Telegram topic"},
    )
    SQLiteChatSurfaceEventJournal(hub_root, durable=False).append_event(
        idempotency_key="web:queued",
        event_type="queue.state_changed",
        surface_kind="web",
        surface_key="chat:repo-1:thread-web",
        managed_thread_id="thread-web",
        repo_id="repo-1",
        status="queued",
    )
    ChannelDirectoryStore(hub_root).record_seen(
        "discord",
        "guild-channel-discovered",
        None,
        "Discovered channel",
    )
    PmaNotificationStore(hub_root).record_notification(
        correlation_id="corr-1",
        source_kind="pma",
        delivery_mode="direct",
        surface_kind="discord",
        surface_key="guild:channel",
        delivery_record_id="delivery-1",
        repo_id="repo-1",
        managed_thread_id="thread-discord",
        notification_id="notif-1",
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).snapshot()
    surfaces = snapshot["surfaces"]
    observed_kinds = {surface["surface_kind"] for surface in surfaces}

    assert expected_surface_kinds <= observed_kinds
    by_kind_key = {
        (surface["surface_kind"], surface["surface_key"]): surface
        for surface in surfaces
    }
    assert by_kind_key[("pma", "thread-pma")]["managed_thread_id"] == "thread-pma"
    assert by_kind_key[("discord", "guild:channel")]["lifecycle"] == "bound"
    assert by_kind_key[("telegram", "-1001:77")]["lifecycle"] == "running"
    assert by_kind_key[("web", "chat:repo-1:thread-web")]["lifecycle"] == "queued"
    assert (
        by_kind_key[("notification", "notification:notif-1")]["managed_thread_id"]
        == "thread-discord"
    )
    assert (
        "channel_directory"
        in by_kind_key[("discord", "guild-channel-discovered")]["facts"]
    )


def test_chat_surface_read_model_orders_and_limits_snapshot(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    journal = SQLiteChatSurfaceEventJournal(hub_root, durable=False)
    journal.append_event(
        idempotency_key="b",
        event_type="channel_directory.discovered",
        surface_kind="telegram",
        surface_key="2",
        status="discovered",
    )
    journal.append_event(
        idempotency_key="a",
        event_type="channel_directory.discovered",
        surface_kind="discord",
        surface_key="1",
        status="discovered",
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).snapshot(limit=1)

    assert [surface["surface_urn"] for surface in snapshot["surfaces"]] == ["discord:1"]
    assert snapshot["limits"] == {"requested": 1, "returned": 1, "max": 1000}
    assert snapshot["cursor"] == 2
