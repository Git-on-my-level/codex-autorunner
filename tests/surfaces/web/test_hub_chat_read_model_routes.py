from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import (
    OrchestrationBindingStore,
    SQLiteChatSurfaceEventJournal,
    ticket_flow_thread_metadata,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_notification_store import PmaNotificationStore
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.read_model_contracts import (
    ChatDetailSnapshot,
    ChatIndexSnapshot,
    load_read_model_contract,
)
from codex_autorunner.surfaces.web.routes import hub_chat_read_models
from codex_autorunner.surfaces.web.routes.hub_chat_read_models import (
    hub_group_dict_to_contract,
)


def _seed_thread_rows(hub_root: Path, count: int) -> None:
    with open_orchestration_sqlite(hub_root, durable=True, migrate=True) as conn:
        with conn:
            for index in range(count):
                status = "running" if index == 3 else "idle"
                agent_id = "hermes" if index == 3 else "codex"
                metadata = {"model": "gpt-5.5"}
                if index == 3:
                    metadata["agent_profile"] = "m4-pma"
                    metadata["chat_kind"] = "coding_agent"
                lifecycle = "archived" if index == 7 else "active"
                resource_kind = "ticket" if index < 12 else "repo"
                resource_id = "TICKET-900" if index < 12 else "repo"
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
                        f"thread-{index:04d}",
                        agent_id,
                        "repo",
                        resource_kind,
                        resource_id,
                        f"Thread {index:04d}",
                        lifecycle,
                        status,
                        json.dumps(metadata),
                        "2026-05-11T00:00:00Z",
                        f"2026-05-11T00:{index % 60:02d}:00Z",
                    ),
                )
                if index in {1, 5}:
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
                            f"exec-{index}",
                            f"thread-{index:04d}",
                            "message",
                            "queued",
                            "2026-05-11T01:00:00Z",
                        ),
                    )


def _insert_thread_row(
    hub_root: Path,
    *,
    thread_id: str,
    display_name: str,
    lifecycle_status: str = "active",
    runtime_status: str = "idle",
    updated_at: str = "2026-05-11T00:00:00Z",
) -> None:
    with open_orchestration_sqlite(hub_root, durable=True, migrate=True) as conn:
        with conn:
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
                    "repo",
                    "repo",
                    "repo",
                    display_name,
                    lifecycle_status,
                    runtime_status,
                    "{}",
                    "2026-05-11T00:00:00Z",
                    updated_at,
                ),
            )


def _insert_chat_thread_row(
    hub_root: Path,
    *,
    thread_id: str,
    repo_id: str,
    resource_kind: str,
    resource_id: str,
    display_name: str,
    runtime_status: str,
    metadata: dict[str, object] | None = None,
    updated_at: str = "2026-05-11T00:00:00Z",
) -> None:
    with open_orchestration_sqlite(hub_root, durable=True, migrate=True) as conn:
        with conn:
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
                    resource_kind,
                    resource_id,
                    display_name,
                    "active",
                    runtime_status,
                    json.dumps(metadata or {}),
                    "2026-05-11T00:00:00Z",
                    updated_at,
                ),
            )


def _write_ticket(path: Path, *, done: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"title: {path.stem}",
                "agent: codex",
                f"done: {'true' if done else 'false'}",
                "---",
                "",
                "## Goal",
                "- Regression fixture ticket.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _archive_thread_row(hub_root: Path, *, thread_id: str, updated_at: str) -> None:
    with open_orchestration_sqlite(hub_root, durable=True, migrate=True) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET lifecycle_status = 'archived',
                       runtime_status = 'completed',
                       updated_at = ?
                 WHERE thread_target_id = ?
                """,
                (updated_at, thread_id),
            )


def _seed_archived_surface_events(hub_root: Path, count: int, *, prefix: str) -> None:
    with open_orchestration_sqlite(hub_root, durable=True, migrate=True) as conn:
        with conn:
            conn.executemany(
                """
                INSERT INTO orch_chat_surface_events (
                    idempotency_key,
                    event_type,
                    surface_kind,
                    surface_key,
                    managed_thread_id,
                    repo_id,
                    lifecycle_status,
                    status,
                    occurred_at,
                    created_at,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        f"{prefix}-{index}",
                        "surface.archived",
                        "pma",
                        f"old-thread-{index}",
                        f"old-thread-{index}",
                        "repo",
                        "archived",
                        "archived",
                        "2026-05-11T00:00:00Z",
                        "2026-05-11T00:00:00Z",
                        "{}",
                    )
                    for index in range(count)
                ),
            )


def _event_payloads(text: str, event_name: str) -> list[dict]:
    payloads: list[dict] = []
    current_event: str | None = None
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ")
            data_lines = []
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))
        elif line == "" and current_event == event_name and data_lines:
            payloads.append(json.loads("\n".join(data_lines)))
            current_event = None
            data_lines = []
    return payloads


def test_chat_index_snapshot_filters_groups_and_bounds_large_windows(hub_env) -> None:
    _seed_thread_rows(hub_env.hub_root, 1000)
    OrchestrationBindingStore(hub_env.hub_root, durable=True).upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="thread-0003",
        repo_id="repo",
        resource_kind="ticket",
        resource_id="TICKET-900",
        metadata={"display_name": "Discord channel"},
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get("/hub/chat/index", params={"view": "active", "limit": 25})

    assert response.status_code == 200
    payload = response.json()
    assert payload["contract_version"] == "chat_index_read.v1"
    assert payload["window"]["returned"] == 1
    assert payload["rows"][0]["managed_thread_id"] == "thread-0003"
    assert payload["rows"][0]["agent"] == "hermes"
    assert payload["rows"][0]["agent_profile"] == "m4-pma"
    assert payload["rows"][0]["chat_kind"] == "coding_agent"
    assert "discord" in payload["rows"][0]["surface_kinds"]
    assert payload["window"]["total_count"] == 1

    grouped = client.get(
        "/hub/chat/index",
        params={"group_by": "ticket_run", "view": "ticket_run", "limit": 10},
    ).json()
    assert grouped["rows"] == []

    children = client.get(
        "/hub/chat/index",
        params={
            "group_by": "ticket_run",
            "parent_group_id": "ticket:TICKET-900",
            "limit": 5,
        },
    ).json()
    assert children["window"]["returned"] == 0
    assert children["rows"] == []


