from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..flows.models import FlowRunRecord, FlowRunStatus
from ..flows.store import FlowStore
from ..pma_queue import PmaQueue, QueueItemState
from .execution_graph import automation_execution_snapshot
from .models import JOB_RUNNING, AutomationJob
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
            if (
                str(job.executor.get("kind") or "").strip() == "ticket_flow"
                and job.ticket_flow_run_id
            )
            or (
                str(job.executor.get("kind") or "").strip() == "pma_turn"
                and job.pma_queue_item_id
            )
        ]

    def _reconcile_child_job(self, job: AutomationJob) -> ChildReconcileResult:
        kind = str(job.executor.get("kind") or "").strip()
        if kind == "pma_turn":
            return self._reconcile_pma_queue_job(job)
        return self._reconcile_ticket_flow_job(job)

    def _reconcile_pma_queue_job(self, job: AutomationJob) -> ChildReconcileResult:
        if self._hub_root is None:
            self._store.fail_job(
                job.job_id,
                error_text="pma_queue child reconcile missing hub root",
            )
            return ChildReconcileResult(missing=1)
        item = PmaQueue(Path(self._hub_root)).get_item_sync(str(job.pma_queue_item_id))
        if item is None:
            child_result = self._reconcile_completed_pma_child(job, None)
            if child_result.changed or child_result.inspected:
                return child_result
            self._store.fail_job(
                job.job_id,
                error_text=f"pma queue item not found: {job.pma_queue_item_id}",
            )
            return ChildReconcileResult(missing=1)
        if item.state in {QueueItemState.PENDING, QueueItemState.RUNNING}:
            return ChildReconcileResult()
        refs = {"pma_lane_id": item.lane_id, "pma_queue_item_id": item.item_id}
        if item.state == QueueItemState.COMPLETED:
            result = item.result if isinstance(item.result, dict) else {}
            status = str(result.get("status") or "").strip().lower()
            if status in {"error", "failed", "failure"}:
                self._store.fail_job(
                    job.job_id,
                    error_text=_pma_queue_error_summary(item),
                    execution_refs=refs,
                )
                return ChildReconcileResult(failed=1)
            child_result = self._reconcile_completed_pma_child(job, item)
            if child_result.changed or child_result.inspected:
                return child_result
            self._store.complete_job(
                job.job_id,
                result_summary=_pma_queue_success_summary(item),
                execution_refs=refs,
            )
            return ChildReconcileResult(completed=1)
        if item.state == QueueItemState.CANCELLED:
            self._store.cancel_job(job.job_id, execution_refs=refs)
            return ChildReconcileResult(cancelled=1)
        self._store.fail_job(
            job.job_id,
            error_text=_pma_queue_error_summary(item),
            execution_refs=refs,
        )
        return ChildReconcileResult(failed=1)

    def _reconcile_completed_pma_child(
        self, job: AutomationJob, item: object | None
    ) -> ChildReconcileResult:
        snapshot = automation_execution_snapshot(job, hub_root=self._hub_root).to_dict()
        managed_thread = snapshot.get("managed_thread")
        if not isinstance(managed_thread, dict):
            return ChildReconcileResult()
        latest_execution = managed_thread.get("latest_execution")
        if not isinstance(latest_execution, dict):
            return ChildReconcileResult(inspected=1)
        status = str(latest_execution.get("status") or "").strip().lower()
        if status in {"pending", "queued", "running", "starting"}:
            return ChildReconcileResult(inspected=1)
        refs = {
            "pma_lane_id": getattr(item, "lane_id", None) or job.pma_lane_id,
            "pma_queue_item_id": getattr(item, "item_id", None)
            or job.pma_queue_item_id,
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
        if status in {"completed", "succeeded", "success", "done"}:
            self._store.complete_job(
                job.job_id,
                result_summary=(
                    _pma_queue_success_summary(item)
                    if item is not None
                    else _managed_thread_success_summary(latest_execution)
                ),
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


def _pma_queue_success_summary(item: object) -> str:
    result = getattr(item, "result", None)
    if isinstance(result, dict):
        for key in ("summary", "detail", "message"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    return f"PMA automation turn completed: {getattr(item, 'item_id', '')}"


def _pma_queue_error_summary(item: object) -> str:
    error = getattr(item, "error", None)
    if isinstance(error, str) and error.strip():
        return error.strip()[:500]
    result = getattr(item, "result", None)
    if isinstance(result, dict):
        for key in ("detail", "error", "message", "summary"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    return f"PMA automation turn failed: {getattr(item, 'item_id', '')}"


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
