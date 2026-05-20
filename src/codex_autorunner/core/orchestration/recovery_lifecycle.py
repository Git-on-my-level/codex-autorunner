from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from ..freshness import parse_iso_datetime
from ..logging_utils import log_event
from .execution_lifecycle import (
    _is_missing_thread_error,
    _is_recoverable_backend_error,
    _resolve_harness_runtime_instance_id,
    _resolve_thread_runtime_binding,
)
from .interfaces import (
    RuntimeThreadHarness,
    ThreadExecutionStore,
)
from .managed_turn_lifecycle_contract import (
    ManagedTurnRecoveryAction,
    classify_managed_turn_recovery_action,
)
from .models import (
    ExecutionRecord,
    ThreadStopOutcome,
    ThreadTarget,
)

LOST_BACKEND_THREAD_ERROR = "Running execution could not be reattached after restart"
MISSING_BACKEND_THREAD_ERROR = (
    "Running execution could not be reattached after restart because no backend "
    "thread binding was persisted"
)
_RESTART_WAIT_AND_SEE_AGENTS = frozenset({"codex"})
logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"ok", "error", "interrupted"})


@dataclass(frozen=True)
class ManagedTurnRecoveryDecision:
    """Auditable decision for one stale managed-thread lifecycle phase."""

    managed_thread_id: str
    execution_id: str
    prior_phase: str
    selected_action: ManagedTurnRecoveryAction
    reason: str
    age_seconds: Optional[float]
    current_status: str
    queue_depth: int = 0


@dataclass(frozen=True)
class ManagedTurnRecoveryResult:
    decision: ManagedTurnRecoveryDecision
    recovered_execution: Optional[ExecutionRecord] = None
    changed: bool = False


