from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..flows.models import FlowRunRecord, FlowRunStatus
from ..flows.store import FlowStore
from ..managed_thread_store import ManagedThreadStore
from ..orchestration.sqlite import open_orchestration_sqlite
from ..pma_queue import PmaQueue, QueueItemState
from .execution_graph import _latest_thread_execution
from .models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    AUTOMATION_CHILD_KIND_PMA_OPERATOR,
    AUTOMATION_CHILD_KIND_TICKET_FLOW,
    JOB_RUNNING,
    AutomationChildExecutionEdge,
    AutomationJob,
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
        return self._store.list_jobs(state=JOB_RUNNING, limit=max(0, int(limit)))

    def _reconcile_child_job(self, job: AutomationJob) -> ChildReconcileResult:
        edges = self._store.list_child_execution_edges(job.job_id)
        if edges:
            return self._reconcile_child_edges(job, edges)
        self._store.fail_job(
            job.job_id,
            error_text=(
                "running automation job has no durable authoritative child edge; "
                "run explicit automation executor migration"
            ),
        )
        return ChildReconcileResult(inspected=1, failed=1)

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
            thread_id=_edge_thread_target_id(edge),
            execution_id=edge.child_id,
            backend_thread_id=None,
        )
        if latest_execution is None:
            return ChildReconcileResult(inspected=1)
        status = str(latest_execution.get("status") or "").strip().lower()
        terminal_state = _managed_thread_terminal_state(status)
        if terminal_state is None:
            return ChildReconcileResult(inspected=1)
        actual_runtime = _actual_runtime_from_execution(
            latest_execution,
            fallback=edge.requested_runtime.to_dict(),
        )
        if not actual_runtime:
            actual_runtime = _actual_runtime_from_thread(
                self._hub_root,
                latest_execution.get("thread_target_id")
                or _edge_thread_target_id(edge),
                fallback=edge.requested_runtime.to_dict(),
            )
        actual_runtime["backend_runtime_id"] = actual_runtime.get(
            "backend_runtime_id"
        ) or latest_execution.get("backend_turn_id")
        transcript_model = _actual_model_from_transcript(
            self._hub_root,
            latest_execution.get("execution_id"),
            latest_execution.get("transcript_mirror_id"),
        )
        if transcript_model is not None:
            actual_runtime["model"] = transcript_model
        self._store.mark_child_execution_terminal(
            edge.edge_id,
            terminal_state=terminal_state,
            terminal_event_id=str(
                latest_execution.get("execution_id") or edge.child_id
            ),
            actual_runtime=actual_runtime,
        )
        if terminal_state == "succeeded":
            reduce_hints["result_summary"] = _managed_thread_success_summary(
                latest_execution
            )
        elif terminal_state == "failed":
            reduce_hints["error_text"] = _managed_thread_error_summary(latest_execution)
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
        return _terminal_result_counts(terminal_state)

    def _ticket_flow_record_for_edge(
        self, job: AutomationJob, edge: AutomationChildExecutionEdge
    ) -> tuple[Path, FlowRunRecord] | ChildReconcileResult:
        scope = (
            edge.actual_runtime.workspace_scope
            if edge.actual_runtime is not None
            else edge.requested_runtime.workspace_scope
        ) or {}
        repo_id = str(scope.get("repo_id") or "").strip()
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
        if terminal_state == "succeeded":
            reduce_hints["result_summary"] = f"PMA operator completed: {item.item_id}"
        elif terminal_state == "failed":
            reduce_hints["error_text"] = (
                item.error or f"PMA operator failed: {item.item_id}"
            )
        return _terminal_result_counts(terminal_state)


