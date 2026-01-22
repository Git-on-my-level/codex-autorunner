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
def simple_flow_definition():
    steps = {}

    async def step1(record: FlowRunRecord, input_data: dict) -> StepOutcome:
        return StepOutcome.continue_to(
            next_steps={"step2"}, output={"step1_done": True}
        )

    async def step2(record: FlowRunRecord, input_data: dict) -> StepOutcome:
        return StepOutcome.complete(output={"step2_done": True})

    steps["step1"] = step1
    steps["step2"] = step2

    definition = FlowDefinition(
        flow_type="test_flow",
        initial_step="step1",
        steps=steps,
    )
    definition.validate()
    return definition


@pytest.fixture
def flow_controller(temp_dir, simple_flow_definition):
    db_path = temp_dir / "flow.db"
    artifacts_root = temp_dir / "artifacts"
    controller = FlowController(
        definition=simple_flow_definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
    )
    controller.initialize()
    yield controller
    controller.shutdown()


@pytest.mark.asyncio
async def test_flow_controller_start_and_complete(flow_controller):
    record = await flow_controller.start_flow(
        input_data={"test": "value"},
    )

    assert record.id
    assert record.flow_type == "test_flow"
    assert record.status == FlowRunStatus.RUNNING

    await asyncio.sleep(0.5)

    final_record = flow_controller.get_status(record.id)
    assert final_record is not None
    assert final_record.status == FlowRunStatus.COMPLETED
    assert final_record.state.get("step1_done") is True
    assert final_record.state.get("step2_done") is True


@pytest.mark.asyncio
async def test_flow_controller_stop(flow_controller):
    record = await flow_controller.start_flow(
        input_data={"test": "value"},
    )

    stopped = await flow_controller.stop_flow(record.id)
    assert stopped.status in {FlowRunStatus.STOPPED, FlowRunStatus.COMPLETED}


@pytest.mark.asyncio
async def test_flow_controller_resume(flow_controller):
    async def failing_step(record: FlowRunRecord, input_data: dict) -> StepOutcome:
        return StepOutcome.fail("Intentional failure")

    async def recovery_step(record: FlowRunRecord, input_data: dict) -> StepOutcome:
        return StepOutcome.complete(output={"recovered": True})

    definition = FlowDefinition(
        flow_type="test_resume",
        initial_step="failing",
        steps={"failing": failing_step, "recovery": recovery_step},
    )

    controller = FlowController(
        definition=definition,
        db_path=flow_controller.db_path,
        artifacts_root=flow_controller.artifacts_root,
    )
    controller.initialize()

    record = await controller.start_flow(input_data={})
    await asyncio.sleep(0.5)

    final = controller.get_status(record.id)
    assert final is not None
    assert final.status == FlowRunStatus.FAILED

    final.state["current_step"] = "recovery"
    controller.store.update_flow_run_status(
        run_id=record.id,
        status=FlowRunStatus.STOPPED,
        state=final.state,
    )

    await controller.resume_flow(record.id)
    await asyncio.sleep(0.5)

    resumed_final = controller.get_status(record.id)
    assert resumed_final is not None
    assert resumed_final.status in {FlowRunStatus.COMPLETED, FlowRunStatus.STOPPED}

    controller.shutdown()


@pytest.mark.asyncio
async def test_flow_event_streaming(flow_controller):
    record = await flow_controller.start_flow(input_data={})

    events = []
    async for event in flow_controller.stream_events(record.id):
        events.append(event)
        if event.event_type.value in {"flow_completed", "flow_failed", "flow_stopped"}:
            break

    assert len(events) > 0
    assert any(e.event_type.value == "flow_started" for e in events)


def test_flow_store_persistence(flow_controller):
    flow_controller.store.create_flow_run(
        run_id="test-run-1",
        flow_type="test_flow",
        input_data={"key": "value"},
    )

    retrieved = flow_controller.store.get_flow_run("test-run-1")
    assert retrieved is not None
    assert retrieved.id == "test-run-1"
    assert retrieved.input_data == {"key": "value"}


def test_flow_events(flow_controller):
    event = flow_controller.store.create_event(
        event_id="test-event-1",
        run_id="test-run-1",
        event_type="step_started",
        data={"step_id": "step1"},
    )

    assert event.id == "test-event-1"
    assert event.run_id == "test-run-1"

    retrieved = flow_controller.store.get_events("test-run-1")
    assert len(retrieved) == 1
    assert retrieved[0].id == "test-event-1"


def test_flow_artifacts(flow_controller):
    artifact = flow_controller.store.create_artifact(
        artifact_id="test-artifact-1",
        run_id="test-run-1",
        kind="spec",
        path="/tmp/spec.md",
    )

    assert artifact.id == "test-artifact-1"
    assert artifact.kind == "spec"

    retrieved = flow_controller.store.get_artifacts("test-run-1")
    assert len(retrieved) == 1
    assert retrieved[0].id == "test-artifact-1"
