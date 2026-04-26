"""Structured observability for flow lifecycle transitions and reconciliation decisions.

Emits structured log events and durable telemetry at every consequential
transition point so operators can explain what happened, why, and what the
system did about it.

Schema is intentionally stable and field names are consistent across runtime
and reconciliation paths so dashboards and tests can key off them reliably.

Event types
-----------
runtime_transition
    A lifecycle reducer transition applied by the runtime.
reconcile_transition
    A state change applied by the reconciler.
reconcile_noop
    Reconciler examined an active run and decided no change was needed.
recovery_takeover
    Reconciler detected a dead/invalid worker and took over the run.
failure_projection
    Failure diagnostics were computed and attached to run state.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .models import FlowEventType, FlowRunStatus
from .store import FlowStore

_logger = logging.getLogger(__name__)

TRANSITION_EVENT_FAMILY = "flow_transition"


@dataclass
class TransitionTelemetryEvent:
    event_family: str = TRANSITION_EVENT_FAMILY
    event_type: str = ""
    run_id: str = ""
    previous_status: str = ""
    resulting_status: str = ""
    trigger: str = ""
    note: str = ""
    step_id: Optional[str] = None
    error_message: Optional[str] = None
    decision_rationale: str = ""
    worker_status: Optional[str] = None
    origin: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if v is None:
                continue
            if isinstance(v, dict) and not v:
                continue
            if (
                isinstance(v, str)
                and v == ""
                and k
                not in (
                    "previous_status",
                    "resulting_status",
                    "trigger",
                    "event_type",
                    "note",
                    "origin",
                    "event_family",
                    "run_id",
                    "decision_rationale",
                )
            ):
                continue
            d[k] = v
        return d


def _emit_log(event: TransitionTelemetryEvent) -> None:
    _logger.info(
        "flow_transition run_id=%s type=%s %s->%s trigger=%s note=%s",
        event.run_id,
        event.event_type,
        event.previous_status,
        event.resulting_status,
        event.trigger,
        event.note,
        extra={"transition_telemetry": event.to_dict()},
    )


def _persist_event(store: Optional[FlowStore], event: TransitionTelemetryEvent) -> None:
    if store is None:
        return
    try:
        store.create_telemetry(
            telemetry_id=str(uuid.uuid4()),
            run_id=event.run_id,
            event_type=FlowEventType.RUN_STATE_CHANGED,
            data=event.to_dict(),
        )
    except Exception:
        _logger.debug("Failed to persist transition telemetry for %s", event.run_id)


def emit_runtime_transition(
    *,
    store: Optional[FlowStore],
    run_id: str,
    previous_status: FlowRunStatus,
    resulting_status: FlowRunStatus,
    trigger: str,
    note: str,
    step_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    event = TransitionTelemetryEvent(
        event_type="runtime_transition",
        run_id=run_id,
        previous_status=previous_status.value,
        resulting_status=resulting_status.value,
        trigger=trigger,
        note=note,
        step_id=step_id,
        error_message=error_message,
        origin="runtime",
    )
    _emit_log(event)
    _persist_event(store, event)


def emit_reconcile_transition(
    *,
    store: Optional[FlowStore],
    run_id: str,
    previous_status: FlowRunStatus,
    resulting_status: FlowRunStatus,
    note: str,
    worker_status: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    event = TransitionTelemetryEvent(
        event_type="reconcile_transition",
        run_id=run_id,
        previous_status=previous_status.value,
        resulting_status=resulting_status.value,
        trigger="reconcile",
        note=note,
        worker_status=worker_status,
        error_message=error_message,
        origin="reconciler",
    )
    _emit_log(event)
    _persist_event(store, event)


def emit_reconcile_noop(
    *,
    store: Optional[FlowStore],
    run_id: str,
    status: FlowRunStatus,
    note: str = "",
    worker_status: Optional[str] = None,
) -> None:
    event = TransitionTelemetryEvent(
        event_type="reconcile_noop",
        run_id=run_id,
        previous_status=status.value,
        resulting_status=status.value,
        trigger="reconcile",
        note=note or "no-change",
        worker_status=worker_status,
        origin="reconciler",
    )
    _emit_log(event)
    _persist_event(store, event)


def emit_recovery_takeover(
    *,
    store: Optional[FlowStore],
    run_id: str,
    previous_status: FlowRunStatus,
    resulting_status: FlowRunStatus,
    note: str,
    worker_status: Optional[str] = None,
    crash_info: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    event = TransitionTelemetryEvent(
        event_type="recovery_takeover",
        run_id=run_id,
        previous_status=previous_status.value,
        resulting_status=resulting_status.value,
        trigger="reconcile-recovery",
        note=note,
        worker_status=worker_status,
        error_message=error_message,
        origin="reconciler",
        extra={"crash": crash_info} if crash_info else {},
    )
    _emit_log(event)
    _persist_event(store, event)


def emit_failure_projection(
    *,
    store: Optional[FlowStore],
    run_id: str,
    status: FlowRunStatus,
    failure_reason_code: Optional[str] = None,
    step_id: Optional[str] = None,
    error_message: Optional[str] = None,
    origin: str = "runtime",
) -> None:
    event = TransitionTelemetryEvent(
        event_type="failure_projection",
        run_id=run_id,
        previous_status=status.value,
        resulting_status=status.value,
        trigger="failure-projected",
        note=f"failure_reason_code={failure_reason_code}",
        step_id=step_id,
        error_message=error_message,
        origin=origin,
        extra=(
            {"failure_reason_code": failure_reason_code} if failure_reason_code else {}
        ),
    )
    _emit_log(event)
    _persist_event(store, event)
