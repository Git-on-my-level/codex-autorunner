from __future__ import annotations

from typing import Any, Optional

from .models import FlowRunStatus

MAX_REASON_SUMMARY_LEN = 120


def _truncate(text: str, max_len: int = MAX_REASON_SUMMARY_LEN) -> str:
    if len(text) <= max_len:
        return text
    return f"{text[:max_len].rstrip()}…"


def ensure_reason_summary(
    state: Any,
    *,
    status: FlowRunStatus,
    error_message: Optional[str] = None,
    default: Optional[str] = None,
) -> dict[str, Any]:
    """Ensure state includes a short reason_summary when stopping/pausing/failing."""
    normalized: dict[str, Any] = state if isinstance(state, dict) else {}
    existing = normalized.get("reason_summary")
    if isinstance(existing, str) and existing.strip():
        return normalized

    reason: Optional[str] = None
    engine = normalized.get("ticket_engine")
    if isinstance(engine, dict):
        engine_reason = engine.get("reason")
        if isinstance(engine_reason, str) and engine_reason.strip():
            reason = engine_reason.strip()

    if not reason and isinstance(error_message, str) and error_message.strip():
        reason = error_message.strip()

    if not reason:
        if default:
            reason = default
        else:
            fallback = {
                FlowRunStatus.PAUSED: "Paused",
                FlowRunStatus.FAILED: "Failed",
                FlowRunStatus.STOPPED: "Stopped",
            }
            reason = fallback.get(status)

    if reason:
        normalized["reason_summary"] = _truncate(reason)
    return normalized
