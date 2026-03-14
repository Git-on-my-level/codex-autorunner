from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from .models import FlowRunTarget, FlowTarget

if TYPE_CHECKING:
    from ..flows.models import FlowRunRecord


@dataclass(frozen=True)
class PausedFlowTarget:
    """Resolved paused flow target that can accept a conversational reply."""

    flow_target: FlowTarget
    run_id: str
    status: Optional[str] = None
    workspace_root: Optional[Path] = None


async def _start_ticket_flow_run(
    repo_root: Path,
    *,
    input_data: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    run_id: Optional[str] = None,
) -> FlowRunRecord:
    from ...flows.ticket_flow.runtime_helpers import start_ticket_flow_run

    return await start_ticket_flow_run(
        repo_root,
        input_data=input_data,
        metadata=metadata,
        run_id=run_id,
    )


async def _resume_ticket_flow_run(
    repo_root: Path,
    run_id: str,
    *,
    force: bool = False,
) -> FlowRunRecord:
    from ...flows.ticket_flow.runtime_helpers import resume_ticket_flow_run

    return await resume_ticket_flow_run(repo_root, run_id, force=force)


async def _stop_ticket_flow_run(repo_root: Path, run_id: str) -> FlowRunRecord:
    from ...flows.ticket_flow.runtime_helpers import stop_ticket_flow_run

    return await stop_ticket_flow_run(repo_root, run_id)


def _ensure_ticket_flow_worker(
    repo_root: Path,
    run_id: str,
    *,
    is_terminal: bool = False,
) -> None:
    from ...flows.ticket_flow.runtime_helpers import ensure_ticket_flow_worker

    ensure_ticket_flow_worker(repo_root, run_id, is_terminal=is_terminal)


def _reconcile_ticket_flow_run(
    repo_root: Path,
    run_id: str,
) -> tuple[FlowRunRecord, bool, bool]:
    from ...flows.ticket_flow.runtime_helpers import reconcile_ticket_flow_run

    return reconcile_ticket_flow_run(repo_root, run_id)