def test_chat_index_group_contract_uses_explicit_ticket_run_group_kind() -> None:
    group = hub_group_dict_to_contract(
        {
            "kind": "ticket_run_group",
            "group_id": "ticket-run:run-1",
            "title": "Legacy ticket run",
            "child_count": 3,
            "waiting_count": 1,
            "running_count": 2,
            "unread_count": 4,
            "last_sort_activity_at": "2026-05-11T00:01:00Z",
            "last_lifecycle_update_at": "2026-05-11T00:05:00Z",
            "debug": {"activity": {"selected_source": "last_sort_activity_at"}},
        }
    )

    assert group.kind == "ticket_run_group"
    assert group.group_id == "ticket-run:run-1"
    assert group.run_id == "run-1"
    assert group.waiting_count == 1
    assert group.running_count == 2
    assert group.unread_count == 4
    assert group.last_activity_at is not None
    assert group.last_sort_activity_at is not None
    assert group.last_sort_activity_at.isoformat() == group.last_activity_at.isoformat()
    assert group.debug == {"activity": {"selected_source": "last_sort_activity_at"}}


def test_chat_index_contract_uses_terminal_thread_status_and_ticket_flow_metadata(
    hub_env,
) -> None:
    thread_id = "thread-ticket-flow-complete"
    with open_orchestration_sqlite(
        hub_env.hub_root, durable=True, migrate=True
    ) as conn:
        with conn:
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
                    "repo",
                    "worktree",
                    "repo--discord-4",
                    "ticket-flow:codex",
                    "active",
                    "completed",
                    json.dumps(
                        ticket_flow_thread_metadata(
                            flow_run_id="run-015",
                            ticket_id="TICKET-015",
                            workspace_root=str(hub_env.repo_root),
                        )
                    ),
                    "2026-05-15T11:57:27Z",
                    "2026-05-15T12:08:56Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO orch_thread_executions (
                    execution_id,
                    thread_target_id,
                    request_kind,
                    status,
                    created_at,
                    started_at,
                    finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "turn-ticket-flow-complete",
                    thread_id,
                    "message",
                    "ok",
                    "2026-05-15T12:03:18Z",
                    "2026-05-15T12:03:18Z",
                    "2026-05-15T12:08:56Z",
                ),
            )
    SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True).append_event(
        idempotency_key="stale-running-progress",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key=thread_id,
        managed_thread_id=thread_id,
        repo_id="repo",
        resource_kind="worktree",
        resource_id="repo--discord-4",
        status="running",
        occurred_at="2026-05-15T12:08:56Z",
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats", params={"filter": "all", "limit": 20}
    )

    assert response.status_code == 200
    snapshot = load_read_model_contract(ChatIndexSnapshot, response.json())
    row = next(item for item in snapshot.rows if item.chat_id == thread_id)
    assert row.status == "idle"
    assert row.effective_status == "idle"
    assert row.ticket_id == "TICKET-015"
    assert row.run_id == "run-015"
    assert row.group_id == "run:run-015"
    assert row.last_activity_at == row.last_sort_activity_at
    assert row.debug is not None
    assert row.debug["activity"]["selected_source"] in {
        "last_visible_message_at",
        "last_sort_activity_at",
    }
    assert row.debug["title"]["selected"] == row.display_title

    active_response = client.get(
        "/hub/read-models/chats", params={"filter": "active", "limit": 20}
    )
    assert active_response.status_code == 200
    active_snapshot = load_read_model_contract(
        ChatIndexSnapshot, active_response.json()
    )
    assert all(item.chat_id != thread_id for item in active_snapshot.rows)


def test_chat_index_contract_prioritizes_failed_runtime_over_running_lifecycle(
    hub_env,
) -> None:
    thread_id = "thread-failed-stale-running"
    with open_orchestration_sqlite(
        hub_env.hub_root, durable=True, migrate=True
    ) as conn:
        with conn:
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
                    "repo",
                    "worktree",
                    "repo--discord-4",
                    "failed flow",
                    "active",
                    "failed",
                    "{}",
                    "2026-05-15T11:57:27Z",
                    "2026-05-15T12:08:56Z",
                ),
            )
    SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True).append_event(
        idempotency_key="failed-stale-running",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key=thread_id,
        managed_thread_id=thread_id,
        repo_id="repo",
        resource_kind="worktree",
        resource_id="repo--discord-4",
        status="running",
        occurred_at="2026-05-15T12:08:56Z",
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats", params={"filter": "all", "limit": 20}
    )

    assert response.status_code == 200
    snapshot = load_read_model_contract(ChatIndexSnapshot, response.json())
    row = next(item for item in snapshot.rows if item.chat_id == thread_id)
    assert row.status == "failed"
    assert row.effective_status == "failed"


