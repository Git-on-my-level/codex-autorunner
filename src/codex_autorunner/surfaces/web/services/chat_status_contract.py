from __future__ import annotations

from typing import Any, Literal, Mapping, Optional

ChatSurfaceStatus = Literal["waiting", "running", "idle", "archived", "failed"]

_TERMINAL_IDLE_STATUSES = {
    "completed",
    "complete",
    "ok",
    "succeeded",
    "success",
    "delivered",
}
_FAILED_STATUSES = {"failed", "error", "blocked", "invalid"}


def normalize_chat_effective_status(value: Any) -> Optional[ChatSurfaceStatus]:
    """Normalize the backend-owned chat index status vocabulary."""

    raw = str(value or "").strip().lower()
    if raw in {"waiting", "queued", "pending"}:
        return "waiting"
    if raw in {"running", "in_progress", "started", "claimed", "delivering"}:
        return "running"
    if raw in {"idle", *_TERMINAL_IDLE_STATUSES}:
        return "idle"
    if raw in _FAILED_STATUSES:
        return "failed"
    if raw == "archived":
        return "archived"
    return None


def chat_effective_status_from_row(row: Mapping[str, Any]) -> ChatSurfaceStatus:
    """Read projected chat status, with one compatibility fallback for old rows."""

    projected = normalize_chat_effective_status(
        row.get("effective_status") or row.get("effectiveStatus")
    )
    if projected is not None:
        return projected
    if _chat_row_has_archived_marker(row):
        return "archived"
    status = normalize_chat_effective_status(row.get("status"))
    if status is not None:
        return status
    return _chat_effective_status_compat_fallback(row)


def _chat_row_has_archived_marker(row: Mapping[str, Any]) -> bool:
    lifecycle = str(row.get("lifecycle") or "").strip().lower()
    lifecycle_status = str(row.get("lifecycle_status") or "").strip().lower()
    archive_state = str(row.get("archive_state") or "").strip().lower()
    runtime = str(row.get("runtime_status") or row.get("target_runtime_status") or "")
    runtime_l = runtime.strip().lower()
    if lifecycle_status == "archived" or archive_state == "archived":
        return True
    return row.get("managed_thread_id") is None and (
        lifecycle == "archived" or runtime_l == "archived"
    )


def _chat_effective_status_compat_fallback(
    row: Mapping[str, Any],
) -> ChatSurfaceStatus:
    lifecycle = str(row.get("lifecycle") or "").strip().lower()
    runtime = str(row.get("runtime_status") or row.get("target_runtime_status") or "")
    runtime_l = runtime.strip().lower()
    if _chat_row_has_archived_marker(row):
        return "archived"
    try:
        queue_depth = int(row.get("queue_depth") or 0)
    except (TypeError, ValueError):
        queue_depth = 0
    if queue_depth > 0:
        return "waiting"
    normalized_runtime = normalize_chat_effective_status(runtime_l)
    if normalized_runtime is not None:
        return normalized_runtime
    if lifecycle == "running":
        return "running"
    return "idle"
