from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.core.orchestration import SQLiteChatSurfaceEventJournal
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.server import create_hub_app


def _seed_thread(hub_root: Path) -> None:
    with open_orchestration_sqlite(hub_root, durable=True, migrate=True) as conn:
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
                "thread-1",
                "codex",
                "repo",
                "repo",
                "repo",
                "PMA Thread",
                "active",
                "idle",
                "2026-05-11T00:00:00Z",
                "2026-05-11T00:00:01Z",
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


def test_hub_chat_events_streams_snapshot_and_incremental_events(hub_env) -> None:
    _seed_thread(hub_env.hub_root)
    journal = SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True)
    first = journal.append_event(
        idempotency_key="event-1",
        event_type="surface.bound",
        surface_kind="web",
        surface_key="chat-1",
        managed_thread_id="thread-1",
        repo_id="repo",
        status="bound",
    ).event
    second = journal.append_event(
        idempotency_key="event-2",
        event_type="queue.state_changed",
        surface_kind="web",
        surface_key="chat-1",
        managed_thread_id="thread-1",
        repo_id="repo",
        status="queued",
    ).event

    client = TestClient(create_hub_app(hub_env.hub_root))
    resp = client.get(
        "/hub/chat/events",
        params={"cursor": str(first.cursor), "once": "true"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    snapshots = _event_payloads(resp.text, "chat.snapshot")
    events = _event_payloads(resp.text, "chat.event")
    assert snapshots[0]["contract_version"] == "chat_surface_read.v1"
    assert ("web", "chat-1") in {
        (surface["surface_kind"], surface["surface_key"])
        for surface in snapshots[0]["surfaces"]
    }
    assert [event["cursor"] for event in events] == [second.cursor]
    assert events[0]["lifecycle"] == "queued"


def test_hub_chat_events_accepts_last_event_id_cursor(hub_env) -> None:
    journal = SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True)
    first = journal.append_event(
        idempotency_key="event-1",
        event_type="surface.bound",
        surface_kind="discord",
        surface_key="channel-1",
        status="bound",
    ).event
    second = journal.append_event(
        idempotency_key="event-2",
        event_type="delivery.status_changed",
        surface_kind="discord",
        surface_key="channel-1",
        status="delivered",
    ).event

    client = TestClient(create_hub_app(hub_env.hub_root))
    resp = client.get(
        "/hub/chat/events?once=true",
        headers={"Last-Event-ID": str(first.cursor)},
    )

    assert resp.status_code == 200
    events = _event_payloads(resp.text, "chat.event")
    assert [event["cursor"] for event in events] == [second.cursor]


def test_hub_chat_events_rejects_malformed_cursor(hub_env) -> None:
    client = TestClient(create_hub_app(hub_env.hub_root))

    resp = client.get("/hub/chat/events", params={"cursor": "not-a-cursor"})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "cursor must be a non-negative integer"


def test_hub_chat_events_can_emit_heartbeat_frame(hub_env) -> None:
    client = TestClient(create_hub_app(hub_env.hub_root))

    resp = client.get(
        "/hub/chat/events",
        params={"once": "true", "include_heartbeat": "true"},
    )

    assert resp.status_code == 200
    assert "event: chat.snapshot" in resp.text
    assert ": keep-alive\n\n" in resp.text
