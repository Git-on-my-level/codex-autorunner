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
        count = int(record.state.get("count") or 0)
        if count == 0:
            return StepOutcome.pause(output={"count": 1})
        return StepOutcome.complete(output={"count": 2})

    steps["step1"] = step1

    definition = FlowDefinition(
        flow_type="ticket_flow",
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
async def test_new_run_supersedes_older_paused_runs(flow_controller):
    record1 = await flow_controller.start_flow(input_data={"test": "value1"})
    await flow_controller.run_flow(record1.id)

    record1_check = flow_controller.get_status(record1.id)
    assert record1_check.status == FlowRunStatus.PAUSED

    record2 = await flow_controller.start_flow(input_data={"test": "value2"})

    record1_after = flow_controller.get_status(record1.id)
    assert record1_after.status == FlowRunStatus.SUPERSEDED
    assert record1_after.metadata.get("superseded_by") == record2.id

    assert record2.status == FlowRunStatus.PENDING


@pytest.mark.asyncio
async def test_resume_rejects_superseded_run(flow_controller):
    record1 = await flow_controller.start_flow(input_data={"test": "value1"})
    await flow_controller.run_flow(record1.id)

    record2 = await flow_controller.start_flow(input_data={"test": "value2"})

    record1_after = flow_controller.get_status(record1.id)
    assert record1_after.status == FlowRunStatus.SUPERSEDED

    with pytest.raises(ValueError) as exc_info:
        await flow_controller.resume_flow(record1.id)

    assert "superseded" in str(exc_info.value).lower()
    assert record2.id in str(exc_info.value)


@pytest.mark.asyncio
async def test_resume_rejects_when_another_run_is_active(flow_controller):
    record1 = await flow_controller.start_flow(input_data={"test": "value1"})
    flow_controller.store.update_flow_run_status(
        run_id=record1.id, status=FlowRunStatus.PAUSED
    )

    record2 = flow_controller.store.create_flow_run(
        run_id="test-active-run-id",
        flow_type="ticket_flow",
        input_data={"test": "value2"},
    )
    flow_controller.store.update_flow_run_status(
        run_id=record2.id, status=FlowRunStatus.RUNNING
    )

    with pytest.raises(ValueError) as exc_info:
        await flow_controller.resume_flow(record1.id, force=True)

    assert "already active" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_store_mark_run_superseded(temp_dir, simple_flow_definition):
    db_path = temp_dir / "flow.db"
    artifacts_root = temp_dir / "artifacts"
    controller = FlowController(
        definition=simple_flow_definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
    )
    controller.initialize()

    record1 = await controller.start_flow(input_data={"test": "value1"})
    record2 = await controller.start_flow(input_data={"test": "value2"})

    controller.store.update_flow_run_status(
        run_id=record1.id, status=FlowRunStatus.PAUSED
    )

    result = controller.store.mark_run_superseded(record1.id, superseded_by=record2.id)
    assert result is not None
    assert result.status == FlowRunStatus.SUPERSEDED
    assert result.metadata.get("superseded_by") == record2.id
    assert result.metadata.get("superseded_at") is not None
    assert result.finished_at is not None

    controller.shutdown()


@pytest.mark.asyncio
async def test_store_list_paused_runs_for_supersession(
    temp_dir, simple_flow_definition
):
    db_path = temp_dir / "flow.db"
    artifacts_root = temp_dir / "artifacts"
    controller = FlowController(
        definition=simple_flow_definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
    )
    controller.initialize()

    record1 = await controller.start_flow(input_data={"test": "value1"})
    record2 = await controller.start_flow(input_data={"test": "value2"})
    record3 = await controller.start_flow(input_data={"test": "value3"})

    controller.store.update_flow_run_status(
        run_id=record1.id, status=FlowRunStatus.PAUSED
    )
    controller.store.update_flow_run_status(
        run_id=record2.id, status=FlowRunStatus.PAUSED
    )

    paused = controller.store.list_paused_runs_for_supersession(
        flow_type="ticket_flow", exclude_run_id=record3.id
    )

    paused_ids = {r.id for r in paused}
    assert record1.id in paused_ids
    assert record2.id in paused_ids
    assert record3.id not in paused_ids

    controller.shutdown()


@pytest.mark.asyncio
async def test_superseded_status_is_terminal():
    assert FlowRunStatus.SUPERSEDED.is_terminal() is True
    assert FlowRunStatus.SUPERSEDED.is_active() is False
    assert FlowRunStatus.SUPERSEDED.is_paused() is False


@pytest.mark.asyncio
async def test_mark_run_superseded_only_affects_paused_runs(
    temp_dir, simple_flow_definition
):
    db_path = temp_dir / ".codex-autorunner" / "flows.db"
    artifacts_root = temp_dir / ".codex-autorunner" / "flows"
    controller = FlowController(
        definition=simple_flow_definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
    )
    controller.initialize()

    try:
        record1 = await controller.start_flow(input_data={"test": "value1"})
        record2 = await controller.start_flow(input_data={"test": "value2"})

        controller.store.update_flow_run_status(
            run_id=record1.id, status=FlowRunStatus.RUNNING
        )

        result = controller.store.mark_run_superseded(
            record1.id, superseded_by=record2.id
        )
        assert result is None

        unchanged = controller.store.get_flow_run(record1.id)
        assert unchanged is not None
        assert unchanged.status == FlowRunStatus.RUNNING
        assert unchanged.metadata.get("superseded_by") is None

        controller.store.update_flow_run_status(
            run_id=record1.id, status=FlowRunStatus.PAUSED
        )
        result = controller.store.mark_run_superseded(
            record1.id, superseded_by=record2.id
        )
        assert result is not None
        assert result.status == FlowRunStatus.SUPERSEDED

    finally:
        controller.shutdown()


@pytest.mark.asyncio
async def test_resume_clears_stale_worker_metadata(temp_dir, simple_flow_definition):
    flows_dir = temp_dir / ".codex-autorunner" / "flows"
    db_path = temp_dir / ".codex-autorunner" / "flows.db"
    artifacts_root = flows_dir
    controller = FlowController(
        definition=simple_flow_definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
    )
    controller.initialize()

    try:
        record1 = await controller.start_flow(input_data={"test": "value1"})
        await controller.run_flow(record1.id)

        flows_dir_for_run = flows_dir / record1.id
        flows_dir_for_run.mkdir(parents=True, exist_ok=True)
        worker_json = flows_dir_for_run / "worker.json"
        worker_json.write_text('{"pid": 12345, "cmd": "old command"}')

        assert worker_json.exists()

        await controller.resume_flow(record1.id, force=True)

        assert not worker_json.exists()
    finally:
        controller.shutdown()
