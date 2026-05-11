from __future__ import annotations

import sqlite3
from pathlib import Path

from codex_autorunner.core.hub_projection_service import HubProjectionService
from codex_autorunner.core.hub_projection_store import (
    ENTITY_STATE_PROJECTION_FAMILY,
    HubProjectionStore,
)
from codex_autorunner.core.orchestration.chat_surface_emitters import (
    emit_chat_surface_event,
)


def test_hub_ui_events_are_idempotent_cursor_ordered_and_bounded(
    tmp_path: Path,
) -> None:
    service = HubProjectionService(tmp_path / "hub", durable=False)

    first = service.apply_canonical_event(
        idempotency_key="event-1",
        entity_kind="chat",
        entity_id="thread-1",
        operation="patch",
        payload={"status": "queued"},
        source_revision="canonical:1",
    )
    duplicate = service.apply_canonical_event(
        idempotency_key="event-1",
        entity_kind="chat",
        entity_id="thread-1",
        operation="patch",
        payload={"status": "ignored"},
        source_revision="canonical:ignored",
    )
    second = service.apply_canonical_event(
        idempotency_key="event-2",
        entity_kind="chat",
        entity_id="thread-1",
        operation="patch",
        payload={"status": "running"},
        source_revision="canonical:2",
    )

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.event == first.event
    assert [event.cursor for event in service.read_events_since(0, limit=1).events] == [
        first.event.cursor
    ]
    batch = service.read_events_since(first.event.cursor, limit=10)
    assert batch.gap_detected is False
    assert [event.cursor for event in batch.events] == [second.event.cursor]
    assert batch.latest_cursor == second.event.cursor


def test_hub_projection_rebuild_matches_incremental_state(tmp_path: Path) -> None:
    service = HubProjectionService(tmp_path / "hub", durable=False)
    service.apply_canonical_event(
        idempotency_key="chat-queued",
        entity_kind="chat",
        entity_id="thread-1",
        operation="patch",
        payload={"status": "queued"},
        source_revision="canonical:1",
    )
    latest = service.apply_canonical_event(
        idempotency_key="chat-running",
        entity_kind="chat",
        entity_id="thread-1",
        operation="patch",
        payload={"status": "running"},
        source_revision="canonical:2",
    )

    key = "chat:thread-1"
    incremental = service.read_snapshot(key=key)
    assert incremental is not None
    assert incremental.payload["payload"] == {"status": "running"}

    service.rebuild_family(ENTITY_STATE_PROJECTION_FAMILY)

    rebuilt = service.read_snapshot(key=key)
    assert rebuilt is not None
    assert rebuilt.payload == incremental.payload
    assert rebuilt.cursor == latest.event.cursor


def test_hub_projection_snapshot_window_supports_cursor_and_revision_reads(
    tmp_path: Path,
) -> None:
    service = HubProjectionService(tmp_path / "hub", durable=False)
    first = service.apply_canonical_event(
        idempotency_key="repo-1",
        entity_kind="repo",
        entity_id="repo-1",
        operation="patch",
        payload={"name": "one"},
    )
    service.apply_canonical_event(
        idempotency_key="repo-2",
        entity_kind="repo",
        entity_id="repo-2",
        operation="patch",
        payload={"name": "two"},
    )

    window = service.read_snapshot_window(
        after_cursor=first.event.cursor,
        after_revision=0,
    )

    assert [item["key"] for item in window["items"]] == ["repo:repo-2"]


def test_hub_ui_event_gap_is_reported_for_stale_cursor(tmp_path: Path) -> None:
    service = HubProjectionService(tmp_path / "hub", durable=False)
    service.apply_canonical_event(
        idempotency_key="event-1",
        entity_kind="repo",
        entity_id="repo-1",
        operation="patch",
        payload={"name": "repo"},
    )

    batch = service.read_events_since(99)

    assert batch.events == []
    assert batch.gap_detected is True


def test_hub_projection_validation_resets_stale_schema(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path / "hub", durable=False)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store.path) as conn:
        conn.execute("CREATE TABLE hub_ui_events (cursor INTEGER PRIMARY KEY)")

    store.validate_or_reset_projection_state()
    result = store.append_event(
        idempotency_key="after-reset",
        entity_kind="repo",
        entity_id="repo-1",
        operation="patch",
        payload={"ok": True},
    )

    assert result.event.cursor == 1
    assert store.read_window(family=ENTITY_STATE_PROJECTION_FAMILY)["items"]


def test_chat_surface_mutation_emits_hub_ui_patch_event(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    result = emit_chat_surface_event(
        hub_root,
        durable=False,
        idempotency_key="surface-bind-1",
        event_type="surface.bound",
        surface_kind="discord",
        surface_key="guild:channel",
        managed_thread_id="thread-1",
        repo_id="repo-1",
        payload={"binding_id": "binding-1"},
    )

    batch = HubProjectionService(hub_root, durable=False).read_events_since(0)

    assert len(batch.events) == 1
    event = batch.events[0]
    assert event.source_revision == f"chat_surface:{result.event.cursor}"
    assert event.entity_kind == "chat_surface"
    assert event.entity_id == "discord:guild:channel"
    assert event.operation == "upsert"
    assert event.payload["repo_id"] == "repo-1"