def test_chat_detail_snapshot_contains_timeline_queue_and_cursor(hub_env) -> None:
    store = ManagedThreadStore(hub_env.hub_root, durable=True)
    thread = store.create_thread(
        "hermes",
        hub_env.repo_root,
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        name="Detail thread",
        metadata={"model": "gpt-5.5", "agent_profile": "m4-pma"},
    )
    thread_id = str(thread["managed_thread_id"])
    running = store.create_turn(thread_id, prompt="hello detail")
    store.create_turn(
        thread_id,
        prompt="queued follow-up",
        busy_policy="queue",
        force_queue=True,
    )
    SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True).append_event(
        idempotency_key="progress-1",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key=thread_id,
        managed_thread_id=thread_id,
        repo_id="repo",
        status="running",
        payload={"patch_type": "timeline_append"},
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(f"/hub/chat/threads/{thread_id}/detail")

    assert response.status_code == 200
    payload = response.json()
    assert payload["contract_version"] == "chat_detail_read.v1"
    assert payload["thread"]["managed_thread_id"] == thread_id
    assert payload["thread"]["agent_profile"] == "m4-pma"
    assert (
        payload["active_turn_status"]["managed_turn_id"] == running["managed_turn_id"]
    )
    assert payload["queue_summary"]["depth"] == 1
    assert payload["timeline"]["items"][0]["kind"] == "user_message"
    assert payload["stream"]["cursor"] >= 1

    older = client.get(
        f"/hub/chat/threads/{thread_id}/timeline/older",
        params={"before_order_key": payload["timeline"]["items"][-1]["order_key"]},
    )
    assert older.status_code == 200
    assert older.json()["contract_version"] == "chat_timeline_page.v1"


@pytest.mark.slow
@pytest.mark.timeout(180)
def test_older_timeline_page_reaches_past_detail_snapshot_cap(hub_env) -> None:
    store = ManagedThreadStore(hub_env.hub_root, durable=True)
    thread = store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        name="Long detail thread",
    )
    thread_id = str(thread["managed_thread_id"])
    for index in range(250):
        store.create_turn(
            thread_id,
            prompt=f"turn {index:03d}",
            busy_policy="queue" if index > 0 else "reject",
            force_queue=index > 0,
        )

    client = TestClient(create_hub_app(hub_env.hub_root))
    detail = client.get(
        f"/hub/chat/threads/{thread_id}/detail",
        params={"timeline_limit": 200},
    ).json()
    assert detail["timeline"]["window"]["has_older"] is True
    oldest_visible = detail["timeline"]["window"]["oldest_order_key"]

    older = client.get(
        f"/hub/chat/threads/{thread_id}/timeline/older",
        params={"before_order_key": oldest_visible, "limit": 25},
    )

    assert older.status_code == 200
    payload = older.json()
    assert payload["window"]["returned"] == 25
    assert payload["window"]["has_older"] is True
    assert payload["items"][-1]["order_key"] < oldest_visible


def test_chat_patch_stream_replays_cursor_ordered_patches(hub_env) -> None:
    journal = SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True)
    first = journal.append_event(
        idempotency_key="patch-1",
        event_type="queue.state_changed",
        surface_kind="pma",
        surface_key="thread-1",
        managed_thread_id="thread-1",
        status="queued",
    ).event
    second = journal.append_event(
        idempotency_key="patch-2",
        event_type="delivery.status_changed",
        surface_kind="discord",
        surface_key="guild:channel",
        managed_thread_id="thread-1",
        status="delivered",
    ).event

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/chat/patches",
        params={"cursor": str(first.cursor), "once": "true"},
    )

    assert response.status_code == 200
    patches = _event_payloads(response.text, "chat.patch")
    assert [patch["cursor"] for patch in patches] == [second.cursor]
    assert patches[0]["patch_type"] == "delivery_lifecycle_change"


def test_hub_read_models_chats_returns_web_contract_snapshot(hub_env) -> None:
    _seed_thread_rows(hub_env.hub_root, 30)
    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats", params={"filter": "all", "limit": 10}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["contractVersion"] == "web-read-models.v1"
    assert body["kind"] == "chat.index.snapshot"
    assert body["repair"]["snapshotRoute"] == "/hub/read-models/chats"
    assert "chatId" in body["rows"][0]
    snapshot = load_read_model_contract(ChatIndexSnapshot, body)
    assert snapshot.rows[0].chat_id.startswith("thread-")
    assert snapshot.rows[0].effective_status == snapshot.rows[0].status
    assert len(snapshot.rows) == 10


def test_hub_read_models_chats_returns_backend_facets_and_counts(hub_env) -> None:
    _insert_chat_thread_row(
        hub_env.hub_root,
        thread_id="route-regular",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        display_name="Route regular",
        runtime_status="idle",
    )
    _insert_chat_thread_row(
        hub_env.hub_root,
        thread_id="route-automation",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        display_name="Route automation",
        runtime_status="idle",
        metadata={"automation_job_id": "job-route", "automation_rule_id": "rule-route"},
        updated_at="2026-05-11T00:01:00Z",
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats",
        params=[("filter", "all"), ("category", "automation"), ("limit", "20")],
    )

    assert response.status_code == 200
    snapshot = load_read_model_contract(ChatIndexSnapshot, response.json())
    assert [row.chat_id for row in snapshot.rows] == ["route-automation"]
    assert snapshot.rows[0].facets is not None
    assert snapshot.rows[0].facets.category == "automation"
    assert snapshot.facet_counts.category["automation"] == 1
    assert snapshot.facet_counts.category.get("regular", 0) == 0
    assert snapshot.facet_request.categories == ["automation"]


def test_hub_read_models_chats_transport_counts_are_external_channels(
    hub_env,
) -> None:
    for index in range(5):
        _insert_chat_thread_row(
            hub_env.hub_root,
            thread_id=f"route-pma-{index}",
            repo_id="repo",
            resource_kind="repo",
            resource_id="repo",
            display_name=f"Route PMA {index}",
            runtime_status="idle",
            updated_at=f"2026-05-11T00:0{index}:00Z",
        )
    _insert_chat_thread_row(
        hub_env.hub_root,
        thread_id="route-discord",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        display_name="Route Discord",
        runtime_status="idle",
        updated_at="2026-05-10T00:00:00Z",
    )
    OrchestrationBindingStore(hub_env.hub_root, durable=True).upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="route-discord",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats",
        params={"filter": "all", "limit": "1"},
    )

    assert response.status_code == 200
    snapshot = load_read_model_contract(ChatIndexSnapshot, response.json())
    assert snapshot.facet_counts.transport == {"discord": 1}

    discord_response = client.get(
        "/hub/read-models/chats",
        params=[("filter", "all"), ("transport", "discord"), ("limit", "1")],
    )

    assert discord_response.status_code == 200
    discord_snapshot = load_read_model_contract(
        ChatIndexSnapshot, discord_response.json()
    )
    assert [row.chat_id for row in discord_snapshot.rows] == ["route-discord"]
    assert discord_snapshot.rows[0].facets is not None
    assert "discord" in discord_snapshot.rows[0].facets.transports
    assert "pma" not in discord_snapshot.facet_counts.transport


