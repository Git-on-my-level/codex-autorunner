from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..orchestration.sqlite import open_orchestration_sqlite
from ..pma_queue import PmaQueue, PmaQueueItem, PmaQueueRepository
from ..text_utils import _json_loads_object
from .models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    AUTOMATION_CHILD_KIND_PMA_OPERATOR,
    AUTOMATION_CHILD_KIND_PUBLISH_OPERATION,
    AUTOMATION_CHILD_KIND_TICKET_FLOW,
    AutomationChildExecutionEdge,
    AutomationJob,
    normalize_bool,
)


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
    child_edges: Optional[list[AutomationChildExecutionEdge]] = None,
) -> AutomationExecutionSnapshot:
    edges = (
        child_edges
        if child_edges is not None
        else (
            cache.child_edges(job.job_id)
            if cache is not None
            else _load_child_edges(hub_root, job.job_id)
        )
    )
    pma_item = _pma_item_for_edges(edges, hub_root=hub_root, cache=cache)
    pma_queue = _pma_queue_snapshot(pma_item)
    managed_thread = _managed_thread_snapshot(
        edges, pma_item=pma_item, hub_root=hub_root, cache=cache
    )
    ticket_flow = _ticket_flow_snapshot(edges)
    publish_operation = _publish_operation_snapshot(edges)
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
            job.job_id: automation_execution_snapshot(
                job, hub_root=hub_root, cache=cache
            )
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
    _child_edges: dict[str, list[AutomationChildExecutionEdge]] = field(
        default_factory=dict
    )
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
        row = (
            self._connection()
            .execute(
                """
            SELECT *
              FROM orch_queue_items
             WHERE queue_item_id = ?
             LIMIT 1
            """,
                (normalized,),
            )
            .fetchone()
        )
        item = self._repository.row_to_item(row) if row is not None else None
        self._pma_items[normalized] = item
        return item

    def child_edges(self, job_id: str) -> list[AutomationChildExecutionEdge]:
        normalized = str(job_id or "").strip()
        if not normalized:
            return []
        if normalized in self._child_edges:
            return self._child_edges[normalized]
        rows = (
            self._connection()
            .execute(
                """
            SELECT *
              FROM orch_automation_child_execution_edges
             WHERE parent_job_id = ?
             ORDER BY created_at ASC, edge_id ASC
            """,
                (normalized,),
            )
            .fetchall()
        )
        edges = [_row_to_child_execution_edge(row) for row in rows]
        self._child_edges[normalized] = edges
        return edges

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


def _pma_item_for_edges(
    edges: list[AutomationChildExecutionEdge],
    *,
    hub_root: Optional[Path],
    cache: Optional[_AutomationExecutionSnapshotCache] = None,
) -> Optional[PmaQueueItem]:
    edge = _latest_child_edge_for_kind(edges, AUTOMATION_CHILD_KIND_PMA_OPERATOR)
    if edge is None:
        return None
    item_id = str(edge.child_id)
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
    edges: list[AutomationChildExecutionEdge],
    *,
    pma_item: Optional[PmaQueueItem],
    hub_root: Optional[Path],
    cache: Optional[_AutomationExecutionSnapshotCache] = None,
) -> Optional[dict[str, Any]]:
    edge = _latest_child_edge_for_kind(edges, AUTOMATION_CHILD_KIND_AGENT_TASK)
    thread_id = _edge_scope_string(
        edge, "thread_target_id"
    ) or _managed_thread_id_from_pma_item(pma_item, hub_root=hub_root, cache=cache)
    execution_id = edge.child_id if edge is not None else None
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
    if latest_execution is not None:
        thread_id = thread_id or latest_execution.get("thread_target_id")
    return {
        "thread_target_id": thread_id,
        "execution_id": execution_id,
        "backend_thread_id": backend_thread_id,
        "latest_execution": latest_execution,
    }


def _ticket_flow_snapshot(
    edges: list[AutomationChildExecutionEdge],
) -> Optional[dict[str, Any]]:
    edge = _latest_child_edge_for_kind(edges, AUTOMATION_CHILD_KIND_TICKET_FLOW)
    if edge is None:
        return None
    scope = _edge_scope(edge)
    return {
        "repo_id": scope.get("base_repo_id"),
        "run_id": edge.child_id,
        "worktree_id": scope.get("repo_id"),
    }


def _publish_operation_snapshot(
    edges: list[AutomationChildExecutionEdge],
) -> Optional[dict[str, Any]]:
    edge = _latest_child_edge_for_kind(edges, AUTOMATION_CHILD_KIND_PUBLISH_OPERATION)
    if edge is None:
        return None
    return {"operation_id": edge.child_id}


