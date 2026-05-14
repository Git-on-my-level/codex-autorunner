from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from codex_autorunner.adapters.chat.channel_directory import ChannelDirectoryStore
from codex_autorunner.core.orchestration import (
    ChatSurfaceReadService,
    OrchestrationBindingStore,
    SQLiteChatSurfaceEventJournal,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_notification_store import PmaNotificationStore
from codex_autorunner.server import create_hub_app


def _seed_thread(
    hub_root: Path,
    *,
    thread_id: str,
    lifecycle_status: str = "active",
    runtime_status: str = "idle",
    repo_id: str = "repo",
    display_name: str | None = None,
) -> None:
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
                thread_id,
                "codex",
                repo_id,
                "repo",
                repo_id,
                display_name or f"Thread {thread_id}",
                lifecycle_status,
                runtime_status,
                "2026-05-11T00:00:00Z",
                "2026-05-11T00:00:10Z",
            ),
        )


def _seed_execution(
    hub_root: Path,
    *,
    thread_id: str,
    status: str,
    execution_id: str | None = None,
) -> None:
    with open_orchestration_sqlite(hub_root, durable=True, migrate=True) as conn:
        conn.execute(
            """
            INSERT INTO orch_thread_executions (
                execution_id,
                thread_target_id,
                request_kind,
                status,
                created_at,
                started_at,
                finished_at,
                error_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id or f"exec-{thread_id}-{status}",
                thread_id,
                "message",
                status,
                "2026-05-11T00:01:00Z",
                "2026-05-11T00:01:01Z" if status == "running" else None,
                "2026-05-11T00:02:00Z" if status in {"completed", "failed"} else None,
                "turn failed" if status == "failed" else None,
            ),
        )


def _seed_delivery(
    hub_root: Path,
    *,
    delivery_id: str,
    thread_id: str,
    turn_id: str,
    surface_kind: str,
    surface_key: str,
    state: str,
    final_status: str = "completed",
) -> None:
    with open_orchestration_sqlite(hub_root, durable=True, migrate=True) as conn:
        conn.execute(
            """
            INSERT INTO orch_managed_thread_deliveries (
                delivery_id,
                managed_thread_id,
                managed_turn_id,
                idempotency_key,
                surface_kind,
                adapter_key,
                surface_key,
                envelope_version,
                final_status,
                state,
                delivered_at,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                thread_id,
                turn_id,
                f"delivery:{delivery_id}",
                surface_kind,
                surface_kind,
                surface_key,
                "managed_thread.final.v1",
                final_status,
                state,
                "2026-05-11T00:02:30Z" if state == "delivered" else None,
                "2026-05-11T00:02:10Z",
                "2026-05-11T00:02:20Z",
            ),
        )


def _append_event(
    journal: SQLiteChatSurfaceEventJournal,
    *,
    key: str,
    event_type: str,
    surface_kind: str,
    surface_key: str,
    status: str,
    thread_id: str | None = None,
    lifecycle_status: str = "active",
) -> None:
    journal.append_event(
        idempotency_key=key,
        event_type=event_type,  # type: ignore[arg-type]
        surface_kind=surface_kind,
        surface_key=surface_key,
        managed_thread_id=thread_id,
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        lifecycle_status=lifecycle_status,
        status=status,
        source_kind="contract",
        source_id=key,
        occurred_at="2026-05-11T00:03:00Z",
    )