@dataclass(frozen=True)
class ManagedTurnRecoveryScanResult:
    scanned: int
    changed: int
    decisions: tuple[ManagedTurnRecoveryDecision, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _execution_age_seconds(
    execution: ExecutionRecord, *, now: datetime
) -> Optional[float]:
    started_at = parse_iso_datetime(execution.started_at) or parse_iso_datetime(
        execution.metadata.get("runtime_started_at")
    )
    if started_at is None:
        return None
    return max(0.0, (now - started_at).total_seconds())


def _managed_turn_lifecycle_phase(execution: ExecutionRecord) -> str:
    raw_phase = (
        execution.metadata.get("managed_turn_lifecycle_phase")
        or execution.metadata.get("lifecycle_phase")
        or execution.metadata.get("phase")
    )
    phase = str(raw_phase or "").strip().lower()
    if phase:
        return phase
    status = str(execution.status or "").strip().lower()
    if status == "queued":
        return "queued"
    if status == "running":
        return "runtime_running"
    if status in _TERMINAL_STATUSES:
        return "terminal_recorded"
    return status or "unknown"


def classify_stale_managed_turn_recovery(
    *,
    managed_thread_id: str,
    execution: ExecutionRecord,
    queue_depth: int,
    now: Optional[datetime] = None,
) -> ManagedTurnRecoveryDecision:
    resolved_now = now or _utc_now()
    phase = _managed_turn_lifecycle_phase(execution)
    status = str(execution.status or "").strip().lower()
    age_seconds = _execution_age_seconds(execution, now=resolved_now)
    action = classify_managed_turn_recovery_action(
        phase=phase,
        status=status,
        terminal_statuses=_TERMINAL_STATUSES,
    )
    return ManagedTurnRecoveryDecision(
        managed_thread_id=managed_thread_id,
        execution_id=execution.execution_id,
        prior_phase=action.phase,
        selected_action=action.selected_action,
        reason=action.reason,
        age_seconds=age_seconds,
        current_status=status,
        queue_depth=queue_depth,
    )


@dataclass(frozen=True)
class RecoveryScanner:
    """Deterministic scanner for stale managed-thread orchestration state."""

    recover_from_harness: Callable[[str], object]
    record_lost_execution: Callable[[str], Optional[ExecutionRecord]]
    get_running_execution: Callable[[str], Optional[ExecutionRecord]]
    list_thread_ids_with_running_executions: Callable[..., list[str]]
    get_queue_depth: Callable[[str], int]
    stale_after_seconds: float
    logger: logging.Logger = logger

    async def scan(
        self, *, now: Optional[datetime] = None
    ) -> ManagedTurnRecoveryScanResult:
        resolved_now = now or _utc_now()
        decisions: list[ManagedTurnRecoveryDecision] = []
        changed = 0
        thread_ids = self.list_thread_ids_with_running_executions(limit=None)
        for thread_id in thread_ids:
            result = await self.scan_thread(thread_id, now=resolved_now)
            if result is None:
                continue
            decisions.append(result.decision)
            if result.changed:
                changed += 1
        return ManagedTurnRecoveryScanResult(
            scanned=len(decisions),
            changed=changed,
            decisions=tuple(decisions),
        )

    async def scan_thread(
        self, thread_target_id: str, *, now: Optional[datetime] = None
    ) -> Optional[ManagedTurnRecoveryResult]:
        execution = self.get_running_execution(thread_target_id)
        if execution is None:
            return None
        resolved_now = now or _utc_now()
        age_seconds = _execution_age_seconds(execution, now=resolved_now)
        if age_seconds is not None and age_seconds < self.stale_after_seconds:
            return None
        decision = classify_stale_managed_turn_recovery(
            managed_thread_id=thread_target_id,
            execution=execution,
            queue_depth=self.get_queue_depth(thread_target_id),
            now=resolved_now,
        )
        log_event(
            self.logger,
            logging.WARNING,
            "orchestration.thread.recovery_scanner.decision",
            managed_thread_id=decision.managed_thread_id,
            execution_id=decision.execution_id,
            prior_phase=decision.prior_phase,
            selected_action=decision.selected_action,
            reason=decision.reason,
            age_seconds=decision.age_seconds,
            current_status=decision.current_status,
            queue_depth=decision.queue_depth,
        )
        recovered: Optional[ExecutionRecord] = None
        if decision.selected_action == "recover_from_harness":
            maybe_recovered = self.recover_from_harness(thread_target_id)
            if hasattr(maybe_recovered, "__await__"):
                maybe_recovered = await maybe_recovered
            if isinstance(maybe_recovered, ExecutionRecord):
                recovered = maybe_recovered
            if recovered is None:
                recovered = self.record_lost_execution(thread_target_id)
        elif decision.selected_action == "record_error":
            recovered = self.record_lost_execution(thread_target_id)
        return ManagedTurnRecoveryResult(
            decision=decision,
            recovered_execution=recovered,
            changed=recovered is not None,
        )


class BusyInterruptFailedError(RuntimeError):
    """Busy-policy interrupt failed while the original execution remained active."""

    def __init__(
        self,
        *,
        thread_target_id: str,
        active_execution_id: Optional[str],
        backend_thread_id: Optional[str],
        detail: str = "Interrupt attempt failed; original turn is still running",
    ) -> None:
        super().__init__(detail)
        self.thread_target_id = thread_target_id
        self.active_execution_id = active_execution_id
        self.backend_thread_id = backend_thread_id
        self.detail = detail


@dataclass
class _ThreadRecoveryHelper:
    """Owns interrupt, stop, restart recovery, and stale-binding validation.

    Ownership contract:
    - This helper is the sole authority for managed-thread recovery decisions.
    - It never synthesizes a successful completion outcome. All recovery paths
      record either ``error`` or ``interrupted`` status.
    - Stale backend binding **hints** (stored runtime instance id differs from
      the live harness id) are logged from execution start via this helper.
      Bindings are not cleared merely for that mismatch; ``start_execution``
      attempts ``resume_conversation`` first and only clears after proven
      backend failure paths.
    - Callers must not substitute their own stale-binding detection logic.
    """

    thread_store: ThreadExecutionStore
    get_thread_target: Callable[[str], Optional[ThreadTarget]]
    get_running_execution: Callable[[str], Optional[ExecutionRecord]]
    harness_for_thread: Callable[[ThreadTarget], RuntimeThreadHarness]

    def hint_stale_backend_binding_for_resume(
        self,
        *,
        thread_target_id: str,
        backend_thread_id: Optional[str],
        runtime_instance_id: Optional[str],
    ) -> bool:
        """Log a stale-runtime hint; never clears ``backend_thread_id`` here."""
        if not backend_thread_id or not runtime_instance_id:
            return False
        runtime_binding = _resolve_thread_runtime_binding(
            self.thread_store, thread_target_id
        )
        if runtime_binding is None:
            return False
        if (
            not runtime_binding.backend_runtime_instance_id
            or runtime_binding.backend_runtime_instance_id == runtime_instance_id
        ):
            return False
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.stale_backend_binding",
            thread_target_id=thread_target_id,
            backend_thread_id=backend_thread_id,
            stored_runtime_instance_id=runtime_binding.backend_runtime_instance_id,
            current_runtime_instance_id=runtime_instance_id,
            action="attempt_resume",
        )
        return False

    async def interrupt_thread(self, thread_target_id: str) -> ExecutionRecord:
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")
        if not thread.workspace_root:
            raise RuntimeError("Thread target is missing workspace_root")
        runtime_binding = _resolve_thread_runtime_binding(
            self.thread_store, thread_target_id
        )

        execution = self.get_running_execution(thread_target_id)
        if execution is None:
            raise KeyError(
                f"Thread target '{thread_target_id}' has no running execution"
            )
        if runtime_binding is None or not runtime_binding.backend_thread_id:
            return self.thread_store.record_execution_interrupted(
                thread_target_id, execution.execution_id
            )

        harness = self.harness_for_thread(thread)
        if not harness.supports("interrupt"):
            raise RuntimeError(f"Agent '{thread.agent_id}' does not support interrupt")
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.interrupt_requested",
            thread_target_id=thread_target_id,
            execution_id=execution.execution_id,
            backend_thread_id=runtime_binding.backend_thread_id,
            backend_turn_id=execution.backend_id,
            agent_id=thread.agent_id,
        )
        await harness.interrupt(
            Path(thread.workspace_root),
            runtime_binding.backend_thread_id,
            execution.backend_id,
        )
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.interrupt_acknowledged",
            thread_target_id=thread_target_id,
            execution_id=execution.execution_id,
            backend_thread_id=runtime_binding.backend_thread_id,
            backend_turn_id=execution.backend_id,
            agent_id=thread.agent_id,
        )
        interrupted = self.thread_store.record_execution_interrupted(
            thread_target_id, execution.execution_id
        )
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.interrupt_recorded",
            thread_target_id=thread_target_id,
            execution_id=interrupted.execution_id,
            backend_thread_id=runtime_binding.backend_thread_id,
            backend_turn_id=interrupted.backend_id,
            status=interrupted.status,
        )
        return interrupted

    def recover_lost_backend_execution(
        self,
        *,
        thread_target_id: str,
        execution: ExecutionRecord,
        backend_thread_id: Optional[str],
        error_message: str,
        reason: str,
    ) -> ExecutionRecord:
        recovered = self.thread_store.record_execution_result(
            thread_target_id,
            execution.execution_id,
            status="error",
            assistant_text="",
            error=error_message,
            backend_turn_id=execution.backend_id,
            transcript_turn_id=None,
        )
        self.thread_store.set_thread_backend_id(
            thread_target_id,
            None,
            backend_runtime_instance_id=None,
        )
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.recovered_lost_backend",
            thread_target_id=thread_target_id,
            execution_id=execution.execution_id,
            backend_thread_id=backend_thread_id,
            backend_turn_id=execution.backend_id,
            reason=reason,
            error=error_message,
        )
        return recovered

    def interrupt_lost_backend_execution(
        self,
        *,
        thread_target_id: str,
        execution: ExecutionRecord,
        backend_thread_id: Optional[str],
        reason: str,
    ) -> ExecutionRecord:
        interrupted = self.thread_store.record_execution_interrupted(
            thread_target_id, execution.execution_id
        )
        self.thread_store.set_thread_backend_id(
            thread_target_id,
            None,
            backend_runtime_instance_id=None,
        )
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.recovered_lost_backend",
            thread_target_id=thread_target_id,
            execution_id=execution.execution_id,
            backend_thread_id=backend_thread_id,
            backend_turn_id=execution.backend_id,
            reason=reason,
            error=None,
        )
        return interrupted

    async def stop_thread(
        self,
        thread_target_id: str,
        *,
        cancel_queued: bool = True,
    ) -> ThreadStopOutcome:
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")
        runtime_binding = _resolve_thread_runtime_binding(
            self.thread_store, thread_target_id
        )

        cancelled_queued = (
            self.thread_store.cancel_queued_executions(thread_target_id)
            if cancel_queued
            else 0
        )
        execution = self.get_running_execution(thread_target_id)
        if execution is None:
            return ThreadStopOutcome(
                thread_target_id=thread_target_id,
                cancelled_queued=cancelled_queued,
            )

        backend_thread_id = (
            runtime_binding.backend_thread_id if runtime_binding is not None else None
        )
        if not backend_thread_id:
            interrupted = self.interrupt_lost_backend_execution(
                thread_target_id=thread_target_id,
                execution=execution,
                backend_thread_id=None,
                reason="missing_backend_thread_id",
            )
            return ThreadStopOutcome(
                thread_target_id=thread_target_id,
                cancelled_queued=cancelled_queued,
                execution=interrupted,
                interrupted_active=True,
                recovered_lost_backend=True,
            )

        runtime_instance_id: Optional[str] = None
        if thread.workspace_root:
            harness = self.harness_for_thread(thread)
            runtime_instance_id = await _resolve_harness_runtime_instance_id(
                harness, Path(thread.workspace_root)
            )
        if (
            runtime_instance_id
            and runtime_binding
            and runtime_binding.backend_runtime_instance_id
            and runtime_binding.backend_runtime_instance_id != runtime_instance_id
        ):
            log_event(
                logger,
                logging.INFO,
                "orchestration.thread.stop_stale_backend_binding",
                thread_target_id=thread_target_id,
                execution_id=execution.execution_id,
                backend_thread_id=backend_thread_id,
                stored_runtime_instance_id=runtime_binding.backend_runtime_instance_id,
                current_runtime_instance_id=runtime_instance_id,
                action="attempt_interrupt",
            )

        try:
            interrupted = await self.interrupt_thread(thread_target_id)
        except Exception as exc:
            if not _is_recoverable_backend_error(exc):
                raise
            reason = (
                "interrupt_thread_not_found"
                if _is_missing_thread_error(exc)
                else "interrupt_thread_runtime_unavailable"
            )
            log_event(
                logger,
                logging.INFO,
                "orchestration.thread.interrupt_recoverable_backend_error",
                thread_target_id=thread_target_id,
                execution_id=execution.execution_id,
                backend_thread_id=backend_thread_id,
                backend_turn_id=execution.backend_id,
                reason=reason,
                exc=exc,
            )
            interrupted = self.interrupt_lost_backend_execution(
                thread_target_id=thread_target_id,
                execution=execution,
                backend_thread_id=backend_thread_id,
                reason=reason,
            )
            return ThreadStopOutcome(
                thread_target_id=thread_target_id,
                cancelled_queued=cancelled_queued,
                execution=interrupted,
                interrupted_active=True,
                recovered_lost_backend=True,
            )

        return ThreadStopOutcome(
            thread_target_id=thread_target_id,
            cancelled_queued=cancelled_queued,
            execution=interrupted,
            interrupted_active=True,
        )

    def recover_running_execution_after_restart(
        self, thread_target_id: str
    ) -> Optional[ExecutionRecord]:
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")

        execution = self.get_running_execution(thread_target_id)
        if execution is None:
            return None

        runtime_binding = _resolve_thread_runtime_binding(
            self.thread_store, thread_target_id
        )
        backend_thread_id = (
            runtime_binding.backend_thread_id
            if runtime_binding is not None and runtime_binding.backend_thread_id
            else (
                thread.backend_thread_id.strip()
                if isinstance(thread.backend_thread_id, str)
                and thread.backend_thread_id.strip()
                else None
            )
        )
        has_backend_turn_id = isinstance(execution.backend_id, str) and bool(
            execution.backend_id.strip()
        )
        has_live_runtime_binding = (
            runtime_binding is not None
            and isinstance(runtime_binding.backend_thread_id, str)
            and bool(runtime_binding.backend_thread_id.strip())
        )
        agent_id = str(thread.agent_id or "").strip().lower()
        if (
            agent_id in _RESTART_WAIT_AND_SEE_AGENTS
            and has_backend_turn_id
            and has_live_runtime_binding
        ):
            log_event(
                logger,
                logging.INFO,
                "orchestration.thread.restart_recovery_deferred",
                thread_target_id=thread_target_id,
                execution_id=execution.execution_id,
                backend_thread_id=backend_thread_id,
                backend_turn_id=execution.backend_id,
                agent_id=thread.agent_id,
                reason="agent_batches_events_until_turn_completion",
            )
            return None
        return self.recover_lost_backend_execution(
            thread_target_id=thread_target_id,
            execution=execution,
            backend_thread_id=backend_thread_id,
            error_message=LOST_BACKEND_THREAD_ERROR,
            reason=(
                "startup_lost_backend_binding"
                if (
                    backend_thread_id
                    or (
                        isinstance(execution.backend_id, str)
                        and execution.backend_id.strip()
                    )
                )
                else "startup_missing_backend_thread_id"
            ),
        )