def _edge_thread_target_id(edge: AutomationChildExecutionEdge) -> Optional[str]:
    runtime = edge.actual_runtime or edge.requested_runtime
    scope = runtime.workspace_scope or {}
    value = scope.get("thread_target_id") or scope.get("thread_id")
    if value is None:
        return None
    return str(value).strip() or None


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
    if terminal_state == "cancelled":
        return ChildReconcileResult(inspected=1, cancelled=1)
    if terminal_state == "interrupted":
        # Ticket-flow "interrupted" maps to JOB_PAUSED; count paused here so we
        # do not also treat it as cancelled (legacy mapping used both).
        return ChildReconcileResult(inspected=1, paused=1)
    return ChildReconcileResult(inspected=1, failed=1)


def _dict_hint(hints: dict[str, object], key: str) -> Optional[dict[str, object]]:
    value = hints.get(key)
    if not isinstance(value, dict):
        return None
    return dict(value)


def _actual_runtime_from_thread(
    hub_root: Optional[Path],
    thread_id: object,
    *,
    fallback: dict[str, object],
) -> dict[str, object]:
    if hub_root is None or not isinstance(thread_id, str) or not thread_id.strip():
        return dict(fallback)
    thread = ManagedThreadStore(hub_root).get_thread(thread_id.strip())
    if not isinstance(thread, dict):
        return dict(fallback)
    raw_metadata = thread.get("metadata")
    metadata: dict[object, object] = (
        raw_metadata if isinstance(raw_metadata, dict) else {}
    )
    raw_backend_binding = thread.get("backend_binding")
    backend_binding: dict[object, object] = (
        raw_backend_binding if isinstance(raw_backend_binding, dict) else {}
    )
    actual = dict(fallback)
    actual["agent"] = thread.get("agent") or thread.get("agent_id")
    actual["profile"] = thread.get("agent_profile") or metadata.get("agent_profile")
    actual["backend_runtime_id"] = (
        thread.get("backend_runtime_instance_id")
        or backend_binding.get("backend_runtime_instance_id")
        or actual.get("backend_runtime_id")
    )
    return actual


def _actual_runtime_from_execution(
    execution: dict[str, object],
    *,
    fallback: dict[str, object],
) -> dict[str, object]:
    runtime_identity = execution.get("runtime_identity")
    if not isinstance(runtime_identity, dict):
        return {}
    effective = runtime_identity.get("effective")
    if not isinstance(effective, dict):
        return {}
    actual = dict(fallback)
    actual.update(
        {
            "agent": effective.get("logical_agent") or effective.get("agent"),
            "model": effective.get("canonical_model_label") or effective.get("model"),
            "profile": effective.get("profile"),
            "reasoning": effective.get("reasoning"),
            "approval_policy": effective.get("approval_policy"),
            "sandbox_policy": effective.get("sandbox_policy"),
            "workspace_scope": effective.get("workspace_scope"),
            "backend_runtime_id": effective.get("backend_runtime_id"),
            "provider_payload": effective.get("provider_payload"),
        }
    )
    return {key: value for key, value in actual.items() if value is not None}


def _actual_model_from_transcript(
    hub_root: Optional[Path], execution_id: object, transcript_mirror_id: object
) -> Optional[str]:
    if hub_root is None:
        return None
    clauses: list[str] = []
    params: list[object] = []
    if isinstance(transcript_mirror_id, str) and transcript_mirror_id.strip():
        clauses.append("transcript_mirror_id = ?")
        params.append(transcript_mirror_id.strip())
    if isinstance(execution_id, str) and execution_id.strip():
        clauses.append("execution_id = ?")
        params.append(execution_id.strip())
    if not clauses:
        return None
    with open_orchestration_sqlite(Path(hub_root), durable=True, migrate=False) as conn:
        row = conn.execute(
            f"""
            SELECT model_id
              FROM orch_transcript_mirrors
             WHERE ({" OR ".join(clauses)})
               AND model_id IS NOT NULL
               AND TRIM(model_id) != ''
             ORDER BY updated_at DESC, created_at DESC
             LIMIT 1
            """,
            tuple(params),
        ).fetchone()
    if row is None:
        return None
    model = row["model_id"]
    return model.strip() if isinstance(model, str) and model.strip() else None


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