def test_chat_index_contract_status_matches_backend_effective_statuses(
    hub_env,
) -> None:
    for thread_id, runtime_status in (
        ("thread-queued-contract", "idle"),
        ("thread-running-contract", "running"),
        ("thread-success-contract", "completed"),
        ("thread-failed-contract", "failed"),
        ("thread-archived-contract", "completed"),
    ):
        _insert_thread_row(
            hub_env.hub_root,
            thread_id=thread_id,
            display_name=thread_id,
            lifecycle_status=(
                "archived" if thread_id == "thread-archived-contract" else "active"
            ),
            runtime_status=runtime_status,
        )
    SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True).append_event(
        idempotency_key="queued-contract",
        event_type="queue.state_changed",
        surface_kind="pma",
        surface_key="thread-queued-contract",
        managed_thread_id="thread-queued-contract",
        repo_id="repo",
        status="queued",
    )
    with open_orchestration_sqlite(
        hub_env.hub_root, durable=True, migrate=True
    ) as conn:
        with conn:
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
                    "exec-queued-contract",
                    "thread-queued-contract",
                    "message",
                    "queued",
                    "2026-05-11T01:00:00Z",
                ),
            )
    PmaNotificationStore(hub_env.hub_root).record_notification(
        correlation_id="notification-contract",
        source_kind="pma",
        delivery_mode="direct",
        surface_kind="discord",
        surface_key="guild:alerts",
        delivery_record_id="delivery-notification-contract",
        repo_id="repo",
        notification_id="notification-contract",
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats", params={"filter": "all", "limit": 20}
    )
    response.raise_for_status()
    snapshot = load_read_model_contract(
        ChatIndexSnapshot,
        response.json(),
    )
    rows = {row.chat_id: row for row in snapshot.rows}

    expected = {
        "thread-queued-contract": "waiting",
        "thread-running-contract": "running",
        "thread-success-contract": "idle",
        "thread-failed-contract": "failed",
    }
    for chat_id, status in expected.items():
        assert rows[chat_id].effective_status == status
        assert rows[chat_id].status == status

    archived_response = client.get(
        "/hub/read-models/chats", params={"filter": "archived", "limit": 20}
    )
    archived_response.raise_for_status()
    archived_snapshot = load_read_model_contract(
        ChatIndexSnapshot,
        archived_response.json(),
    )
    archived_rows = {row.chat_id: row for row in archived_snapshot.rows}
    assert archived_rows["thread-archived-contract"].effective_status == "archived"
    assert archived_rows["thread-archived-contract"].status == "archived"

    external_response = client.get(
        "/hub/read-models/chats", params={"filter": "external", "limit": 20}
    )
    external_response.raise_for_status()
    external_snapshot = load_read_model_contract(
        ChatIndexSnapshot,
        external_response.json(),
    )
    notification_row = next(
        row for row in external_snapshot.rows if row.chat_id.startswith("surface:")
    )
    assert notification_row.effective_status == "idle"
    assert notification_row.status == "idle"


def test_legacy_archived_chat_row_overrides_terminal_status(hub_env) -> None:
    _insert_thread_row(
        hub_env.hub_root,
        thread_id="thread-legacy-archived-completed",
        display_name="legacy archived completed",
        lifecycle_status="archived",
        runtime_status="completed",
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    active_response = client.get(
        "/hub/read-models/chats", params={"filter": "all", "limit": 20}
    )
    active_response.raise_for_status()
    active_snapshot = load_read_model_contract(
        ChatIndexSnapshot,
        active_response.json(),
    )
    assert "thread-legacy-archived-completed" not in {
        row.chat_id for row in active_snapshot.rows
    }

    archived_response = client.get(
        "/hub/read-models/chats", params={"filter": "archived", "limit": 20}
    )
    archived_response.raise_for_status()
    archived_snapshot = load_read_model_contract(
        ChatIndexSnapshot,
        archived_response.json(),
    )
    rows = {row.chat_id: row for row in archived_snapshot.rows}
    assert rows["thread-legacy-archived-completed"].effective_status == "archived"


def test_hub_read_models_chats_counters_cover_full_filtered_set(hub_env) -> None:
    _seed_thread_rows(hub_env.hub_root, 30)
    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats", params={"filter": "all", "limit": 1}
    )

    assert response.status_code == 200
    snapshot = load_read_model_contract(ChatIndexSnapshot, response.json())
    assert len(snapshot.rows) == 1
    assert snapshot.counters.total == 29
    assert snapshot.counters.running == 1
    assert snapshot.rows[0].status != "running"


def test_hub_read_models_chats_all_excludes_effectively_archived_rows(
    hub_env,
) -> None:
    with open_orchestration_sqlite(
        hub_env.hub_root, durable=True, migrate=True
    ) as conn:
        with conn:
            conn.executemany(
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
                    (
                        "thread-active",
                        "codex",
                        "repo",
                        "repo",
                        "repo",
                        "Active thread",
                        "active",
                        "idle",
                        "{}",
                        "2026-05-11T00:00:00Z",
                        "2026-05-11T00:02:00Z",
                    ),
                    (
                        "thread-stale-archived-runtime",
                        "codex",
                        "repo",
                        "repo",
                        "repo",
                        "Stale archived runtime",
                        "archived",
                        "completed",
                        "{}",
                        "2026-05-11T00:00:00Z",
                        "2026-05-11T00:01:00Z",
                    ),
                ),
            )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats", params={"filter": "all", "limit": 20}
    )

    assert response.status_code == 200
    snapshot = load_read_model_contract(ChatIndexSnapshot, response.json())
    assert [row.chat_id for row in snapshot.rows] == ["thread-active"]
    assert snapshot.counters.total == 1
    assert snapshot.counters.archived == 1

    archived_response = client.get(
        "/hub/read-models/chats", params={"filter": "archived", "limit": 20}
    )
    assert archived_response.status_code == 200
    archived_snapshot = load_read_model_contract(
        ChatIndexSnapshot, archived_response.json()
    )
    assert [row.chat_id for row in archived_snapshot.rows] == [
        "thread-stale-archived-runtime"
    ]


