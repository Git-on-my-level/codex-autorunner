from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..flows.models import FlowRunRecord, FlowRunStatus
from ..flows.store import FlowStore
from ..pma_queue import PmaQueue, QueueItemState
from .execution_graph import _latest_thread_execution, automation_execution_snapshot
from .models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    AUTOMATION_CHILD_KIND_PMA_OPERATOR,
    AUTOMATION_CHILD_KIND_TICKET_FLOW,
    JOB_PAUSED,
    JOB_RUNNING,
    AutomationChildExecutionEdge,
    AutomationJob,
    AutomationRuntimeContract,
)
from .store import AutomationStore

RepoPathResolver = Callable[[str], Optional[Path]]


@dataclass(frozen=True)
class ChildReconcileResult:
    inspected: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    paused: int = 0
    missing: int = 0

    @property
    def changed(self) -> int:
        return (
            self.completed + self.failed + self.cancelled + self.paused + self.missing
        )


class AutomationChildRunReconciler:
    def __init__(
        self,
        store: AutomationStore,
        *,
        resolve_repo_path: RepoPathResolver,
        hub_root: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._store = store
        self._resolve_repo_path = resolve_repo_path
        self._hub_root = hub_root or getattr(store, "hub_root", None)
        self._logger = logger or logging.getLogger(__name__)

    def reconcile_running_jobs(self, *, limit: int = 100) -> ChildReconcileResult:
        inspected = completed = failed = cancelled = paused = missing = 0
        for job in self._running_child_jobs(limit=limit):
            inspected += 1
            try:
                result = self._reconcile_child_job(job)
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                self._logger.exception(
                    "Failed reconciling automation child run for job_id=%s",
                    job.job_id,
                )
                self._store.fail_job(
                    job.job_id,
                    error_text=f"child_run_reconcile_failed: {exc}",
                )
                failed += 1
                continue
            completed += result.completed
            failed += result.failed
            cancelled += result.cancelled
            paused += result.paused
            missing += result.missing
        return ChildReconcileResult(
            inspected=inspected,
            completed=completed,
            failed=failed,
            cancelled=cancelled,
            paused=paused,
            missing=missing,
        )

    def _running_child_jobs(self, *, limit: int) -> list[AutomationJob]:
        jobs = self._store.list_jobs(state=JOB_RUNNING, limit=max(0, int(limit)))
        return [
            job
            for job in jobs
            if self._store.list_child_execution_edges(job.job_id)
            or job.ticket_flow_run_id
            or job.managed_thread_execution_id
            or job.pma_queue_item_id
        ]

    def _reconcile_child_job(self, job: AutomationJob) -> ChildReconcileResult:
        edges = self._store.list_child_execution_edges(job.job_id)
        if not edges:
            edges = self._backfill_legacy_child_edges(job)
        if edges:
            return self._reconcile_child_edges(job, edges)
        kind = str(job.executor.get("kind") or "").strip()
        if kind == "managed_thread_turn":
            return self._reconcile_managed_thread_job(job)
        return self._reconcile_ticket_flow_job(job)

    def _backfill_legacy_child_edges(
        self, job: AutomationJob
    ) -> list[AutomationChildExecutionEdge]:
        runtime = AutomationRuntimeContract(
            input_ref={"kind": "automation_job", "job_id": job.job_id}
        )
        edges: list[AutomationChildExecutionEdge] = []
        if job.managed_thread_execution_id:
            edge = self._store.upsert_child_execution_edge(
                AutomationChildExecutionEdge.create(
                    parent_job_id=job.job_id,
                    child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
                    child_id=job.managed_thread_execution_id,
                    requested_runtime=runtime,
                    actual_runtime=runtime,
                )
            )
            edges.append(edge)
        if job.ticket_flow_run_id:
            edge = self._store.upsert_child_execution_edge(
                AutomationChildExecutionEdge.create(
                    parent_job_id=job.job_id,
                    child_kind=AUTOMATION_CHILD_KIND_TICKET_FLOW,
                    child_id=job.ticket_flow_run_id,
                    requested_runtime=runtime,
                    actual_runtime=runtime,
                    terminal_mapping={
                        "succeeded": "succeeded",
                        "failed": "failed",
                        "interrupted": JOB_PAUSED,
                        "cancelled": "cancelled",
                    },
                )
            )
            edges.append(edge)
        return edges

    def _reconcile_child_edges(
        self, job: AutomationJob, edges: list[AutomationChildExecutionEdge]
    ) -> ChildReconcileResult:
        inspected = completed = failed = cancelled = paused = missing = 0
        reduce_hints: dict[str, object] = {}
        for edge in edges:
            if edge.terminal_state is not None:
                inspected += 1
                continue
            if edge.child_kind == AUTOMATION_CHILD_KIND_AGENT_TASK:
                result = self._reconcile_managed_thread_edge(job, edge, reduce_hints)
            elif edge.child_kind == AUTOMATION_CHILD_KIND_TICKET_FLOW:
                result = self._reconcile_ticket_flow_edge(job, edge, reduce_hints)
            elif edge.child_kind == AUTOMATION_CHILD_KIND_PMA_OPERATOR:
                result = self._reconcile_pma_operator_edge(job, edge, reduce_hints)
            else:
                result = ChildReconcileResult(inspected=1)
            inspected += result.inspected
            completed += result.completed
            failed += result.failed
            cancelled += result.cancelled
            paused += result.paused
            missing += result.missing
        reduced = self._store.reduce_parent_job_from_children(
            job.job_id,
            result_summary=(
                str(reduce_hints["result_summary"])
                if "result_summary" in reduce_hints
                else None
            ),
            error_text=(
                str(reduce_hints["error_text"])
                if "error_text" in reduce_hints
                else None
            ),
            execution_refs=_dict_hint(reduce_hints, "execution_refs"),
        )
        if reduced is not None and reduced.state != JOB_RUNNING:
            if reduced.state == "succeeded" and completed == 0:
                completed += 1
            elif reduced.state == "failed" and failed == 0:
                failed += 1
            elif reduced.state == "cancelled" and cancelled == 0:
                cancelled += 1
            elif reduced.state == "paused" and paused == 0:
                paused += 1
        return ChildReconcileResult(
            inspected=inspected,
            completed=completed,
            failed=failed,
            cancelled=cancelled,
            paused=paused,
            missing=missing,
        )

    def _reconcile_managed_thread_edge(
        self,
        job: AutomationJob,
        edge: AutomationChildExecutionEdge,
        reduce_hints: dict[str, object],
    ) -> ChildReconcileResult:
        latest_execution = _latest_thread_execution(
            hub_root=self._hub_root,
            thread_id=job.managed_thread_target_id,
            execution_id=edge.child_id,
            backend_thread_id=None,
        )
        if latest_execution is None:
            return ChildReconcileResult(inspected=1)
        status = str(latest_execution.get("status") or "").strip().lower()
        terminal_state = _managed_thread_terminal_state(status)
        if terminal_state is None:
            return ChildReconcileResult(inspected=1)
        self._store.mark_child_execution_terminal(
            edge.edge_id,
            terminal_state=terminal_state,
            terminal_event_id=str(
                latest_execution.get("execution_id") or edge.child_id
            ),
            actual_runtime={
                **edge.requested_runtime.to_dict(),
                "model": latest_execution.get("model_id"),
                "backend_runtime_id": latest_execution.get("backend_turn_id"),
            },
        )
        refs = {
            "managed_thread_target_id": latest_execution.get("thread_target_id")
            or job.managed_thread_target_id,
            "managed_thread_execution_id": latest_execution.get("execution_id")
            or edge.child_id,
        }
        reduce_hints["execution_refs"] = refs
        if terminal_state == "succeeded":
            reduce_hints["result_summary"] = _managed_thread_success_summary(
                latest_execution
            )
        elif terminal_state == "failed":
            reduce_hints["error_text"] = _managed_thread_error_summary(latest_execution)
        self._store.update_running_job(job.job_id, execution_refs=refs)
        return _terminal_result_counts(terminal_state)

    def _reconcile_ticket_flow_edge(
        self,
        job: AutomationJob,
        edge: AutomationChildExecutionEdge,
        reduce_hints: dict[str, object],
    ) -> ChildReconcileResult:
        record_info = self._ticket_flow_record_for_edge(job, edge)
        if isinstance(record_info, ChildReconcileResult):
            return record_info
        repo_path, record = record_info
        status = record.status
        if not isinstance(status, FlowRunStatus):
            status = FlowRunStatus(str(status))
        terminal_state = _ticket_flow_terminal_state(status)
        if terminal_state is None:
            return ChildReconcileResult(inspected=1)
        self._store.mark_child_execution_terminal(
            edge.edge_id,
            terminal_state=terminal_state,
            terminal_event_id=record.id,
        )
        refs = {"ticket_flow_run_id": record.id}
        if job.ticket_flow_repo_id:
            refs["ticket_flow_repo_id"] = job.ticket_flow_repo_id
        if job.ticket_flow_worktree_id:
            refs["ticket_flow_worktree_id"] = job.ticket_flow_worktree_id
        reduce_hints["execution_refs"] = refs
        if terminal_state == "succeeded":
            reduce_hints["result_summary"] = _ticket_flow_success_summary(
                repo_path, record
            )
        elif terminal_state == "failed":
            reduce_hints["error_text"] = (
                record.error_message or f"ticket-flow run failed: {record.id}"
            )
        elif terminal_state == "interrupted":
            reduce_hints["result_summary"] = f"ticket-flow run paused: {record.id}"
        self._store.update_running_job(job.job_id, execution_refs=refs)
        return _terminal_result_counts(terminal_state)

    def _ticket_flow_record_for_edge(
        self, job: AutomationJob, edge: AutomationChildExecutionEdge
    ) -> tuple[Path, FlowRunRecord] | ChildReconcileResult:
        scope = (
            edge.actual_runtime.workspace_scope
            if edge.actual_runtime is not None
            else edge.requested_runtime.workspace_scope
        ) or {}
        repo_id = (
            job.ticket_flow_worktree_id
            or job.ticket_flow_repo_id
            or str(scope.get("repo_id") or "").strip()
        )
        if not repo_id:
            self._store.mark_child_execution_terminal(
                edge.edge_id,
                terminal_state="failed",
                terminal_event_id=edge.child_id,
            )
            return ChildReconcileResult(inspected=1, missing=1, failed=1)
        repo_path = self._resolve_repo_path(repo_id)
        if repo_path is None:
            self._store.mark_child_execution_terminal(
                edge.edge_id,
                terminal_state="failed",
                terminal_event_id=edge.child_id,
            )
            return ChildReconcileResult(inspected=1, missing=1, failed=1)
        db_path = FlowStore.default_path(repo_path)
        if not db_path.exists():
            self._store.mark_child_execution_terminal(
                edge.edge_id,
                terminal_state="failed",
                terminal_event_id=edge.child_id,
            )
            return ChildReconcileResult(inspected=1, missing=1, failed=1)
        with FlowStore.connect_readonly(db_path) as flow_store:
            record = flow_store.get_flow_run(edge.child_id)
        if record is None:
            self._store.mark_child_execution_terminal(
                edge.edge_id,
                terminal_state="failed",
                terminal_event_id=edge.child_id,
            )
            return ChildReconcileResult(inspected=1, missing=1, failed=1)
        return repo_path, record

    def _reconcile_pma_operator_edge(
        self,
        job: AutomationJob,
        edge: AutomationChildExecutionEdge,
        reduce_hints: dict[str, object],
    ) -> ChildReconcileResult:
        if self._hub_root is None:
            return ChildReconcileResult(inspected=1)
        item = PmaQueue(Path(self._hub_root)).get_item_sync(edge.child_id)
        if item is None:
            self._store.mark_child_execution_terminal(
                edge.edge_id,
                terminal_state="failed",
                terminal_event_id=edge.child_id,
            )
            return ChildReconcileResult(inspected=1, missing=1, failed=1)
        if item.state in {QueueItemState.PENDING, QueueItemState.RUNNING}:
            return ChildReconcileResult(inspected=1)
        terminal_state = (
            "succeeded"
            if item.state in {QueueItemState.COMPLETED, QueueItemState.DEDUPED}
            else "cancelled" if item.state == QueueItemState.CANCELLED else "failed"
        )
        self._store.mark_child_execution_terminal(
            edge.edge_id,
            terminal_state=terminal_state,
            terminal_event_id=item.item_id,
        )
        self._store.update_running_job(
            job.job_id,
            execution_refs={
                "pma_lane_id": item.lane_id,
                "pma_queue_item_id": item.item_id,
            },
        )
        reduce_hints["execution_refs"] = {
            "pma_lane_id": item.lane_id,
            "pma_queue_item_id": item.item_id,
        }
        if terminal_state == "succeeded":
            reduce_hints["result_summary"] = f"PMA operator completed: {item.item_id}"
        elif terminal_state == "failed":
            reduce_hints["error_text"] = (
                item.error or f"PMA operator failed: {item.item_id}"
            )
        return _terminal_result_counts(terminal_state)

    def _reconcile_managed_thread_job(self, job: AutomationJob) -> ChildReconcileResult:
        snapshot = automation_execution_snapshot(job, hub_root=self._hub_root).to_dict()
        managed_thread = snapshot.get("managed_thread")
        if not isinstance(managed_thread, dict):
            self._store.fail_job(
                job.job_id,
                error_text="managed thread child run missing execution snapshot",
            )
            return ChildReconcileResult(missing=1)
        latest_execution = managed_thread.get("latest_execution")
        if not isinstance(latest_execution, dict):
            return ChildReconcileResult(inspected=1)
        status = str(latest_execution.get("status") or "").strip().lower()
        if status in {"pending", "queued", "running", "starting"}:
            return ChildReconcileResult(inspected=1)
        refs = {
            "managed_thread_target_id": managed_thread.get("thread_target_id"),
            "managed_thread_execution_id": latest_execution.get("execution_id"),
        }
        if status in {"error", "failed", "failure"}:
            self._store.fail_job(
                job.job_id,
                error_text=_managed_thread_error_summary(latest_execution),
                execution_refs=refs,
            )
            return ChildReconcileResult(inspected=1, failed=1)
        if status in {"completed", "succeeded", "success", "done", "ok"}:
            self._store.complete_job(
                job.job_id,
                result_summary=_managed_thread_success_summary(latest_execution),
                execution_refs=refs,
            )
            return ChildReconcileResult(inspected=1, completed=1)
        if status in {"cancelled", "canceled", "interrupted", "stopped", "archived"}:
            self._store.cancel_job(job.job_id, execution_refs=refs)
            return ChildReconcileResult(inspected=1, cancelled=1)
        return ChildReconcileResult(inspected=1)

    def _reconcile_ticket_flow_job(self, job: AutomationJob) -> ChildReconcileResult:
        repo_id = job.ticket_flow_worktree_id or job.ticket_flow_repo_id
        if not repo_id:
            self._store.fail_job(
                job.job_id,
                error_text="ticket_flow child run missing worktree repo id",
            )
            return ChildReconcileResult(missing=1)
        repo_path = self._resolve_repo_path(repo_id)
        if repo_path is None:
            self._store.fail_job(
                job.job_id,
                error_text=f"ticket_flow child worktree not found: {repo_id}",
            )
            return ChildReconcileResult(missing=1)
        db_path = FlowStore.default_path(repo_path)
        if not db_path.exists():
            self._store.fail_job(
                job.job_id,
                error_text=f"ticket_flow child flow store not found: {repo_id}",
            )
            return ChildReconcileResult(missing=1)
        with FlowStore.connect_readonly(db_path) as flow_store:
            record = flow_store.get_flow_run(str(job.ticket_flow_run_id))
        if record is None:
            self._store.fail_job(
                job.job_id,
                error_text=f"ticket_flow child run not found: {job.ticket_flow_run_id}",
            )
            return ChildReconcileResult(missing=1)
        return self._reconcile_ticket_flow_record(job, repo_path, record)

    def _reconcile_ticket_flow_record(
        self, job: AutomationJob, repo_path: Path, record: FlowRunRecord
    ) -> ChildReconcileResult:
        status = record.status
        if not isinstance(status, FlowRunStatus):
            status = FlowRunStatus(str(status))
        if status in {
            FlowRunStatus.PENDING,
            FlowRunStatus.RUNNING,
            FlowRunStatus.STOPPING,
        }:
            return ChildReconcileResult()
        if status == FlowRunStatus.PAUSED:
            self._store.pause_job(
                job.job_id,
                result_summary=f"ticket-flow run paused: {record.id}",
                execution_refs={"ticket_flow_run_id": record.id},
            )
            return ChildReconcileResult(paused=1)
        if status == FlowRunStatus.COMPLETED:
            summary = _ticket_flow_success_summary(repo_path, record)
            self._store.complete_job(
                job.job_id,
                result_summary=summary,
                execution_refs={"ticket_flow_run_id": record.id},
            )
            return ChildReconcileResult(completed=1)
        if status == FlowRunStatus.FAILED:
            self._store.fail_job(
                job.job_id,
                error_text=record.error_message
                or f"ticket-flow run failed: {record.id}",
            )
            return ChildReconcileResult(failed=1)
        if status in {FlowRunStatus.STOPPED, FlowRunStatus.SUPERSEDED}:
            self._store.cancel_job(
                job.job_id,
                execution_refs={"ticket_flow_run_id": record.id},
            )
            return ChildReconcileResult(cancelled=1)
        return ChildReconcileResult()


def _ticket_flow_success_summary(repo_path: Path, record: FlowRunRecord) -> str:
    from ..ticket_flow_projection import collect_ticket_flow_census

    census = collect_ticket_flow_census(repo_path)
    parts = [f"ticket-flow run completed: {record.id}"]
    if census.open_pr_url:
        parts.append(f"pr_url={census.open_pr_url}")
    return "; ".join(parts)


def _managed_thread_terminal_state(status: str) -> Optional[str]:
    if status in {"error", "failed", "failure"}:
        return "failed"
    if status in {"completed", "succeeded", "success", "done", "ok"}:
        return "succeeded"
    if status in {"cancelled", "canceled", "interrupted", "stopped", "archived"}:
        return "interrupted" if status == "interrupted" else "cancelled"
    return None


def _ticket_flow_terminal_state(status: FlowRunStatus) -> Optional[str]:
    if status == FlowRunStatus.COMPLETED:
        return "succeeded"
    if status == FlowRunStatus.FAILED:
        return "failed"
    if status in {FlowRunStatus.STOPPED, FlowRunStatus.SUPERSEDED}:
        return "cancelled"
    if status == FlowRunStatus.PAUSED:
        return "interrupted"
    return None


def _terminal_result_counts(terminal_state: str) -> ChildReconcileResult:
    if terminal_state == "succeeded":
        return ChildReconcileResult(inspected=1, completed=1)
    if terminal_state in {"cancelled", "interrupted"}:
        return ChildReconcileResult(inspected=1, cancelled=1)
    return ChildReconcileResult(inspected=1, failed=1)


def _dict_hint(hints: dict[str, object], key: str) -> Optional[dict[str, object]]:
    value = hints.get(key)
    if not isinstance(value, dict):
        return None
    return dict(value)


def _managed_thread_error_summary(execution: dict[str, object]) -> str:
    error = execution.get("error_text")
    if isinstance(error, str) and error.strip():
        return error.strip()[:500]
    execution_id = execution.get("execution_id")
    return f"Managed automation thread failed: {execution_id or 'unknown execution'}"


def _managed_thread_success_summary(execution: dict[str, object]) -> str:
    execution_id = execution.get("execution_id")
    return f"Managed automation thread completed: {execution_id or 'unknown execution'}"


__all__ = [
    "AutomationChildRunReconciler",
    "ChildReconcileResult",
    "RepoPathResolver",
]
