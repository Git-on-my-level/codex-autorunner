from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..flows.models import FlowRunRecord, FlowRunStatus
from ..flows.store import FlowStore
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
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._store = store
        self._resolve_repo_path = resolve_repo_path
        self._logger = logger or logging.getLogger(__name__)

    def reconcile_running_jobs(self, *, limit: int = 100) -> ChildReconcileResult:
        inspected = completed = failed = cancelled = paused = missing = 0
        for job in self._running_ticket_flow_jobs(limit=limit):
            inspected += 1
            try:
                result = self._reconcile_ticket_flow_job(job)
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

    def _running_ticket_flow_jobs(self, *, limit: int) -> list[AutomationJob]:
        jobs = self._store.list_jobs(state=JOB_RUNNING, limit=max(0, int(limit)))
        return [
            job
            for job in jobs
            if str(job.executor.get("kind") or "").strip() == "ticket_flow"
            and job.ticket_flow_run_id
        ]

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
            self._store.cancel_job(job.job_id)
            return ChildReconcileResult(cancelled=1)
        return ChildReconcileResult()


def _ticket_flow_success_summary(repo_path: Path, record: FlowRunRecord) -> str:
    from ..ticket_flow_projection import collect_ticket_flow_census

    census = collect_ticket_flow_census(repo_path)
    parts = [f"ticket-flow run completed: {record.id}"]
    if census.open_pr_url:
        parts.append(f"pr_url={census.open_pr_url}")
    return "; ".join(parts)


__all__ = [
    "AutomationChildRunReconciler",
    "ChildReconcileResult",
    "RepoPathResolver",
]