def test_hub_read_models_chats_discord_rebind_uses_managed_archive_state(
    hub_env,
) -> None:
    with open_orchestration_sqlite(
        hub_env.hub_root, durable=True, migrate=True
    ) as conn:
        with conn:
            conn.executemany(
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
                    (
                        "thread-discord-old",
                        "codex",
                        "repo",
                        "repo",
                        "repo",
                        "Old Discord generation",
                        "archived",
                        "completed",
                        "{}",
                        "2026-05-11T00:00:00Z",
                        "2026-05-11T00:01:00Z",
                    ),
                    (
                        "thread-discord-new",
                        "codex",
                        "repo",
                        "repo",
                        "repo",
                        "New Discord generation",
                        "active",
                        "running",
                        "{}",
                        "2026-05-11T00:02:00Z",
                        "2026-05-11T00:03:00Z",
                    ),
                ),
            )
    bindings = OrchestrationBindingStore(hub_env.hub_root, durable=True)
    first = bindings.upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="thread-discord-old",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        metadata={"display_name": "Discord channel"},
    )
    bindings.disable_binding(binding_id=first.binding_id)
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="thread-discord-new",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        metadata={"display_name": "Discord channel"},
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats",
        params={"filter": "all", "surface_kind": "discord", "limit": 20},
    )

    assert response.status_code == 200
    snapshot = load_read_model_contract(ChatIndexSnapshot, response.json())
    assert [row.chat_id for row in snapshot.rows] == ["thread-discord-new"]
    assert snapshot.rows[0].archive_state == "active"
    assert snapshot.rows[0].status == "running"
    assert snapshot.counters.total == 1
    assert snapshot.counters.archived == 0

    archived_response = client.get(
        "/hub/read-models/chats", params={"filter": "archived", "limit": 20}
    )
    assert archived_response.status_code == 200
    archived_snapshot = load_read_model_contract(
        ChatIndexSnapshot, archived_response.json()
    )
    assert [row.chat_id for row in archived_snapshot.rows] == ["thread-discord-old"]


@pytest.mark.parametrize(
    ("surface_kind", "surface_key", "display_name"),
    [
        ("discord", "guild:channel", "Discord channel"),
        ("telegram", "-1001:77", "Telegram topic"),
    ],
)
def test_hub_read_models_chats_bound_newt_rebind_moves_active_generation(
    hub_env,
    surface_kind: str,
    surface_key: str,
    display_name: str,
) -> None:
    old_thread_id = f"thread-{surface_kind}-old-generation"
    new_thread_id = f"thread-{surface_kind}-new-generation"
    _insert_thread_row(
        hub_env.hub_root,
        thread_id=old_thread_id,
        display_name=f"Old {display_name}",
        updated_at="2026-05-11T00:01:00Z",
    )
    bindings = OrchestrationBindingStore(hub_env.hub_root, durable=True)
    bindings.upsert_binding(
        surface_kind=surface_kind,
        surface_key=surface_key,
        thread_target_id=old_thread_id,
        agent_id="codex",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        metadata={"display_name": display_name},
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    initial = client.get(
        "/hub/read-models/chats",
        params={"filter": "all", "surface_kind": surface_kind, "limit": 20},
    )
    assert initial.status_code == 200
    initial_snapshot = load_read_model_contract(ChatIndexSnapshot, initial.json())
    assert [row.chat_id for row in initial_snapshot.rows] == [old_thread_id]

    _archive_thread_row(
        hub_env.hub_root,
        thread_id=old_thread_id,
        updated_at="2026-05-11T00:02:00Z",
    )
    SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True).append_event(
        idempotency_key=f"archive-old-{surface_kind}-generation",
        event_type="surface.archived",
        surface_kind=surface_kind,
        surface_key=surface_key,
        managed_thread_id=old_thread_id,
        repo_id="repo",
        lifecycle_status="archived",
        status="archived",
        occurred_at="2026-05-11T00:02:00Z",
    )
    archived_before_newt = client.get(
        "/hub/read-models/chats",
        params={"filter": "archived", "limit": 20},
    )
    assert archived_before_newt.status_code == 200
    archived_before_snapshot = load_read_model_contract(
        ChatIndexSnapshot, archived_before_newt.json()
    )
    patch_cursor = archived_before_snapshot.cursor.sequence
    assert [row.chat_id for row in archived_before_snapshot.rows] == [old_thread_id]

    _insert_thread_row(
        hub_env.hub_root,
        thread_id=new_thread_id,
        display_name=f"New {display_name}",
        lifecycle_status="active",
        runtime_status="running",
        updated_at="2026-05-11T00:03:00Z",
    )
    bindings.upsert_binding(
        surface_kind=surface_kind,
        surface_key=surface_key,
        thread_target_id=new_thread_id,
        agent_id="codex",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        metadata={"display_name": display_name},
    )

    patch_response = client.get(
        "/hub/read-models/chats/patches",
        params={
            "cursor": str(patch_cursor),
            "filter": "all",
            "surface_kind": surface_kind,
            "once": "true",
        },
    )
    assert patch_response.status_code == 200
    repairs = _event_payloads(patch_response.text, "projection.cursor_gap")
    assert len(repairs) == 1
    assert repairs[0]["envelope"]["operation"] == "invalidate"
    assert repairs[0]["patch"]["counters"]["total"] == 1
    assert repairs[0]["patch"]["counters"]["archived"] == 0

    all_response = client.get(
        "/hub/read-models/chats", params={"filter": "all", "limit": 20}
    )
    surface_response = client.get(
        "/hub/read-models/chats",
        params={"filter": "all", "surface_kind": surface_kind, "limit": 20},
    )
    archived_response = client.get(
        "/hub/read-models/chats", params={"filter": "archived", "limit": 20}
    )
    old_detail = client.get(f"/hub/read-models/chats/{old_thread_id}")

    assert all_response.status_code == 200
    assert surface_response.status_code == 200
    assert archived_response.status_code == 200
    assert old_detail.status_code == 200

    all_snapshot = load_read_model_contract(ChatIndexSnapshot, all_response.json())
    surface_snapshot = load_read_model_contract(
        ChatIndexSnapshot, surface_response.json()
    )
    archived_snapshot = load_read_model_contract(
        ChatIndexSnapshot, archived_response.json()
    )
    old_detail_snapshot = load_read_model_contract(
        ChatDetailSnapshot, old_detail.json()
    )

    assert new_thread_id in [row.chat_id for row in all_snapshot.rows]
    assert [row.chat_id for row in surface_snapshot.rows] == [new_thread_id]
    assert surface_snapshot.rows[0].surface == surface_kind
    assert surface_snapshot.rows[0].archive_state == "active"
    assert surface_snapshot.rows[0].status == "running"
    assert [row.chat_id for row in archived_snapshot.rows] == [old_thread_id]
    assert archived_snapshot.rows[0].archive_state == "archived"
    assert old_detail_snapshot.thread.chat_id == old_thread_id
    assert old_detail_snapshot.thread.archived is True