async def _wait_for_ticket_flow_terminal(
    repo_root: Path,
    run_id: str,
    *,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.25,
) -> Optional[FlowRunRecord]:
    from ...flows.ticket_flow.runtime_helpers import wait_for_ticket_flow_terminal

    return await wait_for_ticket_flow_terminal(
        repo_root,
        run_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def _archive_ticket_flow_run(
    repo_root: Path,
    run_id: str,
    *,
    force: bool = False,
    delete_run: bool = True,
) -> dict[str, Any]:
    from ...flows.ticket_flow.runtime_helpers import archive_ticket_flow_run

    return archive_ticket_flow_run(
        repo_root,
        run_id,
        force=force,
        delete_run=delete_run,
    )


def _get_ticket_flow_run_status(
    repo_root: Path, run_id: str
) -> Optional[FlowRunRecord]:
    from ...flows.ticket_flow.runtime_helpers import get_ticket_flow_run_status

    return get_ticket_flow_run_status(repo_root, run_id)


def _list_active_ticket_flow_runs(repo_root: Path) -> list[FlowRunRecord]:
    from ...flows.ticket_flow.runtime_helpers import list_active_ticket_flow_runs

    return list_active_ticket_flow_runs(repo_root)


def _list_ticket_flow_runs(repo_root: Path) -> list[FlowRunRecord]:
    from ...flows.ticket_flow.runtime_helpers import list_ticket_flow_runs

    return list_ticket_flow_runs(repo_root)


def _flow_run_target_from_record(
    record: FlowRunRecord,
    *,
    flow_target: FlowTarget,
) -> FlowRunTarget:
    return FlowRunTarget(
        run_id=record.id,
        flow_target_id=flow_target.flow_target_id,
        flow_type=record.flow_type,
        status=record.status.value,
        current_step=record.current_step,
        repo_id=flow_target.repo_id,
        workspace_root=flow_target.workspace_root,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        error_message=record.error_message,
        state=dict(record.state or {}),
        metadata=dict(record.metadata or {}),
    )


def build_ticket_flow_target(
    workspace_root: Path,
    *,
    repo_id: Optional[str] = None,
) -> FlowTarget:
    resolved_root = workspace_root.resolve()
    return FlowTarget(
        flow_target_id="ticket_flow",
        flow_type="ticket_flow",
        display_name="ticket_flow",
        repo_id=repo_id,
        workspace_root=str(resolved_root),
        description="CAR-native ticket workflow backed by FlowController and flows.db.",
    )


@dataclass
class TicketFlowTargetWrapper:
    """Orchestration wrapper that preserves native ticket_flow semantics."""

    flow_target: FlowTarget
    start_flow_run_fn: Callable[..., Awaitable[FlowRunRecord]] = _start_ticket_flow_run
    resume_flow_run_fn: Callable[..., Awaitable[FlowRunRecord]] = (
        _resume_ticket_flow_run
    )
    stop_flow_run_fn: Callable[..., Awaitable[FlowRunRecord]] = _stop_ticket_flow_run
    ensure_worker_fn: Callable[..., None] = _ensure_ticket_flow_worker
    reconcile_flow_run_fn: Callable[..., tuple[FlowRunRecord, bool, bool]] = (
        _reconcile_ticket_flow_run
    )
    wait_for_terminal_fn: Callable[..., Awaitable[Optional[FlowRunRecord]]] = (
        _wait_for_ticket_flow_terminal
    )
    archive_flow_run_fn: Callable[..., dict[str, Any]] = _archive_ticket_flow_run
    get_flow_run_status_fn: Callable[..., Optional[FlowRunRecord]] = (
        _get_ticket_flow_run_status
    )
    list_flow_runs_fn: Callable[..., list[FlowRunRecord]] = _list_ticket_flow_runs
    list_active_flow_runs_fn: Callable[..., list[FlowRunRecord]] = (
        _list_active_ticket_flow_runs
    )

    def _workspace_root(self) -> Path:
        workspace_root = self.flow_target.workspace_root
        if not isinstance(workspace_root, str) or not workspace_root.strip():
            raise RuntimeError("ticket_flow target is missing workspace_root")
        return Path(workspace_root)

    async def start_run(
        self,
        *,
        input_data: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> FlowRunTarget:
        record = await self.start_flow_run_fn(
            self._workspace_root(),
            input_data=input_data,
            metadata=metadata,
            run_id=run_id,
        )
        return _flow_run_target_from_record(record, flow_target=self.flow_target)

    async def resume_run(self, run_id: str, *, force: bool = False) -> FlowRunTarget:
        record = await self.resume_flow_run_fn(
            self._workspace_root(),
            run_id,
            force=force,
        )
        return _flow_run_target_from_record(record, flow_target=self.flow_target)

    async def stop_run(self, run_id: str) -> FlowRunTarget:
        record = await self.stop_flow_run_fn(self._workspace_root(), run_id)
        return _flow_run_target_from_record(record, flow_target=self.flow_target)

    def ensure_run_worker(self, run_id: str, *, is_terminal: bool = False) -> None:
        self.ensure_worker_fn(
            self._workspace_root(),
            run_id,
            is_terminal=is_terminal,
        )

    def reconcile_run(self, run_id: str) -> tuple[FlowRunTarget, bool, bool]:
        record, updated, locked = self.reconcile_flow_run_fn(
            self._workspace_root(),
            run_id,
        )
        return (
            _flow_run_target_from_record(record, flow_target=self.flow_target),
            updated,
            locked,
        )

    async def wait_for_terminal(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.25,
    ) -> Optional[FlowRunTarget]:
        record = await self.wait_for_terminal_fn(
            self._workspace_root(),
            run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if record is None:
            return None
        return _flow_run_target_from_record(record, flow_target=self.flow_target)

    def archive_run(
        self,
        run_id: str,
        *,
        force: bool = False,
        delete_run: bool = True,
    ) -> dict[str, Any]:
        return self.archive_flow_run_fn(
            self._workspace_root(),
            run_id,
            force=force,
            delete_run=delete_run,
        )

    def get_run(self, run_id: str) -> Optional[FlowRunTarget]:
        record = self.get_flow_run_status_fn(self._workspace_root(), run_id)
        if record is None:
            return None
        return _flow_run_target_from_record(record, flow_target=self.flow_target)

    def list_runs(self) -> list[FlowRunTarget]:
        records = self.list_flow_runs_fn(self._workspace_root())
        return [
            _flow_run_target_from_record(record, flow_target=self.flow_target)
            for record in records
        ]

    def list_active_runs(self) -> list[FlowRunTarget]:
        records = self.list_active_flow_runs_fn(self._workspace_root())
        return [
            _flow_run_target_from_record(record, flow_target=self.flow_target)
            for record in records
        ]


def build_ticket_flow_target_wrapper(
    workspace_root: Path,
    *,
    repo_id: Optional[str] = None,
) -> TicketFlowTargetWrapper:
    return TicketFlowTargetWrapper(
        flow_target=build_ticket_flow_target(workspace_root, repo_id=repo_id),
        start_flow_run_fn=_start_ticket_flow_run,
        resume_flow_run_fn=_resume_ticket_flow_run,
        stop_flow_run_fn=_stop_ticket_flow_run,
        ensure_worker_fn=_ensure_ticket_flow_worker,
        reconcile_flow_run_fn=_reconcile_ticket_flow_run,
        wait_for_terminal_fn=_wait_for_ticket_flow_terminal,
        archive_flow_run_fn=_archive_ticket_flow_run,
        get_flow_run_status_fn=_get_ticket_flow_run_status,
        list_flow_runs_fn=_list_ticket_flow_runs,
        list_active_flow_runs_fn=_list_active_ticket_flow_runs,
    )


__all__ = [
    "PausedFlowTarget",
    "TicketFlowTargetWrapper",
    "build_ticket_flow_target",
    "build_ticket_flow_target_wrapper",
]