def _event_payloads(text: str, event_name: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
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


def _event_ids(text: str, event_name: str) -> list[str]:
    ids: list[str] = []
    current_event: str | None = None
    current_id: str | None = None
    for line in text.splitlines():
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ")
            current_id = None
        elif line.startswith("id: "):
            current_id = line.removeprefix("id: ")
        elif line == "" and current_event == event_name and current_id is not None:
            ids.append(current_id)
            current_event = None
            current_id = None
    return ids


def _surface_by_key(snapshot: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (surface["surface_kind"], surface["surface_key"]): surface
        for surface in snapshot["surfaces"]
    }


def _seed_cross_surface_contract_facts(hub_root: Path) -> None:
    journal = SQLiteChatSurfaceEventJournal(hub_root, durable=True)
    bindings = OrchestrationBindingStore(hub_root, durable=True)

    _seed_thread(hub_root, thread_id="thread-discord-new")
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="guild:new",
        thread_target_id="thread-discord-new",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        metadata={"display_name": "Discord /new"},
    )

    _seed_thread(hub_root, thread_id="thread-telegram-bind")
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="-100:10",
        thread_target_id="thread-telegram-bind",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        metadata={"display_name": "Telegram topic"},
    )

    _seed_thread(hub_root, thread_id="thread-discord-old")
    _seed_thread(hub_root, thread_id="thread-discord-rebound")
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="guild:rebind",
        thread_target_id="thread-discord-old",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
    )
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="guild:rebind",
        thread_target_id="thread-discord-rebound",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
    )

    _seed_thread(
        hub_root,
        thread_id="thread-pma-archive",
        lifecycle_status="archived",
        display_name="Archived PMA",
    )
    _append_event(
        journal,
        key="pma:archive",
        event_type="surface.archived",
        surface_kind="pma",
        surface_key="thread-pma-archive",
        thread_id="thread-pma-archive",
        status="archived",
        lifecycle_status="archived",
    )

    _seed_thread(hub_root, thread_id="thread-web-queued")
    _seed_execution(hub_root, thread_id="thread-web-queued", status="queued")
    _append_event(
        journal,
        key="web:queued",
        event_type="queue.state_changed",
        surface_kind="web",
        surface_key="chat:repo:thread-web-queued",
        thread_id="thread-web-queued",
        status="queued",
    )

    _seed_thread(hub_root, thread_id="thread-telegram-running")
    _seed_execution(hub_root, thread_id="thread-telegram-running", status="running")
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="-100:20",
        thread_target_id="thread-telegram-running",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
    )

    _seed_thread(hub_root, thread_id="thread-discord-done")
    _seed_execution(hub_root, thread_id="thread-discord-done", status="completed")
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="guild:done",
        thread_target_id="thread-discord-done",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
    )
    _seed_delivery(
        hub_root,
        delivery_id="delivery-discord-done",
        thread_id="thread-discord-done",
        turn_id="turn-done",
        surface_kind="discord",
        surface_key="guild:done",
        state="delivered",
    )
    _append_event(
        journal,
        key="discord:done",
        event_type="delivery.status_changed",
        surface_kind="discord",
        surface_key="guild:done",
        thread_id="thread-discord-done",
        status="delivered",
    )

    _seed_thread(hub_root, thread_id="thread-pma-failed")
    _seed_execution(hub_root, thread_id="thread-pma-failed", status="failed")

    _seed_thread(hub_root, thread_id="thread-telegram-retry")
    _seed_execution(hub_root, thread_id="thread-telegram-retry", status="completed")
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="-100:30",
        thread_target_id="thread-telegram-retry",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
    )
    _seed_delivery(
        hub_root,
        delivery_id="delivery-telegram-retry",
        thread_id="thread-telegram-retry",
        turn_id="turn-retry",
        surface_kind="telegram",
        surface_key="-100:30",
        state="retry_scheduled",
    )
    _append_event(
        journal,
        key="telegram:retry",
        event_type="delivery.status_changed",
        surface_kind="telegram",
        surface_key="-100:30",
        thread_id="thread-telegram-retry",
        status="retry_scheduled",
    )

    ChannelDirectoryStore(hub_root).record_seen(
        "discord",
        "guild-discovered",
        None,
        "Discovered Discord channel",
    )

    _seed_thread(hub_root, thread_id="thread-notification-source")
    _seed_thread(hub_root, thread_id="thread-notification-continuation")
    notifications = PmaNotificationStore(hub_root)
    notifications.record_notification(
        correlation_id="corr-contract",
        source_kind="pma",
        delivery_mode="direct",
        surface_kind="discord",
        surface_key="guild:new",
        delivery_record_id="delivery-record-contract",
        repo_id="repo",
        managed_thread_id="thread-notification-source",
        notification_id="notif-contract",
    )
    notifications.mark_delivered(
        delivery_record_id="delivery-record-contract",
        delivered_message_id="msg-contract",
    )
    notifications.bind_continuation_thread(
        notification_id="notif-contract",
        thread_target_id="thread-notification-continuation",
    )


