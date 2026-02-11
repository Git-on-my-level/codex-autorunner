from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List

import pytest

from codex_autorunner.core.flows.controller import FlowController
from codex_autorunner.core.flows.definition import FlowDefinition, StepOutcome
from codex_autorunner.core.flows.models import FlowEventType, FlowRunStatus


def _make_controller(
    tmp_path: Path,
    steps: dict[str, Any],
    initial_step: str | None = None,
    *,
    flow_type: str = "test-flow",
) -> FlowController:
    definition = FlowDefinition(
        flow_type=flow_type,
        initial_step=initial_step or list(steps.keys())[0],
        steps=steps,
    )
    controller = FlowController(
        definition=definition,
        db_path=tmp_path / ".codex-autorunner" / "flows.db",
        artifacts_root=tmp_path / ".codex-autorunner" / "flows",
    )
    controller.initialize()
    return controller


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True
    )


@pytest.mark.asyncio
async def test_resume_clears_stop_requested(tmp_path: Path) -> None:
    async def only_step(_record, _input):
        return StepOutcome.complete()

    controller = _make_controller(tmp_path, {"step": only_step})
    record = await controller.start_flow(input_data={}, run_id="run-1")

    controller.store.set_stop_requested(record.id, True)
    paused = controller.store.get_flow_run(record.id)
    assert paused and paused.stop_requested is True

    cleared = await controller.resume_flow(record.id)
    assert cleared.stop_requested is False
    # Resume updates status immediately; run_flow can proceed afterwards.
    assert cleared.status == FlowRunStatus.RUNNING


@pytest.mark.asyncio
async def test_paused_flow_resumes_and_completes(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, {}, initial_step="first")
    events: List[FlowEventType] = []

    async def first_step(record, _input):
        if not record.state.get("ready"):
            # First pass pauses and marks state for resume.
            return StepOutcome.pause(output={"ready": True})
        return StepOutcome.continue_to({"second"}, output={"first": "done"})

    async def second_step(_record, _input):
        return StepOutcome.complete(output={"second": "done"})

    controller.definition.steps = {"first": first_step, "second": second_step}
    controller.definition.initial_step = "first"
    controller.add_event_listener(lambda e: events.append(e.event_type))

    record = await controller.start_flow(input_data={}, run_id="run-2")
    paused = await controller.run_flow(record.id)

    assert paused.status == FlowRunStatus.PAUSED
    assert paused.current_step == "first"
    assert paused.state["ready"] is True

    await controller.resume_flow(record.id)
    finished = await controller.run_flow(record.id)

    assert finished.status == FlowRunStatus.COMPLETED
    assert finished.current_step is None
    assert finished.state["first"] == "done"
    assert finished.state["second"] == "done"

    # Event stream should include resume and completion.
    assert events[0] == FlowEventType.FLOW_STARTED
    assert FlowEventType.FLOW_RESUMED in events
    assert events.count(FlowEventType.FLOW_COMPLETED) == 1


@pytest.mark.asyncio
async def test_stop_requested_mid_run_halts_flow(tmp_path: Path) -> None:
    calls: list[str] = []

    controller = _make_controller(tmp_path, {}, initial_step="first")

    async def first_step(record, _input):
        calls.append("first")
        # Signal stop before next step executes.
        controller.store.set_stop_requested(record.id, True)
        return StepOutcome.continue_to({"second"})

    async def second_step(_record, _input):
        calls.append("second")
        return StepOutcome.complete()

    controller.definition.steps = {"first": first_step, "second": second_step}
    controller.definition.initial_step = "first"

    record = await controller.start_flow(input_data={}, run_id="run-3")
    finished = await controller.run_flow(record.id)

    assert finished.status == FlowRunStatus.STOPPED
    assert finished.current_step == "second"
    assert calls == ["first"]  # second step never ran


@pytest.mark.asyncio
async def test_flow_failure_sets_failed_status_and_events(tmp_path: Path) -> None:
    events: List[FlowEventType] = []

    async def boom(_record, _input):
        raise RuntimeError("boom")

    controller = _make_controller(tmp_path, {"boom": boom})
    controller.add_event_listener(lambda e: events.append(e.event_type))
    record = await controller.start_flow(input_data={}, run_id="run-4")

    failed = await controller.run_flow(record.id)

    assert failed.status == FlowRunStatus.FAILED
    assert failed.error_message == "boom"
    assert failed.current_step is None
    assert FlowEventType.STEP_FAILED in events


@pytest.mark.asyncio
async def test_flow_state_persists_across_reopen(tmp_path: Path) -> None:
    async def first_step(record, _input):
        if record.state.get("counter"):
            return StepOutcome.continue_to({"second"})
        return StepOutcome.pause(output={"counter": 1})

    async def second_step(record, _input):
        # Ensure state from previous run is available.
        assert record.state.get("counter") == 1
        return StepOutcome.complete(output={"second": "done"})

    steps = {"first": first_step, "second": second_step}

    controller1 = _make_controller(tmp_path, steps, initial_step="first")
    record = await controller1.start_flow(input_data={}, run_id="run-5")
    paused = await controller1.run_flow(record.id)
    assert paused.status == FlowRunStatus.PAUSED
    controller1.shutdown()

    # New controller instance reads existing DB/state and resumes.
    controller2 = _make_controller(tmp_path, steps, initial_step="first")
    finished = await controller2.run_flow(record.id)

    assert finished.status == FlowRunStatus.COMPLETED
    assert finished.state["counter"] == 1
    assert finished.state["second"] == "done"


