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
    metadata: dict[str, object] | None = None,
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
                metadata_json,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(metadata or {}),
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


def test_chat_surface_read_model_projects_thread_identity_from_metadata_json(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-pma-profiled",
        metadata={"agent_profile": "m4-pma", "model": "gpt-5.5"},
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).snapshot()
    by_kind_key = {
        (surface["surface_kind"], surface["surface_key"]): surface
        for surface in snapshot["surfaces"]
    }

    surface = by_kind_key[("pma", "thread-pma-profiled")]
    assert surface["managed_thread_id"] == "thread-pma-profiled"
    assert surface["metadata"]["agent_id"] == "codex"
    assert surface["metadata"]["agent_profile"] == "m4-pma"
    assert surface["metadata"]["model"] == "gpt-5.5"


def test_chat_index_and_detail_prefer_bound_chat_display_for_fallback_titles(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
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
                metadata_json,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "thread-discord-friendly",
                "codex",
                "repo-1",
                "repo",
                "repo-1",
                "discord:1495134681929355404",
                "active",
                "idle",
                "{}",
                "2026-05-11T00:00:00Z",
                "2026-05-11T00:00:10Z",
            ),
        )
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
                metadata_json,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "thread-discord-custom",
                "codex",
                "repo-1",
                "repo",
                "repo-1",
                "Customer escalation",
                "active",
                "idle",
                "{}",
                "2026-05-11T00:01:00Z",
                "2026-05-11T00:01:10Z",
            ),
        )
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="1495134681929355404",
        thread_target_id="thread-discord-friendly",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Agent Nexus / #codex"},
    )
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="1495134681929355405",
        thread_target_id="thread-discord-custom",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Agent Nexus / #support"},
    )

    service = ChatSurfaceReadService(hub_root, durable=False)
    index = service.chat_index_snapshot(limit=20)
    row = next(
        item
        for item in index["rows"]
        if item["managed_thread_id"] == "thread-discord-friendly"
    )
    assert row["title"] == "Agent Nexus / #codex"
    assert row["chat_display_name"] == "Agent Nexus / #codex"

    detail = service.chat_detail_snapshot("thread-discord-friendly")
    assert detail["thread"]["title"] == "Agent Nexus / #codex"
    assert detail["thread"]["chat_display_name"] == "Agent Nexus / #codex"

    custom_detail = service.chat_detail_snapshot("thread-discord-custom")
    assert custom_detail["thread"]["title"] == "Customer escalation"
    assert custom_detail["thread"]["chat_display_name"] == "Agent Nexus / #support"


def test_chat_surface_read_model_allows_lifecycle_recovery_events(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    journal = SQLiteChatSurfaceEventJournal(hub_root, durable=False)
    journal.append_event(
        idempotency_key="recovering:failed",
        event_type="execution.progress",
        surface_kind="telegram",
        surface_key="-100:1",
        managed_thread_id="thread-recovering",
        repo_id="repo-1",
        status="failed",
    )
    journal.append_event(
        idempotency_key="recovering:running",
        event_type="execution.progress",
        surface_kind="telegram",
        surface_key="-100:1",
        managed_thread_id="thread-recovering",
        repo_id="repo-1",
        status="running",
    )
    journal.append_event(
        idempotency_key="completed:failed",
        event_type="execution.progress",
        surface_kind="discord",
        surface_key="guild:done",
        managed_thread_id="thread-completed",
        repo_id="repo-1",
        status="failed",
    )
    journal.append_event(
        idempotency_key="completed:delivered",
        event_type="delivery.status_changed",
        surface_kind="discord",
        surface_key="guild:done",
        managed_thread_id="thread-completed",
        repo_id="repo-1",
        status="delivered",
    )
    _seed_thread(hub_root, thread_id="thread-still-failed")
    _seed_execution(hub_root, thread_id="thread-still-failed", status="failed")
    journal.append_event(
        idempotency_key="still-failed:old-bound",
        event_type="surface.bound",
        surface_kind="pma",
        surface_key="thread-still-failed",
        managed_thread_id="thread-still-failed",
        repo_id="repo-1",
        status="bound",
        occurred_at="2026-05-10T00:00:00Z",
    )
    _seed_thread(hub_root, thread_id="thread-pma-recovered")
    _seed_execution(hub_root, thread_id="thread-pma-recovered", status="failed")
    journal.append_event(
        idempotency_key="pma-recovered:running",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key="thread-pma-recovered",
        managed_thread_id="thread-pma-recovered",
        repo_id="repo-1",
        status="running",
        occurred_at="2026-05-11T00:02:00Z",
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).snapshot()
    by_kind_key = {
        (surface["surface_kind"], surface["surface_key"]): surface
        for surface in snapshot["surfaces"]
    }

    assert by_kind_key[("telegram", "-100:1")]["lifecycle"] == "running"
    assert by_kind_key[("discord", "guild:done")]["lifecycle"] == "idle"
    assert by_kind_key[("pma", "thread-still-failed")]["lifecycle"] == "failed"
    pma_snapshot = ChatSurfaceReadService(hub_root, durable=False).pma_compat_snapshot()
    by_thread_id = {
        thread["managed_thread_id"]: thread for thread in pma_snapshot["threads"]
    }
    assert by_thread_id["thread-pma-recovered"]["normalized_status"] == "running"


def test_chat_surface_read_model_builds_pma_compat_snapshot(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(hub_root, thread_id="thread-pma", runtime_status="idle")
    _seed_execution(hub_root, thread_id="thread-pma", status="queued")
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="thread-pma",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Discord channel"},
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).pma_compat_snapshot()

    assert snapshot["contract_version"] == "pma_chat_events.v1"
    assert snapshot["cursor"] == 1
    assert len(snapshot["revision"]) == 64
    assert snapshot["threads"] == [
        {
            "managed_thread_id": "thread-pma",
            "agent": "codex",
            "agent_profile": None,
            "repo_id": "repo-1",
            "resource_kind": "repo",
            "resource_id": "repo-1",
            "workspace_root": None,
            "name": "Thread thread-pma",
            "model": None,
            "backend_thread_id": None,
            "lifecycle_status": "active",
            "runtime_status": "queued",
            "normalized_status": "queued",
            "status": "queued",
            "target_runtime_status": "idle",
            "execution_status": "queued",
            "active_turn_id": "exec-thread-pma-queued",
            "queued_count": 1,
            "status_reason": None,
            "status_changed_at": None,
            "status_terminal": False,
            "status_turn_id": None,
            "last_turn_id": None,
            "last_message_preview": None,
            "compact_seed": None,
            "accepts_messages": True,
            "updated_at": "2026-05-11T00:01:00Z",
            "created_at": "2026-05-11T00:00:00Z",
            "operator_status": "queued",
            "is_reusable": False,
            "chat_bound": True,
            "binding_kind": "discord",
            "binding_id": "guild:channel",
            "chat_display_name": "Discord channel",
            "binding_count": 1,
            "binding_kinds": ["discord"],
            "binding_ids": ["guild:channel"],
            "chat_display_names": ["Discord channel"],
            "cleanup_protected": False,
        }
    ]