def _primary_child_kind(
    job: AutomationJob,
    *,
    pma_queue: Optional[dict[str, Any]],
    managed_thread: Optional[dict[str, Any]],
    ticket_flow: Optional[dict[str, Any]],
    publish_operation: Optional[dict[str, Any]],
) -> str:
    if managed_thread is not None:
        return "managed_thread"
    if ticket_flow is not None:
        return "ticket_flow"
    if publish_operation is not None:
        return "publish_operation"
    if pma_queue is not None:
        return "pma_queue"
    return "none"


def _latest_child_edge_for_kind(
    edges: list[AutomationChildExecutionEdge], child_kind: str
) -> Optional[AutomationChildExecutionEdge]:
    """Latest edge for ``child_kind`` when ``edges`` is oldest-first (DB order)."""
    latest: Optional[AutomationChildExecutionEdge] = None
    for edge in edges:
        if edge.child_kind == child_kind:
            latest = edge
    return latest


def _edge_scope(edge: AutomationChildExecutionEdge) -> dict[str, Any]:
    runtime = edge.actual_runtime or edge.requested_runtime
    return runtime.workspace_scope or {}


def _edge_scope_string(
    edge: Optional[AutomationChildExecutionEdge], key: str
) -> Optional[str]:
    if edge is None:
        return None
    value = _edge_scope(edge).get(key)
    if value is None:
        return None
    return str(value).strip() or None


def _load_child_edges(
    hub_root: Optional[Path], job_id: str
) -> list[AutomationChildExecutionEdge]:
    if hub_root is None:
        return []
    try:
        with open_orchestration_sqlite(
            Path(hub_root), durable=True, migrate=False
        ) as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_automation_child_execution_edges
                 WHERE parent_job_id = ?
                 ORDER BY created_at ASC, edge_id ASC
                """,
                (job_id,),
            ).fetchall()
    except Exception:
        return []
    return [_row_to_child_execution_edge(row) for row in rows]


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
    _ = hub_root, cache
    explicit = (
        _pma_result_string(item, "managed_thread_id")
        or _pma_result_string(item, "managedThreadId")
        or _pma_result_string(item, "thread_target_id")
        or _pma_result_string(item, "threadTargetId")
    )
    return explicit


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
    if execution_id:
        row = conn.execute(
            """
            SELECT execution_id,
                   thread_target_id,
                   status,
                   backend_turn_id,
                   error_text,
                   assistant_text,
                   model_id,
                   transcript_mirror_id,
                   started_at,
                   finished_at,
                   created_at,
                   runtime_identity_json
              FROM orch_thread_executions
             WHERE execution_id = ?
             LIMIT 1
            """,
            (execution_id,),
        ).fetchone()
        if row is not None:
            return _thread_execution_row_to_dict(row)
    row = conn.execute(
        """
        SELECT execution_id,
               thread_target_id,
               status,
               backend_turn_id,
               error_text,
               assistant_text,
               model_id,
               transcript_mirror_id,
               started_at,
               finished_at,
               created_at,
               runtime_identity_json
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
    return _thread_execution_row_to_dict(row)


def _thread_execution_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "execution_id": row["execution_id"],
        "thread_target_id": row["thread_target_id"],
        "status": row["status"],
        "backend_turn_id": row["backend_turn_id"],
        "error_text": row["error_text"],
        "assistant_text": row["assistant_text"],
        "model_id": row["model_id"],
        "transcript_mirror_id": row["transcript_mirror_id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
        "runtime_identity": (
            _json_loads_object(row["runtime_identity_json"])
            if "runtime_identity_json" in row.keys()
            and row["runtime_identity_json"] is not None
            else {}
        ),
    }


def _row_to_child_execution_edge(row: Any) -> AutomationChildExecutionEdge:
    return AutomationChildExecutionEdge.create(
        edge_id=row["edge_id"],
        parent_job_id=row["parent_job_id"],
        child_kind=row["child_kind"],
        child_id=row["child_id"],
        authoritative_for_parent_completion=normalize_bool(
            row["authoritative_for_parent_completion"]
        ),
        requested_runtime=_json_loads_object(row["requested_runtime_json"]),
        actual_runtime=(
            _json_loads_object(row["actual_runtime_json"])
            if row["actual_runtime_json"] is not None
            else None
        ),
        terminal_mapping=_json_loads_object(row["terminal_mapping_json"]),
        terminal_event_id=row["terminal_event_id"],
        terminal_state=row["terminal_state"],
        terminal_observed_at=row["terminal_observed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


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


__all__ = [
    "AutomationExecutionSnapshot",
    "automation_execution_snapshot",
    "automation_execution_snapshots_by_job_id",
]
