from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from ..time_utils import now_iso
from .codex_item_normalizers import (
    merge_runtime_raw_events as _merge_runtime_raw_events,
)
from .execution_history import ExecutionCheckpoint, ExecutionCheckpointSignal
from .runtime_state_events import (
    AssistantDelta,
    AssistantMessage,
    FailureSignal,
    ProgressSignal,
    RuntimeStateEvent,
    TerminalSignal,
    TokenUsage,
    TransportReturned,
    normalize_runtime_state_events,
    normalize_transport_returned,
)
from .stream_text_merge import AssistantOutputState
from .turn_assistant_output import TurnAssistantOutput

RuntimeThreadOutcomeStatus = Literal["ok", "error", "interrupted"]
RuntimeThreadCompletionSource = Literal[
    "interrupt",
    "missing_backend_ids",
    "prompt_return",
    "reconciled_failure",
    "stream_terminal_event",
    "timeout",
    "transport_error",
]
_SUCCESSFUL_COMPLETION_STATUSES = frozenset(
    {"ok", "completed", "complete", "done", "success", "succeeded", "idle"}
)
_INTERRUPTED_COMPLETION_STATUSES = frozenset(
    {"interrupted", "cancelled", "canceled", "aborted"}
)
_DEFAULT_INTERRUPTED_ERROR = "Runtime thread interrupted"


@dataclass(frozen=True)
class RuntimeThreadTerminalSignal:
    source: str
    status: RuntimeThreadOutcomeStatus
    timestamp: str


@dataclass(frozen=True)
class RuntimeStatusClassification:
    source_status: Optional[str]
    normalized_outcome: Optional[RuntimeThreadOutcomeStatus]
    terminal: bool
    prefers_completion_settle: bool
    reason: str


@dataclass(frozen=True)
class TerminalEvidence:
    """Inputs to the terminal-outcome precedence reducer.

    Precedence table:
    - Explicit timeout wins and never delivers partial assistant text.
    - Explicit interrupt wins unless a successful transport return is already
      durably recorded for this turn.
    - Failed transport with no successful terminal evidence is an error.
    - Successful terminal evidence plus assistant text can recover
      transport-layer failures.
    - Idle/status terminal evidence is a terminal signal only; it can release
      already observed assistant text, but cannot synthesize assistant text.
    """

    transport_status: str
    transport_errors: tuple[str, ...]
    transport_returned: bool
    assistant_text: str
    failure_cause: Optional[str]
    terminal_signals: tuple[RuntimeThreadTerminalSignal, ...]
    explicit_interrupt: bool = False
    explicit_timeout: bool = False
    transport_exception: Optional[str] = None
    execution_error_message: str = ""


@dataclass(frozen=True)
class TerminalEvidenceDecision:
    status: RuntimeThreadOutcomeStatus
    assistant_text: str
    error: Optional[str]
    completion_source: RuntimeThreadCompletionSource
    reason: str

    def evidence_fields(self, evidence: TerminalEvidence) -> dict[str, Any]:
        latest_terminal = _latest_terminal_signal(evidence.terminal_signals)
        return {
            "terminal_evidence_reason": self.reason,
            "terminal_evidence_transport_status": evidence.transport_status or None,
            "terminal_evidence_transport_returned": evidence.transport_returned,
            "terminal_evidence_transport_error_count": len(evidence.transport_errors),
            "terminal_evidence_terminal_signal_count": len(evidence.terminal_signals),
            "terminal_evidence_latest_terminal_source": (
                latest_terminal.source if latest_terminal is not None else None
            ),
            "terminal_evidence_latest_terminal_status": (
                latest_terminal.status if latest_terminal is not None else None
            ),
            "terminal_evidence_assistant_chars": len(evidence.assistant_text),
            "terminal_evidence_explicit_interrupt": evidence.explicit_interrupt,
            "terminal_evidence_explicit_timeout": evidence.explicit_timeout,
            "terminal_evidence_transport_exception": (
                bool(evidence.transport_exception)
            ),
        }