def test_hub_read_models_chats_contract_active_filter_matches_index_window(
    hub_env,
) -> None:
    _seed_thread_rows(hub_env.hub_root, 1000)
    OrchestrationBindingStore(hub_env.hub_root, durable=True).upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="thread-0003",
        repo_id="repo",
        resource_kind="ticket",
        resource_id="TICKET-900",
        metadata={"display_name": "Discord channel"},
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats", params={"filter": "active", "limit": 25}
    )
    assert response.status_code == 200
    snapshot = load_read_model_contract(ChatIndexSnapshot, response.json())
    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].chat_id == "thread-0003"
    assert snapshot.rows[0].agent_profile == "m4-pma"
    assert snapshot.rows[0].chat_kind == "coding_agent"
    assert snapshot.rows[0].surface == "discord"


def test_hub_read_models_chats_ticket_flow_group_regression_from_ticket_files(
    hub_env,
) -> None:
    repo_root = hub_env.hub_root / "repos" / "smoke-repo"
    worktree_id = "smoke-repo--ticket-flow"
    run_id = "run-ticket-regression"
    ticket_paths = [
        f".codex-autorunner/tickets/TICKET-{index:03d}.md" for index in range(1, 6)
    ]
    for index, ticket_path in enumerate(ticket_paths, start=1):
        _write_ticket(repo_root / ticket_path, done=index <= 3)
        _insert_chat_thread_row(
            hub_env.hub_root,
            thread_id=f"ticket-flow-child-{index}",
            repo_id="smoke-repo",
            resource_kind="worktree",
            resource_id=worktree_id,
            display_name=f"Ticket flow child {index}",
            runtime_status="completed" if index <= 3 else "running",
            metadata=ticket_flow_thread_metadata(
                flow_run_id=run_id,
                ticket_id=f"TICKET-{index:03d}",
                workspace_root=str(repo_root),
                ticket_path=ticket_path,
            ),
            updated_at=f"2026-05-11T00:0{index}:00Z",
        )

    _insert_chat_thread_row(
        hub_env.hub_root,
        thread_id="generic-completed-same-worktree",
        repo_id="smoke-repo",
        resource_kind="worktree",
        resource_id=worktree_id,
        display_name="Generic completed same worktree",
        runtime_status="completed",
        updated_at="2026-05-11T00:06:00Z",
    )
    _insert_chat_thread_row(
        hub_env.hub_root,
        thread_id="ticketish-title-no-flow",
        repo_id="smoke-repo",
        resource_kind="worktree",
        resource_id=worktree_id,
        display_name="TICKET-999 looks like ticket flow",
        runtime_status="running",
        metadata={"title": "ticket_flow run lookalike"},
        updated_at="2026-05-11T00:07:00Z",
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    active_response = client.get(
        "/hub/read-models/chats",
        params={"filter": "active", "group_by": "ticket_run", "limit": 20},
    )
    ticket_runs_response = client.get(
        "/hub/read-models/chats",
        params={"filter": "ticket_runs", "group_by": "ticket_run", "limit": 20},
    )
    child_response = client.get(
        "/hub/read-models/chats",
        params={
            "filter": "ticket_runs",
            "group_by": "ticket_run",
            "parent_group_id": f"run:{run_id}",
            "limit": 20,
        },
    )

    assert active_response.status_code == 200
    assert ticket_runs_response.status_code == 200
    assert child_response.status_code == 200
    active_snapshot = load_read_model_contract(
        ChatIndexSnapshot, active_response.json()
    )
    ticket_runs_snapshot = load_read_model_contract(
        ChatIndexSnapshot, ticket_runs_response.json()
    )
    child_snapshot = load_read_model_contract(ChatIndexSnapshot, child_response.json())

    assert [group.group_id for group in active_snapshot.groups] == [f"run:{run_id}"]
    assert [group.group_id for group in ticket_runs_snapshot.groups] == [
        f"run:{run_id}"
    ]
    group = ticket_runs_snapshot.groups[0]
    assert group.kind == "ticket_run_group"
    assert group.run_id == run_id
    assert group.scope_kind == "worktree"
    assert group.scope_id == worktree_id
    assert group.status == "running"
    assert group.total_count == 5
    assert group.done_count == 3
    assert group.running_count == 2
    assert group.waiting_count == 0
    assert group.failed_count == 0

    children = {row.chat_id: row for row in child_snapshot.rows}
    assert set(children) == {f"ticket-flow-child-{index}" for index in range(1, 6)}
    assert "generic-completed-same-worktree" not in children
    assert "ticketish-title-no-flow" not in children
    for index in range(1, 6):
        row = children[f"ticket-flow-child-{index}"]
        assert row.flow_type == "ticket_flow"
        assert row.run_id == run_id
        assert row.ticket_path == ticket_paths[index - 1]
        assert row.ticket_done == (index <= 3)
        assert row.ticket_status == ("done" if index <= 3 else "running")


def test_hub_read_models_chats_derives_rows_before_window_limit(hub_env) -> None:
    _seed_thread_rows(hub_env.hub_root, 1200)

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats", params={"filter": "all", "limit": 200}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["window"]["totalEstimate"] == 1199
    assert len(body["rows"]) == 200


def test_hub_read_models_chats_includes_binding_display_contract_fields(
    hub_env,
) -> None:
    _seed_thread_rows(hub_env.hub_root, 4)
    OrchestrationBindingStore(hub_env.hub_root, durable=True).upsert_binding(
        surface_kind="telegram",
        surface_key="chat-42",
        thread_target_id="thread-0003",
        repo_id="repo",
        resource_kind="ticket",
        resource_id="TICKET-900",
        metadata={"display_name": "Release room"},
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats", params={"filter": "active", "limit": 25}
    )

    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert row["chatId"] == "thread-0003"
    assert row["displayTitle"] == "Thread 0003"
    assert row["technicalTitle"] == "thread-0003"
    assert row["primarySurface"]["surface_kind"] == "pma"
    assert "Release room" in row["bindingDisplayNames"]
    assert row["archiveState"] == "active"
    assert row["resourceKind"] == "ticket"
    assert row["sortKey"]["row_id"] == "thread:thread-0003"


def test_hub_read_models_chats_patch_stream_uses_projection_revisions(hub_env) -> None:
    _seed_thread_rows(hub_env.hub_root, 2)
    journal = SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True)
    journal.append_event(
        idempotency_key="index-patch-1",
        event_type="queue.state_changed",
        surface_kind="pma",
        surface_key="thread-0000",
        managed_thread_id="thread-0000",
        repo_id="repo",
        status="queued",
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    snapshot = client.get("/hub/read-models/chats").json()
    snapshot_cursor = snapshot["cursor"]["sequence"]

    journal.append_event(
        idempotency_key="index-patch-2",
        event_type="delivery.status_changed",
        surface_kind="discord",
        surface_key="guild:channel",
        managed_thread_id="thread-0001",
        repo_id="repo",
        status="delivered",
        payload={"display": {"display_name": "Ops channel"}},
    )

    response = client.get(
        "/hub/read-models/chats/patches",
        params={"cursor": str(snapshot_cursor), "once": "true"},
    )

    assert response.status_code == 200
    repairs = _event_payloads(response.text, "projection.cursor_gap")
    assert len(repairs) == 1
    assert repairs[0]["envelope"]["cursor"]["sequence"] == snapshot_cursor + 1
    assert repairs[0]["envelope"]["eventType"] == "projection.cursor_gap"
    assert repairs[0]["envelope"]["entityKind"] == "chat"
    assert repairs[0]["envelope"]["entityId"] == "chat.index"
    assert repairs[0]["envelope"]["operation"] == "invalidate"
    assert repairs[0]["repair"]["requestedCursor"] == snapshot_cursor
    assert repairs[0]["repair"]["snapshotRoute"] == "/hub/read-models/chats"
    assert repairs[0]["patch"]["rows"] == []
    assert repairs[0]["patch"]["counters"]["total"] == 2


def test_hub_read_models_chats_patch_stream_does_not_replay_historical_events(
    hub_env,
) -> None:
    _seed_thread_rows(hub_env.hub_root, 1)
    _seed_archived_surface_events(hub_env.hub_root, 1200, prefix="historical-archive")

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats/patches",
        params={"cursor": "0", "once": "true", "event_limit": 1000},
    )

    assert response.status_code == 200
    assert _event_payloads(response.text, "chat.index.patch") == []
    repairs = _event_payloads(response.text, "projection.cursor_gap")
    assert len(repairs) == 1
    assert repairs[0]["envelope"]["operation"] == "invalidate"
    assert repairs[0]["envelope"]["cursor"]["sequence"] > 0


