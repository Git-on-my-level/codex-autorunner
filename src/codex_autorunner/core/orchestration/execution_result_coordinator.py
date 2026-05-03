from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..time_utils import now_iso
from .models import ExecutionRecord, ThreadTarget

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TerminalTransition:
    thread_target_id: str
    execution_id: str
    to_state: str
    reason: Optional[str]
    event_type: str
    error: Optional[str]


_STATUS_TRANSITIONS = {
    "ok": ("completed", "managed_turn_completed", "managed_thread_completed"),
    "interrupted": (
        "interrupted",
        "managed_turn_interrupted",
        "managed_thread_interrupted",
    ),
}


def build_terminal_transition(
    *,
    thread_target_id: str,
    execution_id: str,
    status: Optional[str],
    error: Optional[str],
) -> TerminalTransition:
    normalized_status = str(status or "").strip().lower()
    mapped = _STATUS_TRANSITIONS.get(normalized_status)
    if mapped is not None:
        to_state, reason, event_type = mapped
        return TerminalTransition(
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            to_state=to_state,
            reason=reason,
            event_type=event_type,
            error=error,
        )
    return TerminalTransition(
        thread_target_id=thread_target_id,
        execution_id=execution_id,
        to_state="failed",
        reason=str(error or "").strip() or "managed_turn_failed",
        event_type="managed_thread_failed",
        error=error,
    )


def build_terminal_transition_payload(
    transition: TerminalTransition,
    *,
    thread: Optional[ThreadTarget],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "thread_id": transition.thread_target_id,
        "from_state": "running",
        "to_state": transition.to_state,
        "reason": transition.reason or "managed_turn_failed",
        "timestamp": now_iso(),
        "event_type": transition.event_type,
        "transition_id": (
            f"managed_turn:{transition.execution_id}:{transition.to_state}"
        ),
        "idempotency_key": (
            f"managed_turn:{transition.execution_id}:{transition.to_state}"
        ),
        "managed_thread_id": transition.thread_target_id,
        "managed_turn_id": transition.execution_id,
    }
    if thread is None:
        return payload
    optional_fields = {
        "repo_id": thread.repo_id,
        "resource_kind": thread.resource_kind,
        "resource_id": thread.resource_id,
    }
    payload.update({key: value for key, value in optional_fields.items() if value})
    payload["agent"] = thread.agent_id
    return payload


@dataclass(frozen=True)
class ExecutionResultCoordinator:
    """Coordinates terminal execution writes and transition notification."""

    get_execution: Callable[[str, str], Optional[ExecutionRecord]]
    get_thread_target: Callable[[str], Optional[ThreadTarget]]
    mark_turn_finished: Callable[..., Any]
    mark_turn_interrupted: Callable[[str], Any]
    notify_transition: Callable[[dict[str, Any]], dict[str, Any]]
    logger: logging.Logger = logger

    def record_execution_result(
        self,
        thread_target_id: str,
        execution_id: str,
        *,
        status: str,
        assistant_text: Optional[str] = None,
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
    ) -> ExecutionRecord:
        updated = self.mark_turn_finished(
            execution_id,
            status=status,
            assistant_text=assistant_text,
            error=error,
            backend_turn_id=backend_turn_id,
            transcript_turn_id=transcript_turn_id,
        )
        if not updated:
            raise KeyError(f"Execution '{execution_id}' was not running")
        execution = self.get_execution(thread_target_id, execution_id)
        if execution is None:
            raise KeyError(
                f"Execution '{execution_id}' is missing after result recording"
            )
        self.notify_terminal_transition(
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            status=execution.status,
            error=execution.error,
        )
        return execution

    def record_execution_interrupted(
        self, thread_target_id: str, execution_id: str
    ) -> ExecutionRecord:
        updated = self.mark_turn_interrupted(execution_id)
        execution = self.get_execution(thread_target_id, execution_id)
        if not updated:
            if execution is not None and execution.status == "interrupted":
                return execution
            raise KeyError(f"Execution '{execution_id}' was not running")
        if execution is None:
            raise KeyError(
                f"Execution '{execution_id}' is missing after interrupt recording"
            )
        self.notify_terminal_transition(
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            status=execution.status,
            error=execution.error,
        )
        return execution

    def notify_terminal_transition(
        self,
        *,
        thread_target_id: str,
        execution_id: str,
        status: Optional[str],
        error: Optional[str] = None,
    ) -> None:
        transition = build_terminal_transition(
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            status=status,
            error=error,
        )
        payload = build_terminal_transition_payload(
            transition,
            thread=self.get_thread_target(thread_target_id),
        )
        try:
            result = self.notify_transition(payload)
        except (OSError, RuntimeError, TypeError, ValueError):
            self.logger.exception(
                "Failed to notify PMA automation for terminal managed-thread "
                "transition (thread_target_id=%s, execution_id=%s, to_state=%s)",
                thread_target_id,
                execution_id,
                transition.to_state,
            )
            return

        try:
            created = int(result.get("created") or 0)
        except (TypeError, ValueError):
            created = 0
        if created > 0:
            self.logger.info(
                "Managed-thread PMA transition enqueued wakeups "
                "(thread_target_id=%s, execution_id=%s, event_type=%s, created=%s)",
                thread_target_id,
                execution_id,
                payload["event_type"],
                created,
            )
