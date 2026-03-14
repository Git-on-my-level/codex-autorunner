from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.core.orchestration.flows import (
    TicketFlowTargetWrapper,
    build_ticket_flow_target,
)
from codex_autorunner.core.orchestration.models import FlowRunTarget
from codex_autorunner.core.orchestration.service import FlowBackedOrchestrationService


def _make_flow_run_record(
    run_id: str,
    *,
    status: FlowRunStatus,
    current_step: str | None = None,
    state: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
) -> FlowRunRecord:
    return FlowRunRecord(
        id=run_id,
        flow_type="ticket_flow",
        status=status,
        current_step=current_step,
        state=state or {},
        metadata=metadata or {},
        created_at="2026-03-14T00:00:00Z",
        started_at=(
            "2026-03-14T00:00:01Z" if status != FlowRunStatus.PENDING else None
        ),
        finished_at=("2026-03-14T00:00:02Z" if status.is_terminal() else None),
        error_message=None,
    )


@pytest.mark.asyncio
async def test_ticket_flow_wrapper_maps_runtime_records_to_orchestration_targets():
    workspace_root = Path("/tmp/example-repo").resolve()
    flow_target = build_ticket_flow_target(workspace_root, repo_id="repo-1")
    calls: list[tuple[object, ...]] = []

    async def start_flow_run(
        repo_root: Path,
        *,
        input_data: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
        run_id: str | None = None,
    ) -> FlowRunRecord:
        calls.append(("start", repo_root, input_data, metadata, run_id))
        return _make_flow_run_record(
            run_id or "run-1",
            status=FlowRunStatus.PENDING,
            current_step="bootstrap",
            metadata={"source": "test"},
        )

    async def resume_flow_run(
        repo_root: Path,
        run_id: str,
        *,
        force: bool = False,
    ) -> FlowRunRecord:
        calls.append(("resume", repo_root, run_id, force))
        return _make_flow_run_record(
            run_id,
            status=FlowRunStatus.RUNNING,
            current_step="worker",
            state={"resumed": True},
        )

    async def stop_flow_run(repo_root: Path, run_id: str) -> FlowRunRecord:
        calls.append(("stop", repo_root, run_id))
        return _make_flow_run_record(
            run_id,
            status=FlowRunStatus.STOPPED,
            current_step="worker",
        )

    def get_flow_run(repo_root: Path, run_id: str) -> FlowRunRecord | None:
        calls.append(("get", repo_root, run_id))
        if run_id != "run-1":
            return None
        return _make_flow_run_record(
            run_id,
            status=FlowRunStatus.PAUSED,
            current_step="await_reply",
            state={"paused": True},
        )

    def list_active_runs(repo_root: Path) -> list[FlowRunRecord]:
        calls.append(("list", repo_root))
        return [
            _make_flow_run_record(
                "run-1",
                status=FlowRunStatus.PAUSED,
                current_step="await_reply",
            ),
            _make_flow_run_record(
                "run-2",
                status=FlowRunStatus.RUNNING,
                current_step="worker",
            ),
        ]

    wrapper = TicketFlowTargetWrapper(
        flow_target=flow_target,
        start_flow_run_fn=start_flow_run,
        resume_flow_run_fn=resume_flow_run,
        stop_flow_run_fn=stop_flow_run,
        get_flow_run_status_fn=get_flow_run,
        list_flow_runs_fn=list_active_runs,
        list_active_flow_runs_fn=list_active_runs,
    )

    started = await wrapper.start_run(
        input_data={"ticket": 620},
        metadata={"source": "cli"},
        run_id="run-1",
    )
    paused = wrapper.get_run("run-1")
    listed = wrapper.list_runs()
    active = wrapper.list_active_runs()
    resumed = await wrapper.resume_run("run-1", force=True)
    stopped = await wrapper.stop_run("run-1")

    assert calls == [
        ("start", workspace_root, {"ticket": 620}, {"source": "cli"}, "run-1"),
        ("get", workspace_root, "run-1"),
        ("list", workspace_root),
        ("list", workspace_root),
        ("resume", workspace_root, "run-1", True),
        ("stop", workspace_root, "run-1"),
    ]
    assert started == FlowRunTarget(
        run_id="run-1",
        flow_target_id="ticket_flow",
        flow_type="ticket_flow",
        status="pending",
        current_step="bootstrap",
        repo_id="repo-1",
        workspace_root=str(workspace_root),
        created_at="2026-03-14T00:00:00Z",
        started_at=None,
        finished_at=None,
        error_message=None,
        state={},
        metadata={"source": "test"},
    )
    assert paused is not None
    assert paused.status == "paused"
    assert paused.state == {"paused": True}
    assert [run.run_id for run in listed] == ["run-1", "run-2"]
    assert [run.run_id for run in active] == ["run-1", "run-2"]
    assert resumed.status == "running"
    assert resumed.state == {"resumed": True}
    assert stopped.status == "stopped"