@dataclass(frozen=True)
class RuntimeThreadOutcome:
    """Collected outcome of one runtime-thread execution before persistence."""

    status: RuntimeThreadOutcomeStatus
    assistant_text: str
    error: Optional[str]
    backend_thread_id: str
    backend_turn_id: Optional[str]
    raw_events: tuple[Any, ...] = ()
    completion_source: RuntimeThreadCompletionSource = "prompt_return"
    terminal_signals: tuple[RuntimeThreadTerminalSignal, ...] = ()
    transport_request_return_timestamp: Optional[str] = None
    last_progress_timestamp: Optional[str] = None
    failure_cause: Optional[str] = None
    terminal_evidence: dict[str, Any] = field(default_factory=dict)
    assistant_output: Optional[TurnAssistantOutput] = None
    effective_runtime: Optional[dict[str, Any]] = None


def reduce_terminal_evidence(evidence: TerminalEvidence) -> TerminalEvidenceDecision:
    """Resolve terminal evidence into one user-visible outcome.

    This is the only policy function that decides precedence between streamed
    terminal signals, prompt/request return status, explicit interrupt records,
    timeout/stall probes, and transport exceptions.
    """

    status = evidence.transport_status
    assistant_text = evidence.assistant_text
    detail = (
        next(iter(evidence.transport_errors), "")
        or evidence.transport_exception
        or evidence.failure_cause
        or None
    )
    transport_classification = classify_runtime_status(status)
    successful_transport = transport_classification.normalized_outcome == "ok"
    interrupted_transport = transport_classification.normalized_outcome == "interrupted"
    failed_transport = (
        bool(status) and not successful_transport and not interrupted_transport
    )
    successful_terminal = _saw_successful_terminal_signal(evidence.terminal_signals)
    latest_terminal = _latest_terminal_signal(evidence.terminal_signals)
    has_assistant_text = bool(assistant_text.strip())

    if evidence.explicit_timeout:
        return TerminalEvidenceDecision(
            status="error",
            assistant_text="",
            error=detail or evidence.execution_error_message,
            completion_source="timeout",
            reason="explicit_timeout",
        )

    if evidence.explicit_interrupt and not successful_transport:
        return TerminalEvidenceDecision(
            status="interrupted",
            assistant_text="",
            error=detail or _DEFAULT_INTERRUPTED_ERROR,
            completion_source="interrupt",
            reason="explicit_interrupt",
        )

    if evidence.transport_exception:
        if successful_terminal and has_assistant_text:
            return TerminalEvidenceDecision(
                status="ok",
                assistant_text=assistant_text,
                error=None,
                completion_source="reconciled_failure",
                reason="terminal_success_recovered_transport_exception",
            )
        return TerminalEvidenceDecision(
            status="error",
            assistant_text="",
            error=detail or evidence.execution_error_message,
            completion_source="transport_error",
            reason="transport_exception_without_terminal_success",
        )

    if not evidence.transport_returned:
        if latest_terminal is not None and latest_terminal.status == "ok":
            return TerminalEvidenceDecision(
                status="ok",
                assistant_text=assistant_text,
                error=None,
                completion_source="stream_terminal_event",
                reason="stream_terminal_success",
            )
        if latest_terminal is not None and latest_terminal.status == "interrupted":
            return TerminalEvidenceDecision(
                status="interrupted",
                assistant_text="",
                error=detail or _DEFAULT_INTERRUPTED_ERROR,
                completion_source="stream_terminal_event",
                reason="stream_terminal_interrupted",
            )
        return TerminalEvidenceDecision(
            status="error",
            assistant_text="",
            error=detail or evidence.execution_error_message,
            completion_source="stream_terminal_event",
            reason="stream_terminal_error",
        )

    if evidence.transport_errors:
        if interrupted_transport:
            return TerminalEvidenceDecision(
                status="interrupted",
                assistant_text="",
                error=detail or _DEFAULT_INTERRUPTED_ERROR,
                completion_source="prompt_return",
                reason="transport_interrupted",
            )
        if successful_terminal and has_assistant_text:
            return TerminalEvidenceDecision(
                status="ok",
                assistant_text=assistant_text,
                error=None if successful_transport else detail or None,
                completion_source=(
                    "reconciled_failure"
                    if not successful_transport
                    else "prompt_return"
                ),
                reason=(
                    "terminal_success_recovered_failed_transport"
                    if not successful_transport
                    else "successful_transport_with_terminal_success"
                ),
            )
        if failed_transport:
            return TerminalEvidenceDecision(
                status="error",
                assistant_text="",
                error=detail or evidence.execution_error_message,
                completion_source="prompt_return",
                reason="failed_transport_without_terminal_success",
            )
        if has_assistant_text:
            return TerminalEvidenceDecision(
                status="ok",
                assistant_text=assistant_text,
                error=detail or None,
                completion_source="prompt_return",
                reason="successful_transport_with_assistant_text",
            )
        return TerminalEvidenceDecision(
            status="error",
            assistant_text="",
            error=detail or evidence.execution_error_message,
            completion_source="prompt_return",
            reason="transport_errors_without_assistant_text",
        )

    if interrupted_transport:
        return TerminalEvidenceDecision(
            status="interrupted",
            assistant_text="",
            error=evidence.failure_cause,
            completion_source="interrupt",
            reason="transport_interrupted",
        )
    if failed_transport:
        return TerminalEvidenceDecision(
            status="error",
            assistant_text="",
            error=detail or evidence.execution_error_message,
            completion_source="prompt_return",
            reason="failed_transport",
        )
    return TerminalEvidenceDecision(
        status="ok",
        assistant_text=assistant_text,
        error=None,
        completion_source="prompt_return",
        reason="successful_transport",
    )


