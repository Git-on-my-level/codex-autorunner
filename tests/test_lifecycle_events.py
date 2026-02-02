"""Test lifecycle events system."""
import json
import tempfile
from pathlib import Path
from codex_autorunner.core.lifecycle_events import (
    LifecycleEvent,
    LifecycleEventEmitter,
    LifecycleEventStore,
    LifecycleEventType,
)


def test_lifecycle_event_store_load_save():
    """Test that lifecycle event store can load and save events."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        store = LifecycleEventStore(tmp_path)

        event1 = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_PAUSED,
            repo_id="test-repo",
            run_id="run-123",
            data={"test": "data"},
        )
        event2 = LifecycleEvent(
            event_type=LifecycleEventType.DISPATCH_CREATED,
            repo_id="test-repo",
            run_id="run-123",
            data={"seq": 1},
        )

        store.append(event1)
        store.append(event2)

        loaded = store.load()
        assert len(loaded) == 2
        assert loaded[0].event_type == LifecycleEventType.FLOW_PAUSED
        assert loaded[0].repo_id == "test-repo"
        assert loaded[0].run_id == "run-123"
        assert loaded[0].data == {"test": "data"}
        assert loaded[0].processed is False
        assert loaded[1].event_type == LifecycleEventType.DISPATCH_CREATED
        assert loaded[1].data == {"seq": 1}


def test_lifecycle_event_store_get_unprocessed():
    """Test that store returns only unprocessed events."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        store = LifecycleEventStore(tmp_path)

        event1 = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_PAUSED,
            repo_id="test-repo",
            run_id="run-1",
        )
        event2 = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_COMPLETED,
            repo_id="test-repo",
            run_id="run-2",
        )
        event3 = LifecycleEvent(
            event_type=LifecycleEventType.DISPATCH_CREATED,
            repo_id="test-repo",
            run_id="run-3",
        )

        store.append(event1)
        store.append(event2)
        store.append(event3)

        unprocessed = store.get_unprocessed()
        assert len(unprocessed) == 3

        store.mark_processed(event1.event_id)

        unprocessed = store.get_unprocessed()
        assert len(unprocessed) == 2
        assert unprocessed[0].event_id == event2.event_id
        assert unprocessed[1].event_id == event3.event_id


def test_lifecycle_event_emitter():
    """Test that lifecycle event emitter stores events."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        emitter = LifecycleEventEmitter(tmp_path)

        emitter.emit_flow_paused("test-repo", "run-1")
        emitter.emit_flow_completed("test-repo", "run-1")
        emitter.emit_dispatch_created("test-repo", "run-1")

        events = emitter._store.load()
        assert len(events) == 3
        assert events[0].event_type == LifecycleEventType.FLOW_PAUSED
        assert events[1].event_type == LifecycleEventType.FLOW_COMPLETED
        assert events[2].event_type == LifecycleEventType.DISPATCH_CREATED


def test_lifecycle_event_store_prune():
    """Test that pruning keeps only last N processed events."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        store = LifecycleEventStore(tmp_path)

        for i in range(10):
            event = LifecycleEvent(
                event_type=LifecycleEventType.FLOW_PAUSED,
                repo_id=f"repo-{i}",
                run_id=f"run-{i}",
            )
            store.append(event)
            store.mark_processed(event.event_id)

        all_events = store.load()
        assert len(all_events) == 10

        store.prune_processed(keep_last=5)

        pruned = store.load()
        assert len(pruned) == 5


if __name__ == "__main__":
    test_lifecycle_event_store_load_save()
    test_lifecycle_event_store_get_unprocessed()
    test_lifecycle_event_emitter()
    test_lifecycle_event_store_prune()
    print("All tests passed!")
