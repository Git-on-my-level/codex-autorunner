from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..orchestration.sqlite import open_orchestration_sqlite
from ..pma_queue import PmaQueue, PmaQueueItem, PmaQueueRepository
from .models import AutomationJob


@dataclass(frozen=True)
class AutomationExecutionSnapshot:
    """Read-side projection of an automation job and its child execution."""

    job_id: str
    primary_child_kind: str
    target_href: Optional[str] = None
    chat_href: Optional[str] = None
    managed_thread: Optional[dict[str, Any]] = None
    pma_queue: Optional[dict[str, Any]] = None
    ticket_flow: Optional[dict[str, Any]] = None
    publish_operation: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "primary_child_kind": self.primary_child_kind,
            "target_href": self.target_href,
            "chat_href": self.chat_href,
            "managed_thread": self.managed_thread,
            "pma_queue": self.pma_queue,
            "ticket_flow": self.ticket_flow,
            "publish_operation": self.publish_operation,
        }


def automation_execution_snapshot(
    job: AutomationJob,
    *,
    hub_root: Optional[Path] = None,
    cache: Optional["_AutomationExecutionSnapshotCache"] = None,
) -> AutomationExecutionSnapshot:
    pma_item = _pma_item_for_job(job, hub_root=hub_root, cache=cache)
    pma_queue = _pma_queue_snapshot(pma_item)
    managed_thread = _managed_thread_snapshot(
        job, pma_item=pma_item, hub_root=hub_root, cache=cache
    )
    ticket_flow = _ticket_flow_snapshot(job)
    publish_operation = _publish_operation_snapshot(job)
    primary_child_kind = _primary_child_kind(
        job,
        pma_queue=pma_queue,
        managed_thread=managed_thread,
        ticket_flow=ticket_flow,
        publish_operation=publish_operation,
    )
    chat_id = _dict_string(managed_thread, "thread_target_id")
    target_href = _target_href(chat_id=chat_id, ticket_flow=ticket_flow)
    return AutomationExecutionSnapshot(
        job_id=job.job_id,
        primary_child_kind=primary_child_kind,
        target_href=target_href,
        chat_href=f"/chats/{chat_id}" if chat_id else None,
        managed_thread=managed_thread,
        pma_queue=pma_queue,
        ticket_flow=ticket_flow,
        publish_operation=publish_operation,
    )


def automation_execution_snapshots_by_job_id(
    jobs: list[AutomationJob], *, hub_root: Optional[Path] = None
) -> dict[str, AutomationExecutionSnapshot]:
    if not jobs:
        return {}
    if hub_root is None:
        return {
            job.job_id: automation_execution_snapshot(job, hub_root=None)
            for job in jobs
        }
    cache = _AutomationExecutionSnapshotCache(hub_root)
    try:
        return {
            job.job_id: automation_execution_snapshot(job, hub_root=hub_root, cache=cache)
            for job in jobs
        }
    finally:
        cache.close()


