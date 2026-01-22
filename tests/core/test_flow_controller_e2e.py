"""
End-to-end flow controller tests.

Tests verify to complete lifecycle of flow controller operations,
including DB state transitions and SSE event ordering.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from codex_autorunner.core.flows import (
    FlowController,
    FlowDefinition,
    FlowRunRecord,
    FlowRunStatus,
    StepOutcome,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def flow_definition():
    """Create a simple flow definition for testing."""

    async def step1(record: FlowRunRecord, input_data: dict) -> StepOutcome:
        await asyncio.sleep(0.05)  # Simulate work
        return StepOutcome.continue_to(
            next_steps={"step2"},
            output={"step1_done": True, "value": input_data.get("value", 0) + 1},
        )

    async def step2(record: FlowRunRecord, input_data: dict) -> StepOutcome:
        await asyncio.sleep(0.05)
        return StepOutcome.continue_to(
            next_steps={"step3"},
            output={"step2_done": True},
        )

    async def step3(record: FlowRunRecord, input_data: dict) -> StepOutcome:
        await asyncio.sleep(0.05)
        return StepOutcome.complete(
            output={"step3_done": True, "final_value": 42},
        )

    definition = FlowDefinition(
        flow_type="test_flow",
        initial_step="step1",
        steps={"step1": step1, "step2": step2, "step3": step3},
    )
    definition.validate()
    return definition


@pytest.fixture
def flow_controller(temp_dir, flow_definition):
    """Create a flow controller."""
    db_path = temp_dir / "flow.db"
    artifacts_root = temp_dir / "artifacts"
    controller = FlowController(
        definition=flow_definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
    )
    controller.initialize()
    yield controller
    controller.shutdown()


@pytest.mark.asyncio
async def test_flow_controller_stop_flow(flow_controller):
    """Test stopping a flow."""
    # Start a slow flow that takes longer to stop
    record = await flow_controller.start_flow(input_data={"value": 0})
    assert record.id
    assert record.status == FlowRunStatus.PENDING

    runner = asyncio.create_task(flow_controller.run_flow(record.id))
    await asyncio.sleep(0.1)

    # Stop of flow
    stopped_record = await flow_controller.stop_flow(record.id)
    await runner
    assert stopped_record.id == record.id
    assert stopped_record.status in {
        FlowRunStatus.STOPPED,
        FlowRunStatus.COMPLETED,
    }

    # Verify stop event
    events = flow_controller.store.get_events(record.id)
    event_types = [e.event_type.value for e in events]
    if stopped_record.status == FlowRunStatus.STOPPED:
        assert "flow_stopped" in event_types


@pytest.mark.asyncio
async def test_flow_controller_stop(flow_controller):
    """Test stopping a flow."""
    # Start a slow flow that takes longer to stop
    record = await flow_controller.start_flow(input_data={"value": 0})
    assert record.id
    assert record.status == FlowRunStatus.PENDING

    runner = asyncio.create_task(flow_controller.run_flow(record.id))
    await asyncio.sleep(0.1)

    # Stop the flow
    stopped_record = await flow_controller.stop_flow(record.id)
    await runner
    assert stopped_record.id == record.id
    assert stopped_record.status in {
        FlowRunStatus.STOPPED,
        FlowRunStatus.COMPLETED,
    }

    # Verify stop event
    events = flow_controller.store.get_events(record.id)
    event_types = [e.event_type.value for e in events]
    if stopped_record.status == FlowRunStatus.STOPPED:
        assert "flow_stopped" in event_types


@pytest.mark.asyncio
async def test_flow_controller_db_state_transitions(flow_controller):
    """Validate DB state transitions throughout flow lifecycle."""
    record = await flow_controller.start_flow(input_data={})
    run_id = record.id

    run_task = asyncio.create_task(flow_controller.run_flow(run_id))

    # Initial state
    await asyncio.sleep(0.05)
    state1 = flow_controller.store.get_flow_run(run_id)
    assert state1.status == FlowRunStatus.RUNNING
    assert state1.started_at is not None

    # Let it progress
    await asyncio.sleep(0.15)

    # Check state after first step
    state2 = flow_controller.store.get_flow_run(run_id)
    assert state2.status == FlowRunStatus.RUNNING
    assert state2.state.get("step1_done") is True

    # Stop and check final state
    await flow_controller.stop_flow(run_id)
    await run_task
    final_state = flow_controller.store.get_flow_run(run_id)

    # Final state should be either STOPPED or COMPLETED
    assert final_state.status in {FlowRunStatus.STOPPED, FlowRunStatus.COMPLETED}

    # Verify event history
    events = flow_controller.store.get_events(run_id)
    assert len(events) > 0

    # Verify events have expected types
    event_types = [e.event_type.value for e in events]
    assert "flow_started" in event_types
    assert "step_started" in event_types


@pytest.mark.asyncio
async def test_flow_controller_sse_event_ordering(flow_controller):
    """Validate SSE event ordering is correct."""
    record = await flow_controller.start_flow(input_data={})
    run_id = record.id

    # Collect events via stream
    collected_events = []
    event_types_in_order = []

    runner = asyncio.create_task(flow_controller.run_flow(run_id))

    async def collect_events():
        async for event in flow_controller.stream_events(run_id):
            collected_events.append(event)
            event_types_in_order.append(event.event_type.value)
            if event.event_type.value in {
                "flow_completed",
                "flow_failed",
                "flow_stopped",
            }:
                break

    # Start collection in background
    collect_task = asyncio.create_task(collect_events())

    # Wait for completion
    await asyncio.wait_for(collect_task, timeout=10.0)
    await runner

    # Verify event ordering
    assert len(event_types_in_order) > 0

    # flow_started should be first
    assert event_types_in_order[0] == "flow_started"

    # Verify step events are ordered
    step_started_indices = [
        i for i, t in enumerate(event_types_in_order) if t == "step_started"
    ]
    step_completed_indices = [
        i for i, t in enumerate(event_types_in_order) if t == "step_completed"
    ]

    # Each step should start before completing
    assert len(step_started_indices) >= len(step_completed_indices)
    for start_idx, end_idx in zip(step_started_indices, step_completed_indices):
        assert (
            start_idx < end_idx
        ), f"Step started at {start_idx} but completed at {end_idx}"

    # Verify final state
    final_record = flow_controller.get_status(run_id)
    assert final_record.status in {
        FlowRunStatus.COMPLETED,
        FlowRunStatus.STOPPED,
        FlowRunStatus.FAILED,
    }


@pytest.mark.asyncio
async def test_flow_controller_multiple_runs_isolated(flow_controller):
    """Test that multiple flow runs are isolated from each other."""
    # Start two flows
    record1 = await flow_controller.start_flow(input_data={"value": 0})
    record2 = await flow_controller.start_flow(input_data={"value": 10})

    assert record1.id != record2.id
    assert record1.status == FlowRunStatus.PENDING
    assert record2.status == FlowRunStatus.PENDING

    final1_task = asyncio.create_task(flow_controller.run_flow(record1.id))
    final2_task = asyncio.create_task(flow_controller.run_flow(record2.id))
    await asyncio.gather(final1_task, final2_task)

    # Verify both completed
    final1 = final1_task.result()
    final2 = final2_task.result()

    assert final1.status == FlowRunStatus.COMPLETED
    assert final2.status == FlowRunStatus.COMPLETED

    # Verify states are isolated (value is incremented by step1)
    assert final1.state.get("value") == 1  # 0 + 1 from step1
    assert final2.state.get("value") == 11  # 10 + 1 from step1

    # Verify events are isolated
    events1 = flow_controller.store.get_events(record1.id)
    events2 = flow_controller.store.get_events(record2.id)

    assert len(events1) > 0
    assert len(events2) > 0

    # No event should be shared
    event_ids_1 = {e.id for e in events1}
    event_ids_2 = {e.id for e in events2}
    assert event_ids_1.isdisjoint(event_ids_2)


@pytest.mark.asyncio
async def test_flow_controller_resume_preserves_state(flow_controller):
    """Test that resume preserves existing state."""
    # Start a flow
    record = await flow_controller.start_flow(input_data={"initial": "value"})
    run_id = record.id

    # Let it progress partially
    runner = asyncio.create_task(flow_controller.run_flow(run_id))
    await asyncio.sleep(0.1)

    # Get current state
    state_before = flow_controller.get_status(run_id)
    assert state_before is not None

    await flow_controller.stop_flow(run_id)
    await runner

    state_after_stop = flow_controller.get_status(run_id)
    if state_after_stop.status == FlowRunStatus.STOPPED:
        modified_state = state_after_stop.state.copy()
        modified_state["resumed"] = True
        flow_controller.store.update_flow_run_status(
            run_id=run_id,
            status=FlowRunStatus.STOPPED,
            state=modified_state,
        )

        await flow_controller.resume_flow(run_id)
        final_state = await flow_controller.run_flow(run_id)
        assert final_state.status == FlowRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_flow_controller_stop_before_completion(flow_controller):
    """Test stopping a flow before it completes naturally."""
    # Start flow
    record = await flow_controller.start_flow(input_data={})
    run_id = record.id

    runner = asyncio.create_task(flow_controller.run_flow(run_id))
    # Stop immediately
    stopped = await flow_controller.stop_flow(run_id)
    await runner

    # Verify stopped state
    assert stopped.id == run_id
    assert stopped.status in {FlowRunStatus.STOPPED, FlowRunStatus.COMPLETED}

    # Verify stop event
    events = flow_controller.store.get_events(run_id)
    event_types = [e.event_type.value for e in events]
    if FlowRunStatus.STOPPED in [stopped.status]:
        assert "flow_stopped" in event_types or "flow_completed" in event_types


@pytest.mark.asyncio
async def test_flow_controller_list_runs(flow_controller):
    """Test listing flow runs."""
    # Start a few flows
    record1 = await flow_controller.start_flow(input_data={"seq": 1})
    record2 = await flow_controller.start_flow(input_data={"seq": 2})
    record3 = await flow_controller.start_flow(input_data={"seq": 3})

    await asyncio.gather(
        flow_controller.run_flow(record1.id),
        flow_controller.run_flow(record2.id),
        flow_controller.run_flow(record3.id),
    )

    # List all runs
    all_runs = flow_controller.list_runs()
    assert len(all_runs) >= 3

    # List running runs (should be 0 after completion)
    running = flow_controller.list_runs(status=FlowRunStatus.RUNNING)
    assert len(running) == 0

    # List completed runs
    completed = flow_controller.list_runs(status=FlowRunStatus.COMPLETED)
    assert len(completed) >= 3


@pytest.mark.asyncio
async def test_flow_controller_error_handling(flow_controller):
    """Test error handling in flow controller."""

    # Create a flow that fails
    async def failing_step(record: FlowRunRecord, input_data: dict) -> StepOutcome:
        await asyncio.sleep(0.05)
        return StepOutcome.fail("Intentional failure")

    definition = FlowDefinition(
        flow_type="failing_flow",
        initial_step="fail",
        steps={"fail": failing_step},
    )
    definition.validate()

    # Use separate controller for failing flow
    db_path = flow_controller.artifacts_root / "failing.db"
    failing_controller = FlowController(
        definition=definition,
        db_path=db_path,
        artifacts_root=flow_controller.artifacts_root / "failing",
    )
    failing_controller.initialize()

    try:
        # Start failing flow
        record = await failing_controller.start_flow(input_data={})

        final = await failing_controller.run_flow(record.id)

        # Verify failed state
        assert final.status == FlowRunStatus.FAILED

        # Verify failure event
        events = failing_controller.store.get_events(record.id)
        event_types = [e.event_type.value for e in events]
        # step_failed is emitted when a step returns StepOutcome.fail()
        assert "step_failed" in event_types
        # The flow status should be FAILED
        assert final.error_message is not None

    finally:
        failing_controller.shutdown()
