"""Test flow event projection into orchestration storage."""

import tempfile
from pathlib import Path

from codex_autorunner.core.lifecycle_events import (
    LifecycleEventEmitter,
    LifecycleEventStore,
    LifecycleEventType,
)


def test_flow_lifecycle_events_project_to_orchestration():
    """Test that flow lifecycle events are projected into orchestration SQLite."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        store = LifecycleEventStore(tmp_path)
        emitter = LifecycleEventEmitter(tmp_path)

        emitter.emit_flow_started(
            "test-repo", "run-123", data={"flow_type": "ticket_flow"}
        )

        emitter.emit_flow_resumed(
            "test-repo", "run-123", data={"flow_type": "ticket_flow"}
        )

        emitter.emit_flow_paused(
            "test-repo", "run-123", data={"reason": "waiting for input"}
        )

        emitter.emit_flow_completed(
            "test-repo", "run-123", data={"output": {"result": "success"}}
        )

        emitter.emit_flow_failed(
            "test-repo", "run-123", data={"error": "something went wrong"}
        )

        emitter.emit_dispatch_created("test-repo", "run-123", data={"seq": 1})

        loaded = store.load()
        assert len(loaded) == 6

        event_types = {e.event_type for e in loaded}
        assert LifecycleEventType.FLOW_STARTED in event_types
        assert LifecycleEventType.FLOW_RESUMED in event_types
        assert LifecycleEventType.FLOW_PAUSED in event_types
        assert LifecycleEventType.FLOW_COMPLETED in event_types
        assert LifecycleEventType.FLOW_FAILED in event_types
        assert LifecycleEventType.DISPATCH_CREATED in event_types

        started_events = [
            e for e in loaded if e.event_type == LifecycleEventType.FLOW_STARTED
        ]
        assert len(started_events) == 1
        assert started_events[0].repo_id == "test-repo"
        assert started_events[0].run_id == "run-123"
        assert started_events[0].data.get("flow_type") == "ticket_flow"

        resumed_events = [
            e for e in loaded if e.event_type == LifecycleEventType.FLOW_RESUMED
        ]
        assert len(resumed_events) == 1
        assert resumed_events[0].data.get("flow_type") == "ticket_flow"

        paused_events = [
            e for e in loaded if e.event_type == LifecycleEventType.FLOW_PAUSED
        ]
        assert len(paused_events) == 1
        assert paused_events[0].data.get("reason") == "waiting for input"

        completed_events = [
            e for e in loaded if e.event_type == LifecycleEventType.FLOW_COMPLETED
        ]
        assert len(completed_events) == 1
        assert completed_events[0].data.get("output", {}).get("result") == "success"

        failed_events = [
            e for e in loaded if e.event_type == LifecycleEventType.FLOW_FAILED
        ]
        assert len(failed_events) == 1
        assert failed_events[0].data.get("error") == "something went wrong"

        dispatch_events = [
            e for e in loaded if e.event_type == LifecycleEventType.DISPATCH_CREATED
        ]
        assert len(dispatch_events) == 1
        assert dispatch_events[0].data.get("seq") == 1


def test_flow_events_persist_in_orchestration_sqlite():
    """Test that lifecycle events are persisted in orchestration SQLite storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        emitter = LifecycleEventEmitter(tmp_path)

        emitter.emit_flow_started("repo-1", "run-1", data={"step": "init"})
        emitter.emit_flow_resumed("repo-1", "run-1", data={"step": "resume"})
        emitter.emit_flow_paused("repo-1", "run-1", data={"reason": "pause"})
        emitter.emit_flow_completed("repo-1", "run-1", data={"result": "done"})

        store = LifecycleEventStore(tmp_path)
        events = store.load()

        assert len(events) == 4

        event_by_type = {e.event_type: e for e in events}

        assert LifecycleEventType.FLOW_STARTED in event_by_type
        assert event_by_type[LifecycleEventType.FLOW_STARTED].data.get("step") == "init"

        assert LifecycleEventType.FLOW_RESUMED in event_by_type
        assert (
            event_by_type[LifecycleEventType.FLOW_RESUMED].data.get("step") == "resume"
        )

        assert LifecycleEventType.FLOW_PAUSED in event_by_type
        assert (
            event_by_type[LifecycleEventType.FLOW_PAUSED].data.get("reason") == "pause"
        )

        assert LifecycleEventType.FLOW_COMPLETED in event_by_type
        assert (
            event_by_type[LifecycleEventType.FLOW_COMPLETED].data.get("result")
            == "done"
        )


def test_dispatch_visibility_projection():
    """Test that dispatch created events are visible for orchestration consumers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        emitter = LifecycleEventEmitter(tmp_path)

        emitter.emit_dispatch_created(
            "test-repo",
            "run-123",
            data={"seq": 1, "title": "Review PR", "mode": "pause"},
        )
        emitter.emit_dispatch_created(
            "test-repo",
            "run-123",
            data={"seq": 2, "title": "Approve", "mode": "handoff"},
        )

        store = LifecycleEventStore(tmp_path)
        events = store.load()

        dispatch_events = [
            e for e in events if e.event_type == LifecycleEventType.DISPATCH_CREATED
        ]
        assert len(dispatch_events) == 2

        dispatch_events.sort(key=lambda e: e.data.get("seq", 0))
        assert dispatch_events[0].data.get("title") == "Review PR"
        assert dispatch_events[0].data.get("mode") == "pause"
        assert dispatch_events[1].data.get("title") == "Approve"
        assert dispatch_events[1].data.get("mode") == "handoff"
