from __future__ import annotations

from codex_autorunner.surfaces.web.routes.pma_routes.managed_thread_tail_serializers import (
    _record_serialized_tail_event,
)


def test_record_serialized_tail_event_uses_live_payload_fields() -> None:
    snapshot = {
        "events": [{"event_id": 1, "summary": "tool: old", "received_at": "old-at"}],
        "last_event_at": "old-at",
    }
    serialized_event = {
        "event_id": 2,
        "summary": "tool: new",
        "received_at": "new-at",
    }

    event_id = _record_serialized_tail_event(snapshot, serialized_event)

    assert event_id == 2
    assert snapshot["events"][-1] == serialized_event
    assert snapshot["last_event_at"] == "new-at"
