from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.flows import (
    FlowController,
    FlowDefinition,
    FlowRunStatus,
    StepOutcome,
)


@pytest.mark.asyncio
async def test_resume_clears_failure_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "flow.db"
    artifacts_root = tmp_path / "artifacts"

    async def step(record, input_data: dict):
        _ = input_data
        return StepOutcome.complete(output=dict(record.state or {}))

    definition = FlowDefinition(
        flow_type="resume_failure_test",
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
        controller.store.update_flow_run_status(
            run_id=record.id,
            status=FlowRunStatus.FAILED,
            state={"failure": {"failed_at": "x", "stderr_tail": "boom"}},
        )

        updated = await controller.resume_flow(record.id)
        assert updated.status == FlowRunStatus.RUNNING
        assert isinstance(updated.state, dict)
        assert "failure" not in updated.state
    finally:
        controller.shutdown()