def test_hub_read_models_chats_patch_stream_bulk_archive_is_bounded(
    hub_env,
) -> None:
    _seed_thread_rows(hub_env.hub_root, 300)
    client = TestClient(create_hub_app(hub_env.hub_root))
    snapshot = client.get("/hub/read-models/chats").json()
    snapshot_cursor = snapshot["cursor"]["sequence"]

    journal = SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True)
    for index in range(300):
        journal.append_event(
            idempotency_key=f"bulk-archive-{index}",
            event_type="surface.archived",
            surface_kind="pma",
            surface_key=f"thread-{index:04d}",
            managed_thread_id=f"thread-{index:04d}",
            repo_id="repo",
            lifecycle_status="archived",
            status="archived",
        )

    response = client.get(
        "/hub/read-models/chats/patches",
        params={"cursor": str(snapshot_cursor), "once": "true", "event_limit": 1000},
    )

    assert response.status_code == 200
    assert _event_payloads(response.text, "chat.index.patch") == []
    repairs = _event_payloads(response.text, "projection.cursor_gap")
    assert len(repairs) == 1
    assert repairs[0]["envelope"]["cursor"]["sequence"] == snapshot_cursor + 1
    assert repairs[0]["patch"]["counters"]["archived"] >= 300


def test_hub_read_models_chats_patch_stream_repairs_future_cursor(hub_env) -> None:
    _seed_thread_rows(hub_env.hub_root, 1)
    SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True).append_event(
        idempotency_key="index-gap-1",
        event_type="surface.bound",
        surface_kind="pma",
        surface_key="thread-0000",
        managed_thread_id="thread-0000",
        repo_id="repo",
        status="bound",
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats/patches",
        params={"cursor": "999", "once": "true"},
    )

    assert response.status_code == 200
    repairs = _event_payloads(response.text, "projection.cursor_gap")
    assert repairs[0]["envelope"]["operation"] == "invalidate"
    assert repairs[0]["envelope"]["eventType"] == "projection.cursor_gap"
    assert repairs[0]["envelope"]["entityId"] == "chat.index"
    assert repairs[0]["repair"]["requestedCursor"] == 999
    assert repairs[0]["repair"]["snapshotRoute"] == "/hub/read-models/chats"


