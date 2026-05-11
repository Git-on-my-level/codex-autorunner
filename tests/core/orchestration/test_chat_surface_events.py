from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.orchestration import (
    SQLiteChatSurfaceEventJournal,
    normalize_chat_surface_event_type,
)


def test_append_event_is_idempotent_and_cursor_readable(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    journal = SQLiteChatSurfaceEventJournal(hub_root, durable=False)

    first = journal.append_event(
        idempotency_key="bind:discord:guild-channel",
        event_type="surface.bound",
        surface_kind="Discord",
        surface_key=" guild:channel ",
        managed_thread_id="thread-1",
        repo_id="repo-1",
        lifecycle_status="active",
        source_kind="test",
        source_id="case-1",
        payload={"binding_id": "binding-1"},
        occurred_at="2026-05-11T00:00:00Z",
    )
    duplicate = journal.append_event(
        idempotency_key="bind:discord:guild-channel",
        event_type="surface.bound",
        surface_kind="discord",
        surface_key="guild:channel",
        payload={"binding_id": "ignored"},
    )

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.event == first.event
    assert first.event.cursor == 1
    assert first.event.surface_kind == "discord"
    assert first.event.surface_key == "guild:channel"
    assert first.event.payload == {"binding_id": "binding-1"}
    assert journal.latest_cursor() == first.event.cursor
    assert journal.read_events_since(0) == [first.event]
    assert journal.read_events_since(first.event.cursor) == []


def test_read_events_since_resumes_without_duplicates_after_reopen(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    journal = SQLiteChatSurfaceEventJournal(hub_root, durable=False)
    first = journal.append_event(
        idempotency_key="queue:1",
        event_type="queue.state_changed",
        surface_kind="web",
        surface_key="chat-1",
        status="queued",
    ).event

    reopened = SQLiteChatSurfaceEventJournal(hub_root, durable=False)
    second = reopened.append_event(
        idempotency_key="execution:1",
        event_type="execution.progress",
        surface_kind="web",
        surface_key="chat-1",
        managed_thread_id="thread-1",
        status="running",
        payload={"percent": 50},
    ).event
    third = reopened.append_event(
        idempotency_key="delivery:1",
        event_type="delivery.status_changed",
        surface_kind="web",
        surface_key="chat-1",
        status="delivered",
    ).event

    assert [event.cursor for event in reopened.read_events_since(first.cursor)] == [
        second.cursor,
        third.cursor,
    ]
    assert reopened.read_history(limit=2) == [second, third]


@pytest.mark.parametrize(
    "event_type",
    [
        "surface.bound",
        "surface.rebound",
        "surface.archived",
        "lifecycle.status_changed",
        "queue.state_changed",
        "execution.progress",
        "delivery.status_changed",
        "notification.reply_context_changed",
        "channel_directory.discovered",
    ],
)
def test_chat_surface_event_type_catalog_includes_required_mutations(
    event_type: str,
) -> None:
    assert normalize_chat_surface_event_type(event_type) == event_type


def test_append_event_rejects_unknown_event_type(tmp_path: Path) -> None:
    journal = SQLiteChatSurfaceEventJournal(tmp_path / "hub", durable=False)

    with pytest.raises(ValueError, match="unknown chat surface event type"):
        journal.append_event(
            idempotency_key="bad",
            event_type="surface.exploded",  # type: ignore[arg-type]
            surface_kind="web",
            surface_key="chat-1",
        )