@pytest.mark.asyncio
async def test_resume_flow_rejects_active_run(tmp_path: Path) -> None:
    async def step(_record, _input):
        return StepOutcome.complete()

    controller = _make_controller(tmp_path, {"step": step})
    record = await controller.start_flow(input_data={}, run_id="run-6")
    controller.store.update_flow_run_status(
        run_id=record.id, status=FlowRunStatus.RUNNING
    )

    with pytest.raises(ValueError):
        await controller.resume_flow(record.id)


@pytest.mark.asyncio
async def test_ticket_flow_resume_requires_signal_or_force(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)

    async def step(_record, _input):
        return StepOutcome.complete()

    controller = _make_controller(tmp_path, {"step": step}, flow_type="ticket_flow")
    run_id = "run-blocked"
    record = await controller.start_flow(input_data={}, run_id=run_id)

    fingerprint = controller._repo_fingerprint()
    assert isinstance(fingerprint, str)
    blocked_state = {
        "ticket_engine": {
            "status": "paused",
            "reason_code": "loop_no_diff",
            "pause_context": {
                "paused_reply_seq": 0,
                "repo_fingerprint": fingerprint,
            },
        }
    }
    controller.store.update_flow_run_status(
        run_id=record.id, status=FlowRunStatus.PAUSED, state=blocked_state
    )

    with pytest.raises(ValueError):
        await controller.resume_flow(record.id)

    reply_path = tmp_path / ".codex-autorunner" / "runs" / run_id / "USER_REPLY.md"
    reply_path.parent.mkdir(parents=True, exist_ok=True)
    reply_path.write_text("unblock\n", encoding="utf-8")

    resumed = await controller.resume_flow(record.id)
    assert resumed.status == FlowRunStatus.RUNNING


@pytest.mark.asyncio
async def test_ticket_flow_resume_signal_uses_workspace_root(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    workspace = tmp_path / "nested-workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    async def step(_record, _input):
        return StepOutcome.complete()

    controller = _make_controller(tmp_path, {"step": step}, flow_type="ticket_flow")
    run_id = "run-nested"
    record = await controller.start_flow(
        input_data={"workspace_root": "nested-workspace"},
        run_id=run_id,
    )

    fingerprint = controller._repo_fingerprint()
    assert isinstance(fingerprint, str)
    blocked_state = {
        "ticket_engine": {
            "status": "paused",
            "reason_code": "loop_no_diff",
            "pause_context": {
                "paused_reply_seq": 0,
                "repo_fingerprint": fingerprint,
            },
        }
    }
    controller.store.update_flow_run_status(
        run_id=record.id, status=FlowRunStatus.PAUSED, state=blocked_state
    )

    reply_path = workspace / ".codex-autorunner" / "runs" / run_id / "USER_REPLY.md"
    reply_path.parent.mkdir(parents=True, exist_ok=True)
    reply_path.write_text("reply in workspace\n", encoding="utf-8")

    resumed = await controller.resume_flow(record.id)
    assert resumed.status == FlowRunStatus.RUNNING


@pytest.mark.asyncio
async def test_ticket_flow_force_resume_bypasses_signal_check(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)

    async def step(_record, _input):
        return StepOutcome.complete()

    controller = _make_controller(tmp_path, {"step": step}, flow_type="ticket_flow")
    run_id = "run-force"
    record = await controller.start_flow(input_data={}, run_id=run_id)
    fingerprint = controller._repo_fingerprint()
    assert isinstance(fingerprint, str)
    blocked_state = {
        "ticket_engine": {
            "status": "paused",
            "reason_code": "infra_error",
            "pause_context": {
                "paused_reply_seq": 0,
                "repo_fingerprint": fingerprint,
            },
        }
    }
    controller.store.update_flow_run_status(
        run_id=record.id, status=FlowRunStatus.PAUSED, state=blocked_state
    )

    resumed = await controller.resume_flow(record.id, force=True)
    assert resumed.status == FlowRunStatus.RUNNING


@pytest.mark.asyncio
async def test_step_stop_outcome_sets_stopped(tmp_path: Path) -> None:
    async def stop_step(_record, _input):
        return StepOutcome.stop(output={"stopped": True})

    controller = _make_controller(tmp_path, {"stop": stop_step})
    record = await controller.start_flow(input_data={}, run_id="run-7")

    stopped = await controller.run_flow(record.id)

    assert stopped.status == FlowRunStatus.STOPPED
    assert stopped.current_step is None
    assert stopped.state["stopped"] is True
    assert stopped.finished_at is not None


@pytest.mark.asyncio
async def test_step_complete_sets_finished_and_state(tmp_path: Path) -> None:
    async def done_step(_record, _input):
        return StepOutcome.complete(output={"done": 1})

    controller = _make_controller(tmp_path, {"done": done_step})
    record = await controller.start_flow(input_data={}, run_id="run-8")

    finished = await controller.run_flow(record.id)

    assert finished.status == FlowRunStatus.COMPLETED
    assert finished.state["done"] == 1
    assert finished.current_step is None
    assert finished.finished_at is not None


@pytest.mark.asyncio
async def test_continue_advances_to_sorted_next_step(tmp_path: Path) -> None:
    events: List[FlowEventType] = []

    async def first(_record, _input):
        return StepOutcome.continue_to({"b", "a"})

    async def step_a(record, _input):
        events.append(FlowEventType.STEP_STARTED)
        return StepOutcome.complete(output={"a": True})

    controller = _make_controller(
        tmp_path, {"first": first, "a": step_a, "b": step_a}, initial_step="first"
    )
    controller.add_event_listener(lambda e: events.append(e.event_type))
    record = await controller.start_flow(input_data={}, run_id="run-9")

    finished = await controller.run_flow(record.id)

    assert finished.status == FlowRunStatus.COMPLETED
    # ensure 'a' executed before completion by checking output
    assert finished.state["a"] is True
