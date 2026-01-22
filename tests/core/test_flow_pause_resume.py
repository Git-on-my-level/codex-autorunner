from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.flows import FlowController, FlowDefinition, FlowRunStatus, StepOutcome


@pytest.mark.asyncio
async def test_flow_can_pause_and_resume(tmp_path: Path) -> None:
    db_path = tmp_path / "flow.db"
    artifacts_root = tmp_path / "artifacts"

    async def step(record, input_data: dict):
        # Pause on first invocation, then complete on resume.
        _ = input_data
        count = int(record.state.get("count") or 0)
        if count == 0:
            return StepOutcome.pause(output={"count": 1})
        return StepOutcome.complete(output={"count": 2})

    definition = FlowDefinition(
        flow_type="pause_test",
        initial_step="step",
        steps={"step": step},
    )
    definition.validate()

    controller = FlowController(
        definition=definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
    )
    controller.initialize()
    try:
        record = await controller.start_flow(input_data={})
        paused = await controller.run_flow(record.id)
        assert paused.status == FlowRunStatus.PAUSED

        await controller.resume_flow(record.id)
        completed = await controller.run_flow(record.id)
        assert completed.status == FlowRunStatus.COMPLETED
        assert completed.state.get("count") == 2
    finally:
        controller.shutdown()