def extract_runtime_status_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("type", "status", "state"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def classify_runtime_status(value: Any) -> RuntimeStatusClassification:
    source_status = extract_runtime_status_value(value)
    normalized = source_status.strip().lower() if source_status else ""
    if not normalized:
        return RuntimeStatusClassification(
            source_status=None,
            normalized_outcome=None,
            terminal=False,
            prefers_completion_settle=False,
            reason="missing_status",
        )
    if normalized in _SUCCESSFUL_COMPLETION_STATUSES:
        return RuntimeStatusClassification(
            source_status=source_status,
            normalized_outcome="ok",
            terminal=True,
            prefers_completion_settle=normalized
            in {"completed", "complete", "done", "success", "succeeded"},
            reason="successful_status",
        )
    if normalized in _INTERRUPTED_COMPLETION_STATUSES or normalized == "stopped":
        return RuntimeStatusClassification(
            source_status=source_status,
            normalized_outcome="interrupted",
            terminal=True,
            prefers_completion_settle=False,
            reason="interrupted_status",
        )
    if normalized in {"failed", "failure", "error", "errored"}:
        return RuntimeStatusClassification(
            source_status=source_status,
            normalized_outcome="error",
            terminal=True,
            prefers_completion_settle=False,
            reason="failed_status",
        )
    return RuntimeStatusClassification(
        source_status=source_status,
        normalized_outcome=None,
        terminal=False,
        prefers_completion_settle=False,
        reason="active_or_unknown_status",
    )


def runtime_status_is_terminal(value: Any) -> bool:
    return classify_runtime_status(value).terminal


def runtime_status_prefers_completion_settle(value: Any) -> bool:
    return classify_runtime_status(value).prefers_completion_settle


def _saw_successful_terminal_signal(
    signals: tuple[RuntimeThreadTerminalSignal, ...],
) -> bool:
    return any(signal.status == "ok" for signal in signals)


def _latest_terminal_signal(
    signals: tuple[RuntimeThreadTerminalSignal, ...],
) -> Optional[RuntimeThreadTerminalSignal]:
    if not signals:
        return None
    return signals[-1]


@dataclass
class RuntimeTurnTerminalStateMachine:
    """Authoritative runtime-turn terminal reconciler for orchestration."""

    backend_thread_id: str
    backend_turn_id: Optional[str]
    last_assistant_text: str = ""
    transport_status: Optional[str] = None
    transport_errors: tuple[str, ...] = ()
    transport_request_return_timestamp: Optional[str] = None
    last_progress_timestamp: Optional[str] = None
    last_progress_monotonic: Optional[float] = None
    failure_cause: Optional[str] = None
    token_usage: Optional[dict[str, Any]] = None
    effective_runtime: Optional[dict[str, Any]] = None
    last_runtime_method: Optional[str] = None
    raw_events: list[Any] = field(default_factory=list)
    terminal_signals: list[RuntimeThreadTerminalSignal] = field(default_factory=list)
    _terminal_signal_keys: set[tuple[str, RuntimeThreadOutcomeStatus]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _terminal_signal_event: asyncio.Event = field(init=False, repr=False)
    _assistant_text: AssistantOutputState = field(
        default_factory=AssistantOutputState,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._terminal_signal_event = asyncio.Event()
        self._assistant_text = AssistantOutputState(
            stream_text=self.last_assistant_text,
        )

    def _note_assistant_stream_text(self, text: str) -> None:
        self._assistant_text.note_stream_snapshot(text)
        self.last_assistant_text = self._assistant_text.text

    def _note_assistant_message_text(self, text: str) -> None:
        if isinstance(text, str) and text.strip():
            self._assistant_text.note_final_message(text)
            self.last_assistant_text = self._assistant_text.text

    def terminal_signal_waiter(self) -> asyncio.Event:
        return self._terminal_signal_event

    def note_raw_event(
        self, raw_event: Any, *, timestamp: Optional[str] = None
    ) -> None:
        event_timestamp = timestamp or now_iso()
        self.raw_events.append(raw_event)
        self.last_progress_timestamp = event_timestamp
        self.last_progress_monotonic = time.monotonic()
        for event in normalize_runtime_state_events(raw_event):
            self.note_state_event(event, timestamp=event_timestamp)

    def note_state_event(
        self,
        event: RuntimeStateEvent,
        *,
        timestamp: Optional[str] = None,
    ) -> None:
        event_timestamp = timestamp or now_iso()
        if isinstance(event, AssistantDelta):
            self._note_assistant_stream_text(event.text)
            return
        if isinstance(event, AssistantMessage):
            self._note_assistant_message_text(event.text)
            return
        if isinstance(event, TerminalSignal):
            if event.final_text:
                self._note_assistant_message_text(event.final_text)
            if event.error:
                self.failure_cause = event.error
            self._note_terminal_signal(
                RuntimeThreadTerminalSignal(
                    source=event.source,
                    status=event.status,
                    timestamp=event_timestamp,
                )
            )
            return
        if isinstance(event, TransportReturned):
            self.transport_request_return_timestamp = event_timestamp
            self.transport_status = event.status
            self.transport_errors = event.errors
            if event.assistant_text.strip():
                self._note_assistant_message_text(event.assistant_text)
            if self.transport_errors and not self.failure_cause:
                self.failure_cause = self.transport_errors[0]
            return
        if isinstance(event, FailureSignal):
            self.failure_cause = event.error
            return
        if isinstance(event, TokenUsage):
            self.token_usage = dict(event.usage)
            return
        if isinstance(event, ProgressSignal) and event.kind == "runtime_method":
            self.last_runtime_method = event.message

    def note_transport_result(
        self,
        result: Any,
        *,
        timestamp: Optional[str] = None,
    ) -> None:
        event_timestamp = timestamp or now_iso()
        effective_runtime = getattr(result, "effective_runtime", None)
        if isinstance(effective_runtime, dict):
            self.effective_runtime = dict(effective_runtime)
        transport_event = normalize_transport_returned(result)
        self.note_state_event(transport_event, timestamp=event_timestamp)
        merged_raw_events = _merge_runtime_raw_events(
            self.raw_events,
            list(transport_event.raw_events),
        )
        if len(merged_raw_events) > len(self.raw_events):
            new_events = merged_raw_events[len(self.raw_events) :]
            self.raw_events = merged_raw_events
            for raw_event in new_events:
                for event in normalize_runtime_state_events(raw_event):
                    self.note_state_event(event, timestamp=event_timestamp)

    def build_missing_backend_ids_outcome(self, error: str) -> RuntimeThreadOutcome:
        return RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=error,
            backend_thread_id=self.backend_thread_id,
            backend_turn_id=self.backend_turn_id,
            raw_events=tuple(self.raw_events),
            completion_source="missing_backend_ids",
            terminal_signals=tuple(self.terminal_signals),
            transport_request_return_timestamp=self.transport_request_return_timestamp,
            last_progress_timestamp=self.last_progress_timestamp,
            failure_cause=error,
        )

    def build_timeout_outcome(self, error: str) -> RuntimeThreadOutcome:
        timestamp = now_iso()
        self.failure_cause = error
        self._note_terminal_signal(
            RuntimeThreadTerminalSignal(
                source="timeout",
                status="error",
                timestamp=timestamp,
            )
        )
        return self._build_outcome_from_evidence(
            self._terminal_evidence(
                explicit_timeout=True,
                execution_error_message=error,
            )
        )

    def build_interrupted_outcome(self, error: str) -> RuntimeThreadOutcome:
        timestamp = now_iso()
        self.failure_cause = error
        self._note_terminal_signal(
            RuntimeThreadTerminalSignal(
                source="interrupt",
                status="interrupted",
                timestamp=timestamp,
            )
        )
        return self._build_outcome_from_evidence(
            self._terminal_evidence(
                explicit_interrupt=True,
                execution_error_message=error,
            )
        )

    def build_transport_exception_outcome(
        self,
        error: str,
    ) -> RuntimeThreadOutcome:
        self.failure_cause = error
        return self._build_outcome_from_evidence(
            self._terminal_evidence(
                transport_exception=error,
                execution_error_message=error,
            )
        )

    def build_outcome(self, execution_error_message: str) -> RuntimeThreadOutcome:
        return self._build_outcome_from_evidence(
            self._terminal_evidence(execution_error_message=execution_error_message)
        )

    def _terminal_evidence(
        self,
        *,
        explicit_interrupt: bool = False,
        explicit_timeout: bool = False,
        transport_exception: Optional[str] = None,
        execution_error_message: str = "",
    ) -> TerminalEvidence:
        return TerminalEvidence(
            transport_status=self.transport_status or "",
            transport_errors=tuple(self.transport_errors),
            transport_returned=self.transport_request_return_timestamp is not None,
            assistant_text=self.last_assistant_text,
            failure_cause=self.failure_cause,
            terminal_signals=tuple(self.terminal_signals),
            explicit_interrupt=explicit_interrupt,
            explicit_timeout=explicit_timeout,
            transport_exception=transport_exception,
            execution_error_message=execution_error_message,
        )

    def _build_outcome_from_evidence(
        self, evidence: TerminalEvidence
    ) -> RuntimeThreadOutcome:
        decision = reduce_terminal_evidence(evidence)
        return self._build_outcome(
            status=decision.status,
            assistant_text=decision.assistant_text,
            error=decision.error,
            completion_source=decision.completion_source,
            terminal_evidence=decision.evidence_fields(evidence),
        )

    def _saw_successful_terminal_signal(self) -> bool:
        return _saw_successful_terminal_signal(tuple(self.terminal_signals))

    def _latest_terminal_signal(self) -> Optional[RuntimeThreadTerminalSignal]:
        return _latest_terminal_signal(tuple(self.terminal_signals))

    def _note_terminal_signal(self, signal: RuntimeThreadTerminalSignal) -> None:
        key = (signal.source, signal.status)
        if key in self._terminal_signal_keys:
            return
        self._terminal_signal_keys.add(key)
        self.terminal_signals.append(signal)
        self._terminal_signal_event.set()

    def _build_outcome(
        self,
        *,
        status: RuntimeThreadOutcomeStatus,
        assistant_text: str,
        error: Optional[str],
        completion_source: RuntimeThreadCompletionSource,
        terminal_evidence: Optional[dict[str, Any]] = None,
    ) -> RuntimeThreadOutcome:
        return RuntimeThreadOutcome(
            status=status,
            assistant_text=assistant_text,
            error=error,
            backend_thread_id=self.backend_thread_id,
            backend_turn_id=self.backend_turn_id,
            raw_events=tuple(self.raw_events),
            completion_source=completion_source,
            terminal_signals=tuple(self.terminal_signals),
            transport_request_return_timestamp=self.transport_request_return_timestamp,
            last_progress_timestamp=self.last_progress_timestamp,
            failure_cause=self.failure_cause,
            terminal_evidence=dict(terminal_evidence or {}),
            effective_runtime=(
                dict(self.effective_runtime)
                if isinstance(self.effective_runtime, dict)
                else None
            ),
        )

    def build_checkpoint(
        self,
        *,
        execution_id: Optional[str] = None,
        thread_target_id: Optional[str] = None,
        status: Optional[str] = None,
        completion_source: Optional[str] = None,
        assistant_text: Optional[str] = None,
        projection_event_cursor: int = 0,
        trace_manifest_id: Optional[str] = None,
    ) -> ExecutionCheckpoint:
        effective_text = str(
            self.last_assistant_text if assistant_text is None else assistant_text
        )
        latest_terminal = self._latest_terminal_signal()
        checkpoint_status = (
            str(status or "").strip()
            or str(self.transport_status or "").strip()
            or (latest_terminal.status if latest_terminal is not None else "running")
        )
        return ExecutionCheckpoint(
            status=checkpoint_status or "running",
            execution_id=execution_id,
            thread_target_id=thread_target_id,
            backend_thread_id=self.backend_thread_id or None,
            backend_turn_id=self.backend_turn_id or None,
            completion_source=completion_source,
            assistant_text_preview=_checkpoint_preview(effective_text),
            assistant_char_count=len(effective_text),
            last_runtime_method=self.last_runtime_method,
            last_progress_at=self.last_progress_timestamp,
            transport_status=self.transport_status,
            transport_request_return_timestamp=self.transport_request_return_timestamp,
            token_usage=(
                dict(self.token_usage) if isinstance(self.token_usage, dict) else None
            ),
            failure_cause=self.failure_cause,
            raw_event_count=len(self.raw_events),
            projection_event_cursor=max(int(projection_event_cursor or 0), 0),
            terminal_signals=tuple(
                ExecutionCheckpointSignal(
                    source=signal.source,
                    status=signal.status,
                    timestamp=signal.timestamp,
                )
                for signal in self.terminal_signals
            ),
            trace_manifest_id=trace_manifest_id,
        )


def _checkpoint_preview(value: str, limit: int = 240) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


__all__ = [
    "ExecutionCheckpoint",
    "RuntimeThreadCompletionSource",
    "RuntimeThreadOutcome",
    "RuntimeThreadOutcomeStatus",
    "RuntimeThreadTerminalSignal",
    "RuntimeStatusClassification",
    "RuntimeTurnTerminalStateMachine",
    "TerminalEvidence",
    "TerminalEvidenceDecision",
    "classify_runtime_status",
    "extract_runtime_status_value",
    "reduce_terminal_evidence",
    "runtime_status_is_terminal",
    "runtime_status_prefers_completion_settle",
]