@pytest.mark.asyncio
async def test_flow_service_routes_operations_through_ticket_flow_wrapper():
    flow_target = build_ticket_flow_target(Path("/tmp/example-repo"), repo_id="repo-1")
    runs: dict[str, FlowRunTarget] = {}
    calls: list[tuple[object, ...]] = []

    class StubWrapper:
        def __init__(self) -> None:
            self.flow_target = flow_target

        async def start_run(self, **kwargs: object) -> FlowRunTarget:
            calls.append(("start", kwargs))
            run = FlowRunTarget(
                run_id="run-1",
                flow_target_id=flow_target.flow_target_id,
                flow_type=flow_target.flow_type,
                status="pending",
                current_step="bootstrap",
                repo_id="repo-1",
                workspace_root=flow_target.workspace_root,
                created_at="2026-03-14T00:00:00Z",
                metadata={"source": "test"},
            )
            runs[run.run_id] = run
            return run

        async def resume_run(
            self, run_id: str, *, force: bool = False
        ) -> FlowRunTarget:
            calls.append(("resume", run_id, force))
            run = runs[run_id]
            resumed = FlowRunTarget(**{**run.to_dict(), "status": "running"})
            runs[run_id] = resumed
            return resumed

        async def stop_run(self, run_id: str) -> FlowRunTarget:
            calls.append(("stop", run_id))
            run = runs[run_id]
            stopped = FlowRunTarget(**{**run.to_dict(), "status": "stopped"})
            runs[run_id] = stopped
            return stopped

        def ensure_run_worker(self, run_id: str, *, is_terminal: bool = False) -> None:
            calls.append(("ensure", run_id, is_terminal))

        def reconcile_run(self, run_id: str) -> tuple[FlowRunTarget, bool, bool]:
            calls.append(("reconcile", run_id))
            return runs[run_id], True, False

        async def wait_for_terminal(
            self,
            run_id: str,
            *,
            timeout_seconds: float = 10.0,
            poll_interval_seconds: float = 0.25,
        ) -> FlowRunTarget | None:
            calls.append(("wait", run_id, timeout_seconds, poll_interval_seconds))
            return runs.get(run_id)

        def archive_run(
            self, run_id: str, *, force: bool = False, delete_run: bool = True
        ) -> dict[str, object]:
            calls.append(("archive", run_id, force, delete_run))
            return {
                "run_id": run_id,
                "archived_tickets": 1,
                "archived_runs": True,
                "archived_contextspace": False,
            }

        def get_run(self, run_id: str) -> FlowRunTarget | None:
            calls.append(("get", run_id))
            return runs.get(run_id)

        def list_runs(self) -> list[FlowRunTarget]:
            calls.append(("list_all",))
            return list(runs.values())

        def list_active_runs(self) -> list[FlowRunTarget]:
            calls.append(("list",))
            return [
                run
                for run in runs.values()
                if run.status in {"pending", "running", "paused", "stopping"}
            ]

    service = FlowBackedOrchestrationService(
        flow_wrappers={flow_target.flow_target_id: StubWrapper()}
    )

    started = await service.start_flow_run(
        "ticket_flow",
        input_data={"ticket": 620},
        metadata={"source": "web"},
        run_id="ignored-by-stub",
    )
    assert service.list_flow_targets() == [flow_target]
    assert service.get_flow_target("ticket_flow") == flow_target
    assert service.get_flow_run(started.run_id) == started
    assert [run.run_id for run in service.list_flow_runs()] == ["run-1"]
    assert [run.run_id for run in service.list_active_flow_runs()] == ["run-1"]

    resumed = await service.resume_flow_run("run-1", force=True)
    stopped = await service.stop_flow_run("run-1")
    service.ensure_flow_run_worker("run-1")
    reconciled, updated, locked = service.reconcile_flow_run("run-1")
    waited = await service.wait_for_flow_run_terminal("run-1")
    archived = service.archive_flow_run("run-1", force=True, delete_run=False)

    assert resumed.status == "running"
    assert stopped.status == "stopped"
    assert reconciled.status == "stopped"
    assert updated is True
    assert locked is False
    assert waited == stopped
    assert archived["run_id"] == "run-1"
    assert service.list_active_flow_runs() == []
    assert service.get_flow_target("missing") is None
    with pytest.raises(KeyError, match="Unknown flow run 'missing'"):
        await service.resume_flow_run("missing")
    assert calls == [
        (
            "start",
            {
                "input_data": {"ticket": 620},
                "metadata": {"source": "web"},
                "run_id": "ignored-by-stub",
            },
        ),
        ("get", "run-1"),
        ("list_all",),
        ("list",),
        ("get", "run-1"),
        ("resume", "run-1", True),
        ("get", "run-1"),
        ("stop", "run-1"),
        ("get", "run-1"),
        ("ensure", "run-1", False),
        ("get", "run-1"),
        ("reconcile", "run-1"),
        ("get", "run-1"),
        ("wait", "run-1", 10.0, 0.25),
        ("get", "run-1"),
        ("archive", "run-1", True, False),
        ("list",),
        ("get", "missing"),
    ]
