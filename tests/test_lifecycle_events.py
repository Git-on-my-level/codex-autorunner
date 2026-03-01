"""Test lifecycle events system."""

import asyncio
import tempfile
from pathlib import Path

from codex_autorunner.core.flows import (
    FlowController,
    FlowDefinition,
    FlowRunRecord,
    StepOutcome,
)
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
        assert loaded[0].origin == "system"
        assert loaded[0].processed is False
        assert loaded[1].event_type == LifecycleEventType.DISPATCH_CREATED
        assert loaded[1].data == {"seq": 1}
        assert loaded[1].origin == "system"


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
        emitter.emit_flow_completed("test-repo", "run-1", origin="runner")
        emitter.emit_dispatch_created("test-repo", "run-1", origin="user")

        events = emitter._store.load()
        assert len(events) == 3
        assert events[0].event_type == LifecycleEventType.FLOW_PAUSED
        assert events[1].event_type == LifecycleEventType.FLOW_COMPLETED
        assert events[2].event_type == LifecycleEventType.DISPATCH_CREATED
        assert events[0].origin == "system"
        assert events[1].origin == "runner"
        assert events[2].origin == "user"


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


def test_flow_completed_duplicate_is_deduped_with_metadata_and_stable_event_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        emitter = LifecycleEventEmitter(tmp_path)
        received_ids: list[str] = []

        def _listener(event: LifecycleEvent) -> None:
            received_ids.append(event.event_id)

        emitter.add_listener(_listener)

        first_event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_COMPLETED,
            repo_id="repo-1",
            run_id="run-1",
            data={"transition_token": "completed:1"},
            timestamp="2026-03-01T10:00:00+00:00",
        )
        duplicate_event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_COMPLETED,
            repo_id="repo-1",
            run_id="run-1",
            data={"transition_token": "completed:1"},
            timestamp="2026-03-01T10:01:00+00:00",
        )

        first_id = emitter.emit(first_event)
        duplicate_id = emitter.emit(duplicate_event)

        assert duplicate_id == first_id
        assert received_ids == [first_id]

        events = emitter._store.load()
        assert len(events) == 1
        assert len(emitter._store.get_unprocessed()) == 1

        stored = events[0]
        assert stored.event_id == first_id
        assert stored.event_type == LifecycleEventType.FLOW_COMPLETED
        assert stored.data["transition_token"] == "completed:1"
        assert stored.data["duplicate_count"] == 1
        assert stored.data["first_seen_at"] == "2026-03-01T10:00:00+00:00"
        assert stored.data["last_seen_at"] == "2026-03-01T10:01:00+00:00"


def test_non_duplicate_events_still_append():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        store = LifecycleEventStore(tmp_path)

        store.append(
            LifecycleEvent(
                event_type=LifecycleEventType.FLOW_COMPLETED,
                repo_id="repo-1",
                run_id="run-1",
                data={"transition_token": "completed:1"},
            )
        )
        store.append(
            LifecycleEvent(
                event_type=LifecycleEventType.FLOW_COMPLETED,
                repo_id="repo-1",
                run_id="run-1",
                data={"transition_token": "completed:2"},
            )
        )
        store.append(
            LifecycleEvent(
                event_type=LifecycleEventType.DISPATCH_CREATED,
                repo_id="repo-1",
                run_id="run-1",
            )
        )
        store.append(
            LifecycleEvent(
                event_type=LifecycleEventType.DISPATCH_CREATED,
                repo_id="repo-1",
                run_id="run-1",
            )
        )

        events = store.load()
        assert len(events) == 4
        assert [event.event_type for event in events] == [
            LifecycleEventType.FLOW_COMPLETED,
            LifecycleEventType.FLOW_COMPLETED,
            LifecycleEventType.DISPATCH_CREATED,
            LifecycleEventType.DISPATCH_CREATED,
        ]


def test_runtime_terminal_events_include_transition_metadata():
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path / "repo"
            repo_root.mkdir(parents=True, exist_ok=True)

            async def complete_step(
                record: FlowRunRecord, input_data: dict
            ) -> StepOutcome:
                await asyncio.sleep(0)
                return StepOutcome.complete(output={"done": True})

            definition = FlowDefinition(
                flow_type="test_flow",
                initial_step="step1",
                steps={"step1": complete_step},
            )
            definition.validate()

            controller = FlowController(
                definition=definition,
                db_path=repo_root / ".codex-autorunner" / "flows.db",
                artifacts_root=repo_root / ".codex-autorunner" / "flows",
                hub_root=tmp_path,
            )
            controller.initialize()
            try:
                record = await controller.start_flow(input_data={})
                await controller.run_flow(record.id)
            finally:
                controller.shutdown()

            store = LifecycleEventStore(tmp_path)
            completed = [
                event
                for event in store.load()
                if event.event_type == LifecycleEventType.FLOW_COMPLETED
            ]
            assert completed
            payload = completed[-1].data
            transition_token = payload.get("transition_token")
            idempotency_key = payload.get("transition_idempotency_key")
            assert isinstance(transition_token, str)
            assert transition_token
            assert isinstance(idempotency_key, str)
            assert idempotency_key
            assert transition_token in idempotency_key

    asyncio.run(_run())


if __name__ == "__main__":
    test_lifecycle_event_store_load_save()
    test_lifecycle_event_store_get_unprocessed()
    test_lifecycle_event_emitter()
    test_lifecycle_event_store_prune()
    test_flow_completed_duplicate_is_deduped_with_metadata_and_stable_event_id()
    test_non_duplicate_events_still_append()
    test_runtime_terminal_events_include_transition_metadata()
    print("All tests passed!")