@dataclass
class _AutomationExecutionSnapshotCache:
    hub_root: Path
    _repository: PmaQueueRepository = field(init=False)
    _conn: Any = field(default=None, init=False, repr=False)
    _conn_cm: Any = field(default=None, init=False, repr=False)
    _pma_items: dict[str, Optional[PmaQueueItem]] = field(default_factory=dict)
    _thread_prefixes: dict[str, Optional[str]] = field(default_factory=dict)
    _latest_executions: dict[
        tuple[Optional[str], Optional[str], Optional[str]], Optional[dict[str, Any]]
    ] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._repository = PmaQueueRepository(self.hub_root)

    def close(self) -> None:
        if self._conn_cm is not None:
            self._conn_cm.__exit__(None, None, None)
            self._conn_cm = None
            self._conn = None

    def _connection(self) -> Any:
        if self._conn is None:
            self._conn_cm = open_orchestration_sqlite(
                self.hub_root, durable=True, migrate=False
            )
            self._conn = self._conn_cm.__enter__()
        return self._conn

    def pma_item(self, item_id: str) -> Optional[PmaQueueItem]:
        normalized = str(item_id or "").strip()
        if not normalized:
            return None
        if normalized in self._pma_items:
            return self._pma_items[normalized]
        row = self._connection().execute(
            """
            SELECT *
              FROM orch_queue_items
             WHERE queue_item_id = ?
             LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        item = self._repository.row_to_item(row) if row is not None else None
        self._pma_items[normalized] = item
        return item

    def resolve_thread_prefix(self, prefix: str) -> Optional[str]:
        normalized = prefix.strip()
        if not normalized:
            return None
        if normalized in self._thread_prefixes:
            return self._thread_prefixes[normalized]
        resolved = _resolve_thread_prefix_with_conn(self._connection(), normalized)
        self._thread_prefixes[normalized] = resolved
        return resolved

    def latest_thread_execution(
        self,
        *,
        thread_id: Optional[str],
        execution_id: Optional[str],
        backend_thread_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        key = (thread_id, execution_id, backend_thread_id)
        if key in self._latest_executions:
            return self._latest_executions[key]
        latest = _latest_thread_execution_with_conn(
            self._connection(),
            thread_id=thread_id,
            execution_id=execution_id,
            backend_thread_id=backend_thread_id,
        )
        self._latest_executions[key] = latest
        return latest


def _pma_item_for_job(
    job: AutomationJob,
    *,
    hub_root: Optional[Path],
    cache: Optional[_AutomationExecutionSnapshotCache] = None,
) -> Optional[PmaQueueItem]:
    if not job.pma_queue_item_id:
        return None
    item_id = str(job.pma_queue_item_id)
    if cache is not None:
        return cache.pma_item(item_id)
    if hub_root is None:
        return None
    return PmaQueue(Path(hub_root)).get_item_sync(item_id)


def _pma_queue_snapshot(item: Optional[PmaQueueItem]) -> Optional[dict[str, Any]]:
    if item is None:
        return None
    return {
        "item_id": item.item_id,
        "lane_id": item.lane_id,
        "state": item.state.value,
        "started_at": item.started_at,
        "finished_at": item.finished_at,
        "error": item.error,
        "result": item.result or {},
    }


def _managed_thread_snapshot(
    job: AutomationJob,
    *,
    pma_item: Optional[PmaQueueItem],
    hub_root: Optional[Path],
    cache: Optional[_AutomationExecutionSnapshotCache] = None,
) -> Optional[dict[str, Any]]:
    thread_id = job.managed_thread_target_id or _managed_thread_id_from_pma_item(
        pma_item, hub_root=hub_root, cache=cache
    )
    execution_id = job.managed_thread_execution_id
    backend_thread_id = _pma_result_string(
        pma_item, "backend_thread_id"
    ) or _pma_result_string(pma_item, "thread_id")
    if not thread_id and not execution_id:
        return None
    if cache is not None:
        latest_execution = cache.latest_thread_execution(
            thread_id=thread_id,
            execution_id=execution_id,
            backend_thread_id=backend_thread_id,
        )
    else:
        latest_execution = _latest_thread_execution(
            hub_root=hub_root,
            thread_id=thread_id,
            execution_id=execution_id,
            backend_thread_id=backend_thread_id,
        )
    return {
        "thread_target_id": thread_id,
        "execution_id": execution_id,
        "backend_thread_id": backend_thread_id,
        "latest_execution": latest_execution,
    }


def _ticket_flow_snapshot(job: AutomationJob) -> Optional[dict[str, Any]]:
    if not (
        job.ticket_flow_run_id or job.ticket_flow_worktree_id or job.ticket_flow_repo_id
    ):
        return None
    return {
        "repo_id": job.ticket_flow_repo_id,
        "run_id": job.ticket_flow_run_id,
        "worktree_id": job.ticket_flow_worktree_id,
    }


def _publish_operation_snapshot(job: AutomationJob) -> Optional[dict[str, Any]]:
    if not job.publish_operation_id:
        return None
    return {"operation_id": job.publish_operation_id}


def _primary_child_kind(
    job: AutomationJob,
    *,
    pma_queue: Optional[dict[str, Any]],
    managed_thread: Optional[dict[str, Any]],
    ticket_flow: Optional[dict[str, Any]],
    publish_operation: Optional[dict[str, Any]],
) -> str:
    kind = str(job.executor.get("kind") or "").strip()
    if kind == "pma_turn" and pma_queue is not None:
        return "pma_queue"
    if managed_thread is not None:
        return "managed_thread"
    if ticket_flow is not None:
        return "ticket_flow"
    if publish_operation is not None:
        return "publish_operation"
    if pma_queue is not None:
        return "pma_queue"
    return "none"


def _target_href(
    *, chat_id: Optional[str], ticket_flow: Optional[dict[str, Any]]
) -> Optional[str]:
    if chat_id:
        return f"/chats/{chat_id}"
    worktree_id = _dict_string(ticket_flow, "worktree_id")
    if worktree_id:
        return f"/worktrees/{worktree_id}/tickets"
    return None


def _managed_thread_id_from_pma_item(
    item: Optional[PmaQueueItem],
    *,
    hub_root: Optional[Path],
    cache: Optional[_AutomationExecutionSnapshotCache] = None,
) -> Optional[str]:
    explicit = (
        _pma_result_string(item, "managed_thread_id")
        or _pma_result_string(item, "managedThreadId")
        or _pma_result_string(item, "thread_target_id")
        or _pma_result_string(item, "threadTargetId")
    )
    if explicit:
        return explicit
    prefix = _thread_prefix_from_pma_result(item)
    if not prefix or hub_root is None:
        return prefix
    if cache is not None:
        return cache.resolve_thread_prefix(prefix) or prefix
    return _resolve_thread_prefix(Path(hub_root), prefix) or prefix


def _thread_prefix_from_pma_result(item: Optional[PmaQueueItem]) -> Optional[str]:
    result = item.result if item is not None and isinstance(item.result, dict) else {}
    message = result.get("message")
    if isinstance(message, str):
        match = re.search(r"Thread\s+`([0-9a-fA-F]{8,36})`", message)
        if match:
            return match.group(1)
    raw_events = result.get("raw_events")
    if isinstance(raw_events, list):
        for event in raw_events:
            output = _nested_string(
                event,
                ("message", "params", "properties", "part", "state", "output"),
            )
            match = re.search(
                r"\b([0-9a-fA-F]{8}-[0-9a-fA-F-]{27}|[0-9a-fA-F]{8})\b",
                output or "",
            )
            if match:
                return match.group(1)
    return None


def _resolve_thread_prefix(hub_root: Path, prefix: str) -> Optional[str]:
    normalized = prefix.strip()
    if not normalized:
        return None
    try:
        with open_orchestration_sqlite(hub_root, durable=True, migrate=False) as conn:
            return _resolve_thread_prefix_with_conn(conn, normalized)
    except Exception:
        return None


def _resolve_thread_prefix_with_conn(conn: Any, prefix: str) -> Optional[str]:
    row = conn.execute(
        """
        SELECT thread_target_id
          FROM orch_thread_targets
         WHERE thread_target_id = ?
            OR thread_target_id LIKE ?
         ORDER BY updated_at DESC
         LIMIT 1
        """,
        (prefix, f"{prefix}%"),
    ).fetchone()
    if row is None:
        return None
    return str(row["thread_target_id"] or "").strip() or None


def _latest_thread_execution(
    *,
    hub_root: Optional[Path],
    thread_id: Optional[str],
    execution_id: Optional[str],
    backend_thread_id: Optional[str],
) -> Optional[dict[str, Any]]:
    if hub_root is None or not (thread_id or execution_id or backend_thread_id):
        return None
    try:
        with open_orchestration_sqlite(
            Path(hub_root), durable=True, migrate=False
        ) as conn:
            return _latest_thread_execution_with_conn(
                conn,
                thread_id=thread_id,
                execution_id=execution_id,
                backend_thread_id=backend_thread_id,
            )
    except Exception:
        return None


def _latest_thread_execution_with_conn(
    conn: Any,
    *,
    thread_id: Optional[str],
    execution_id: Optional[str],
    backend_thread_id: Optional[str],
) -> Optional[dict[str, Any]]:
    if not (thread_id or execution_id or backend_thread_id):
        return None
    row = conn.execute(
        """
        SELECT execution_id,
               thread_target_id,
               status,
               backend_turn_id,
               error_text,
               assistant_text,
               model_id,
               started_at,
               finished_at,
               created_at
          FROM orch_thread_executions
         WHERE (? IS NOT NULL AND execution_id = ?)
            OR (? IS NOT NULL AND thread_target_id = ?)
            OR (? IS NOT NULL AND backend_turn_id LIKE ?)
         ORDER BY COALESCE(finished_at, started_at, created_at) DESC
         LIMIT 1
        """,
        (
            execution_id,
            execution_id,
            thread_id,
            thread_id,
            backend_thread_id,
            f"{backend_thread_id}:%" if backend_thread_id else None,
        ),
    ).fetchone()
    if row is None:
        return None
    return {
        "execution_id": row["execution_id"],
        "thread_target_id": row["thread_target_id"],
        "status": row["status"],
        "backend_turn_id": row["backend_turn_id"],
        "error_text": row["error_text"],
        "assistant_text": row["assistant_text"],
        "model_id": row["model_id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
    }


def _pma_result_string(item: Optional[PmaQueueItem], key: str) -> Optional[str]:
    result = item.result if item is not None and isinstance(item.result, dict) else {}
    value = result.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _dict_string(data: Optional[dict[str, Any]], key: str) -> Optional[str]:
    value = data.get(key) if data else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _nested_string(data: Any, path: tuple[str, ...]) -> Optional[str]:
    value = data
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = [
    "AutomationExecutionSnapshot",
    "automation_execution_snapshot",
    "automation_execution_snapshots_by_job_id",
]