def test_hub_read_models_chats_patch_stream_repairs_future_cursor_with_empty_journal(
    hub_env,
) -> None:
    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/chats/patches",
        params={"cursor": "999", "once": "true"},
    )

    assert response.status_code == 200
    repairs = _event_payloads(response.text, "projection.cursor_gap")
    assert len(repairs) == 1
    assert repairs[0]["envelope"]["operation"] == "invalidate"
    assert repairs[0]["patch"]["rows"] == []


def test_hub_read_models_chat_detail_contract_snapshot(hub_env) -> None:
    store = ManagedThreadStore(hub_env.hub_root, durable=True)
    thread = store.create_thread(
        "hermes",
        hub_env.repo_root,
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        name="Detail thread",
        metadata={"model": "gpt-5.5", "agent_profile": "m4-pma"},
    )
    thread_id = str(thread["managed_thread_id"])
    running = store.create_turn(thread_id, prompt="hello detail")
    store.create_turn(
        thread_id,
        prompt="queued follow-up",
        busy_policy="queue",
        force_queue=True,
    )
    SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True).append_event(
        idempotency_key="progress-contract-1",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key=thread_id,
        managed_thread_id=thread_id,
        repo_id="repo",
        status="running",
        payload={"patch_type": "timeline_append"},
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(f"/hub/read-models/chats/{thread_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["contractVersion"] == "web-read-models.v1"
    assert body["kind"] == "chat.detail.snapshot"
    assert body["repair"]["snapshotRoute"] == f"/hub/read-models/chats/{thread_id}"
    snapshot = load_read_model_contract(ChatDetailSnapshot, body)
    assert snapshot.thread.chat_id == thread_id
    assert snapshot.thread.agent_profile == "m4-pma"
    assert snapshot.queue.active_turn_id == running["managed_turn_id"]
    assert snapshot.queue.depth == 1
    assert snapshot.timeline[0].kind == "user_message"
    assert snapshot.timeline[0].managed_turn_id == running["managed_turn_id"]
    assert snapshot.timeline[0].section == "user_message"
    assert snapshot.timeline[0].section_order == 10
    assert snapshot.timeline[0].order_key
    assert snapshot.timeline[0].text == "hello detail"
    assert body["timeline"][0]["managedTurnId"] == running["managed_turn_id"]
    assert body["timeline"][0]["section"] == "user_message"
    assert body["timeline"][0]["sectionOrder"] == 10


def test_hub_read_models_chat_detail_patch_stream_is_repairable(hub_env) -> None:
    store = ManagedThreadStore(hub_env.hub_root, durable=True)
    thread = store.create_thread(
        "hermes",
        hub_env.repo_root,
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        name="Detail patch thread",
    )
    thread_id = str(thread["managed_thread_id"])
    SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True).append_event(
        idempotency_key="detail-patch-contract-1",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key=thread_id,
        managed_thread_id=thread_id,
        repo_id="repo",
        status="running",
        payload={"patch_type": "timeline_append"},
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        f"/hub/read-models/chats/{thread_id}/patches",
        params={"once": "true"},
    )

    assert response.status_code == 200
    body = response.text
    assert "event: chat.detail.patch" in body
    assert '"contractVersion": "web-read-models.v1"' in body
    assert '"eventType": "chat.detail.patch"' in body
    assert '"entityId": "' + thread_id + '"' in body
    assert '"operation": "reset"' in body
    assert '"appendedTimeline": []' in body


def test_hub_read_models_chat_detail_patch_cursor_skips_unrelated_batches(
    hub_env,
) -> None:
    store = ManagedThreadStore(hub_env.hub_root, durable=True)
    target = store.create_thread(
        "hermes",
        hub_env.repo_root,
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        name="Target detail patch thread",
    )
    other = store.create_thread(
        "hermes",
        hub_env.repo_root,
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        name="Other detail patch thread",
    )
    target_id = str(target["managed_thread_id"])
    other_id = str(other["managed_thread_id"])
    service = hub_chat_read_models.ChatReadModelService(hub_env.hub_root)
    baseline = service.chat_detail_patch_contracts(target_id, None, event_limit=100)
    baseline_cursor = int(baseline["cursor"] or 0)
    journal = SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True)
    journal.append_event(
        idempotency_key="detail-patch-other-first",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key=other_id,
        managed_thread_id=other_id,
        repo_id="repo",
        status="running",
        payload={"patch_type": "timeline_append"},
    )
    journal.append_event(
        idempotency_key="detail-patch-target-second",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key=target_id,
        managed_thread_id=target_id,
        repo_id="repo",
        status="running",
        payload={"patch_type": "timeline_append"},
    )

    first_batch = service.chat_detail_patch_contracts(
        target_id, baseline_cursor, event_limit=1
    )
    assert first_batch["events"] == []
    first_cursor = int(first_batch["cursor"])
    assert first_cursor > 0

    next_cursor = hub_chat_read_models._advance_chat_detail_stream_cursor(
        0, first_cursor
    )
    second_batch = service.chat_detail_patch_contracts(
        target_id, next_cursor, event_limit=1
    )

    assert len(second_batch["events"]) == 1
    assert second_batch["events"][0]["envelope"]["entityId"] == target_id