def test_end_to_end_chat_surface_contracts_keep_routes_in_sync(hub_env) -> None:
    hub_root = hub_env.hub_root
    _seed_cross_surface_contract_facts(hub_root)

    service_snapshot = ChatSurfaceReadService(hub_root, durable=True).snapshot()
    client = TestClient(create_hub_app(hub_root))
    generic_response = client.get("/hub/chat/events?once=true")
    pma_response = client.get("/hub/pma/events?once=true")

    assert generic_response.status_code == 200
    assert pma_response.status_code == 200
    generic_route_snapshot = _event_payloads(generic_response.text, "chat.snapshot")[0]
    pma_route_snapshot = _event_payloads(pma_response.text, "chat_snapshot")[0]

    expected_lifecycles = {
        ("discord", "guild:new"): ("bound", "thread-discord-new"),
        ("telegram", "-100:10"): ("bound", "thread-telegram-bind"),
        ("discord", "guild:rebind"): ("bound", "thread-discord-rebound"),
        ("pma", "thread-pma-archive"): ("archived", "thread-pma-archive"),
        ("web", "chat:repo:thread-web-queued"): ("queued", "thread-web-queued"),
        ("telegram", "-100:20"): ("running", "thread-telegram-running"),
        ("discord", "guild:done"): ("idle", "thread-discord-done"),
        ("pma", "thread-pma-failed"): ("failed", "thread-pma-failed"),
        ("telegram", "-100:30"): ("idle", "thread-telegram-retry"),
        ("discord", "guild-discovered"): ("discovered", None),
        (
            "notification",
            "notification:notif-contract",
        ): ("bound", "thread-notification-continuation"),
    }

    service_surfaces = _surface_by_key(service_snapshot)
    route_surfaces = _surface_by_key(generic_route_snapshot)
    for key, (expected_lifecycle, expected_thread_id) in expected_lifecycles.items():
        assert service_surfaces[key]["lifecycle"] == expected_lifecycle
        assert route_surfaces[key]["lifecycle"] == expected_lifecycle
        assert service_surfaces[key]["managed_thread_id"] == expected_thread_id
        assert route_surfaces[key]["managed_thread_id"] == expected_thread_id

    pma_threads = {
        thread["managed_thread_id"]: thread for thread in pma_route_snapshot["threads"]
    }
    for key, (expected_lifecycle, thread_id) in expected_lifecycles.items():
        if key[0] != "pma" or thread_id is None:
            continue
        assert pma_threads[thread_id]["normalized_status"] == expected_lifecycle
        assert (
            service_surfaces[key]["lifecycle"]
            == pma_threads[thread_id]["normalized_status"]
        )

    assert _event_payloads(generic_response.text, "chat.event") == []


def test_pma_chat_events_use_numeric_resume_ids_and_tolerate_revision_ids(
    hub_env,
) -> None:
    hub_root = hub_env.hub_root
    _seed_cross_surface_contract_facts(hub_root)
    client = TestClient(create_hub_app(hub_root))

    response = client.get("/hub/pma/events?once=true")

    assert response.status_code == 200
    snapshot = _event_payloads(response.text, "chat_snapshot")[0]
    assert _event_ids(response.text, "chat_snapshot") == [str(snapshot["cursor"])]

    reconnect_response = client.get(
        "/hub/pma/events?once=true",
        headers={"Last-Event-ID": snapshot["revision"]},
    )

    assert reconnect_response.status_code == 200
    assert _event_payloads(reconnect_response.text, "chat_snapshot")


def test_pma_chat_events_reject_malformed_non_revision_last_event_id(
    hub_env,
) -> None:
    client = TestClient(create_hub_app(hub_env.hub_root))

    response = client.get(
        "/hub/pma/events?once=true",
        headers={"Last-Event-ID": "not-a-cursor"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "cursor must be a non-negative integer"


def test_chat_surface_contract_stream_resumes_after_startup_recovery(hub_env) -> None:
    hub_root = hub_env.hub_root
    _seed_cross_surface_contract_facts(hub_root)
    first_service = ChatSurfaceReadService(hub_root, durable=True)
    first_cursor = first_service.latest_cursor()
    SQLiteChatSurfaceEventJournal(hub_root, durable=True).append_event(
        idempotency_key="startup-recovery:running",
        event_type="execution.progress",
        surface_kind="web",
        surface_key="chat:repo:thread-web-queued",
        managed_thread_id="thread-web-queued",
        repo_id="repo",
        resource_kind="repo",
        resource_id="repo",
        status="running",
        occurred_at="2026-05-11T00:04:00Z",
    )

    recovered_service = ChatSurfaceReadService(hub_root, durable=True)
    client = TestClient(create_hub_app(hub_root))
    response = client.get(
        "/hub/chat/events",
        params={"cursor": str(first_cursor), "once": "true"},
    )

    assert response.status_code == 200
    assert recovered_service.latest_cursor() == first_cursor + 1
    assert [
        event["event_type"] for event in recovered_service.events_since(first_cursor)
    ] == ["execution.progress"]
    route_snapshot = _event_payloads(response.text, "chat.snapshot")[0]
    route_events = _event_payloads(response.text, "chat.event")
    assert route_snapshot["cursor"] == first_cursor + 1
    assert route_events == []
