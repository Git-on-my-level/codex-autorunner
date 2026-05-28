from __future__ import annotations

from typing import Any

from ..orchestration import FreshConversationRequiredError
from ..ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    OutputDelta,
    RunEvent,
)

DEFAULT_PMA_TIMEOUT_SECONDS = 1800
DEFAULT_PMA_WALL_CLOCK_TIMEOUT_SECONDS = 7200
SUCCESSFUL_COMPLETION_STATUSES = frozenset(
    {"ok", "completed", "complete", "done", "success"}
)
_MISSING_THREAD_MARKERS = (
    "thread not found",
    "no rollout found for thread id",
)


def _is_missing_backend_thread_error(exc: Exception) -> bool:
    exc_type = type(exc)
    if exc_type.__name__ != "CodexAppServerResponseError":
        return False
    if not exc_type.__module__.startswith("codex_autorunner.adapters.app_server"):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _MISSING_THREAD_MARKERS)


def requires_fresh_pma_conversation(exc: Exception) -> bool:
    if isinstance(exc, FreshConversationRequiredError):
        return True
    return _is_missing_backend_thread_error(exc)


def timeline_has_assistant_output(events: list[RunEvent]) -> bool:
    return any(
        isinstance(event, OutputDelta)
        and event.delta_type
        in {
            RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
            RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
        }
        for event in events
    )


def raw_events_show_completion(raw_events: tuple[Any, ...]) -> bool:
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            continue
        method = str(raw_event.get("method") or "").strip().lower()
        if not method:
            message = raw_event.get("message")
            if isinstance(message, dict):
                method = str(message.get("method") or "").strip().lower()
        if method in {
            "turn/completed",
            "prompt/completed",
            "session.idle",
        }:
            return True
    return False


def classify_runtime_turn_result(
    *,
    status: str,
    errors: tuple[Any, ...],
    assistant_text: str,
    completed_seen: bool,
    raw_events: tuple[Any, ...],
) -> dict[str, Any]:
    normalized_status = str(status or "").strip().lower()
    successful_completion = normalized_status in SUCCESSFUL_COMPLETION_STATUSES
    if errors:
        detail = next(
            (str(error or "").strip() for error in errors if str(error or "").strip()),
            "",
        )
        if (
            assistant_text
            and (successful_completion or completed_seen)
            and (completed_seen or raw_events_show_completion(raw_events))
        ):
            return {"status": "ok"}
        return {"status": "error", "detail": detail or "Managed thread failed"}
    if normalized_status in {"interrupted", "cancelled", "canceled", "aborted"}:
        return {"status": "interrupted", "detail": "Managed thread interrupted"}
    if normalized_status and not successful_completion:
        return {"status": "error", "detail": "Managed thread failed"}
    return {"status": "ok"}


__all__ = [
    "DEFAULT_PMA_TIMEOUT_SECONDS",
    "DEFAULT_PMA_WALL_CLOCK_TIMEOUT_SECONDS",
    "SUCCESSFUL_COMPLETION_STATUSES",
    "classify_runtime_turn_result",
    "raw_events_show_completion",
    "requires_fresh_pma_conversation",
    "timeline_has_assistant_output",
]
