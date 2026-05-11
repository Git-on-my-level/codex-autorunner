from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import (
    OrchestrationBindingStore,
    SQLiteChatSurfaceEventJournal,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.server import create_hub_app


def _seed_thread_rows(hub_root: Path, count: int) -> None:
    with open_orchestration_sqlite(hub_root, durable=True, migrate=True) as conn:
        with conn:
            for index in range(count):
                status = "running" if index == 3 else "idle"
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
                        "codex",
                        "repo",
                        resource_kind,
                        resource_id,
                        f"Thread {index:04d}",
                        lifecycle,
                        status,
                        json.dumps({"model": "gpt-5.5"}),
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
    assert "discord" in payload["rows"][0]["surface_kinds"]
    assert payload["window"]["total_count"] == 1

    grouped = client.get(
        "/hub/chat/index",
        params={"group_by": "ticket_run", "view": "ticket_run", "limit": 10},
    ).json()
    assert grouped["rows"][0]["row_type"] == "group"
    assert grouped["rows"][0]["group_id"] == "ticket:TICKET-900"
    assert grouped["rows"][0]["child_count"] == 11

    children = client.get(
        "/hub/chat/index",
        params={
            "group_by": "ticket_run",
            "parent_group_id": "ticket:TICKET-900",
            "limit": 5,
        },
    ).json()
    assert children["window"]["returned"] == 5
    assert children["window"]["has_more"] is True
    assert {row["group_id"] for row in children["rows"]} == {"ticket:TICKET-900"}


def test_chat_detail_snapshot_contains_timeline_queue_and_cursor(hub_env) -> None:
    store = ManagedThreadStore(hub_env.hub_root, durable=True)
    thread = store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        name="Detail thread",
        metadata={"model": "gpt-5.5"},
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
