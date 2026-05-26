from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..time_utils import now_iso
from .managed_turn_lifecycle_contract import (
    ManagedTurnLifecyclePhase,
    ManagedTurnTerminalOutcome,
    ManagedTurnTerminalStatus,
    classify_terminal_recording,
)
from .models import ExecutionRecord, ThreadTarget
from .turn_assistant_output import TurnAssistantOutput

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
_TERMINAL_STATUSES = frozenset({"ok", "error", "interrupted"})
_MANAGED_TURN_LIFECYCLE_PHASE_KEY = "managed_turn_lifecycle_phase"


def _execution_lifecycle_phase(execution: Optional[ExecutionRecord]) -> str:
    if execution is None:
        return ""
    return str(execution.metadata.get(_MANAGED_TURN_LIFECYCLE_PHASE_KEY) or "").strip()


def _terminal_outcome_from_execution(
    execution: Optional[ExecutionRecord],
) -> Optional[ManagedTurnTerminalOutcome]:
    if execution is None or execution.status not in _TERMINAL_STATUSES:
        return None
    return ManagedTurnTerminalOutcome(
        status=execution.status,  # type: ignore[arg-type]
        error=execution.error,
    )


def _terminal_outcome_from_status(
    status: str, error: Optional[str]
) -> ManagedTurnTerminalOutcome:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in _TERMINAL_STATUSES:
        normalized_status = "error"
    return ManagedTurnTerminalOutcome(
        status=normalized_status,  # type: ignore[arg-type]
        error=error,
    )


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
    advance_lifecycle_phase: Optional[Callable[..., Any]] = None
    logger: logging.Logger = logger
    retry_delays: tuple[float, ...] = (0.25, 0.75)
    sleep: Callable[[float], None] = time.sleep

    def record_execution_result(
        self,
        thread_target_id: str,
        execution_id: str,
        *,
        status: str,
        assistant_text: Optional[str] = None,
        assistant_output: Optional[TurnAssistantOutput] = None,
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
        effective_runtime: Optional[Any] = None,
    ) -> ExecutionRecord:
        proposed_outcome = _terminal_outcome_from_status(status, error)
        existing = self.get_execution(thread_target_id, execution_id)
        terminal_decision = classify_terminal_recording(
            existing=_terminal_outcome_from_execution(existing),
            proposed=proposed_outcome,
        )
        self.logger.info(
            "Managed-turn terminal recording decision "
            "(thread_target_id=%s, execution_id=%s, action=%s, "
            "terminal_status=%s, existing_status=%s)",
            thread_target_id,
            execution_id,
            terminal_decision.action,
            proposed_outcome.status,
            terminal_decision.existing.status if terminal_decision.existing else None,
        )
        if terminal_decision.action in {"duplicate", "conflict"}:
            if existing is not None:
                if (
                    terminal_decision.action == "duplicate"
                    and _execution_lifecycle_phase(existing) != "terminal_recorded"
                ):
                    self._backfill_terminal_recorded_phase(
                        thread_target_id,
                        execution_id,
                        current_phase=_execution_lifecycle_phase(existing),
                        terminal_status=terminal_decision.outcome.status,
                    )
                    existing = (
                        self.get_execution(thread_target_id, execution_id) or existing
                    )
                    self.notify_terminal_transition(
                        thread_target_id=thread_target_id,
                        execution_id=execution_id,
                        status=existing.status,
                        error=existing.error,
                    )
                return existing
            raise KeyError(f"Execution '{execution_id}' is missing")
        current_phase = _execution_lifecycle_phase(existing)
        if current_phase != "terminal_recording":
            if current_phase != "runtime_terminal_observed":
                self._advance_lifecycle_phase(
                    thread_target_id,
                    execution_id,
                    to_phase="runtime_terminal_observed",
                    terminal_status=proposed_outcome.status,
                )
            self._advance_lifecycle_phase(
                thread_target_id,
                execution_id,
                to_phase="terminal_recording",
                terminal_status=proposed_outcome.status,
            )
        finish_kwargs: dict[str, Any] = {
            "status": status,
            "assistant_text": assistant_text,
            "error": error,
            "backend_turn_id": backend_turn_id,
            "transcript_turn_id": transcript_turn_id,
            "effective_runtime": effective_runtime,
        }
        if effective_runtime is not None:
            finish_kwargs["effective_runtime"] = effective_runtime
        if assistant_output is not None:
            finish_kwargs["assistant_output"] = assistant_output
        updated = self.mark_turn_finished(execution_id, **finish_kwargs)
        if not updated:
            raise KeyError(f"Execution '{execution_id}' was not running")
        execution = self.get_execution(thread_target_id, execution_id)
        if execution is None:
            raise KeyError(
                f"Execution '{execution_id}' is missing after result recording"
            )
        self._advance_lifecycle_phase(
            thread_target_id,
            execution_id,
            to_phase="terminal_recorded",
            terminal_status=proposed_outcome.status,
        )
        execution = self.get_execution(thread_target_id, execution_id) or execution
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
        existing = self.get_execution(thread_target_id, execution_id)
        proposed_outcome = ManagedTurnTerminalOutcome(status="interrupted")
        terminal_decision = classify_terminal_recording(
            existing=_terminal_outcome_from_execution(existing),
            proposed=proposed_outcome,
        )
        self.logger.info(
            "Managed-turn terminal recording decision "
            "(thread_target_id=%s, execution_id=%s, action=%s, "
            "terminal_status=interrupted, existing_status=%s)",
            thread_target_id,
            execution_id,
            terminal_decision.action,
            terminal_decision.existing.status if terminal_decision.existing else None,
        )
        if terminal_decision.action in {"duplicate", "conflict"}:
            if existing is not None:
                if (
                    terminal_decision.action == "duplicate"
                    and _execution_lifecycle_phase(existing) != "terminal_recorded"
                ):
                    self._backfill_terminal_recorded_phase(
                        thread_target_id,
                        execution_id,
                        current_phase=_execution_lifecycle_phase(existing),
                        terminal_status=terminal_decision.outcome.status,
                    )
                    existing = (
                        self.get_execution(thread_target_id, execution_id) or existing
                    )
                    self.notify_terminal_transition(
                        thread_target_id=thread_target_id,
                        execution_id=execution_id,
                        status=existing.status,
                        error=existing.error,
                    )
                return existing
            raise KeyError(f"Execution '{execution_id}' is missing")
        current_phase = _execution_lifecycle_phase(existing)
        if current_phase != "terminal_recording":
            if current_phase != "runtime_terminal_observed":
                self._advance_lifecycle_phase(
                    thread_target_id,
                    execution_id,
                    to_phase="runtime_terminal_observed",
                    terminal_status="interrupted",
                )
            self._advance_lifecycle_phase(
                thread_target_id,
                execution_id,
                to_phase="terminal_recording",
                terminal_status="interrupted",
            )
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
        self._advance_lifecycle_phase(
            thread_target_id,
            execution_id,
            to_phase="terminal_recorded",
            terminal_status="interrupted",
        )
        execution = self.get_execution(thread_target_id, execution_id) or execution
        self.notify_terminal_transition(
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            status=execution.status,
            error=execution.error,
        )
        return execution

    def _advance_lifecycle_phase(
        self,
        thread_target_id: str,
        execution_id: str,
        *,
        to_phase: ManagedTurnLifecyclePhase,
        terminal_status: Optional[ManagedTurnTerminalStatus] = None,
    ) -> None:
        if self.advance_lifecycle_phase is None:
            return
        self.advance_lifecycle_phase(
            thread_target_id,
            execution_id,
            to_phase=to_phase,
            terminal_status=terminal_status,
        )

    def _backfill_terminal_recorded_phase(
        self,
        thread_target_id: str,
        execution_id: str,
        *,
        current_phase: str,
        terminal_status: Optional[ManagedTurnTerminalStatus],
    ) -> None:
        if current_phase != "terminal_recording":
            if current_phase != "runtime_terminal_observed":
                self._advance_lifecycle_phase(
                    thread_target_id,
                    execution_id,
                    to_phase="runtime_terminal_observed",
                    terminal_status=terminal_status,
                )
            self._advance_lifecycle_phase(
                thread_target_id,
                execution_id,
                to_phase="terminal_recording",
                terminal_status=terminal_status,
            )
        self._advance_lifecycle_phase(
            thread_target_id,
            execution_id,
            to_phase="terminal_recorded",
            terminal_status=terminal_status,
        )

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
        attempts = len(self.retry_delays) + 1
        result: dict[str, Any] = {}
        for attempt in range(1, attempts + 1):
            try:
                result = self.notify_transition(payload)
                break
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self.logger.warning(
                    "Failed to notify automation for terminal managed-thread "
                    "transition; retrying if budget remains "
                    "(thread_target_id=%s, execution_id=%s, event_type=%s, "
                    "subscription_id=%s, attempt=%s, attempts=%s, error=%s)",
                    thread_target_id,
                    execution_id,
                    payload["event_type"],
                    payload.get("subscription_id"),
                    attempt,
                    attempts,
                    exc,
                    exc_info=True,
                )
                if attempt >= attempts:
                    return
                self.sleep(self.retry_delays[attempt - 1])

        try:
            created = int(result.get("created") or 0)
        except (TypeError, ValueError):
            created = 0
        if created > 0:
            self.logger.info(
                "Managed-thread transition enqueued automation jobs "
                "(thread_target_id=%s, execution_id=%s, event_type=%s, created=%s)",
                thread_target_id,
                execution_id,
                payload["event_type"],
                created,
            )
