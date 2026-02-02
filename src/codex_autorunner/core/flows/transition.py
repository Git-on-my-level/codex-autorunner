from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.core.flows.reasons import ensure_reason_summary
from codex_autorunner.core.flows.store import now_iso


@dataclass(frozen=True)
class TransitionDecision:
    """Result of resolving a flow's next status.

    Attributes
    ----------
    status: FlowRunStatus
        The resolved outer status.
    finished_at: Optional[str]
        Completion timestamp when the flow reaches a terminal state.
    state: dict[str, Any]
        Updated state payload (ticket_engine etc.).
    note: Optional[str]
        Reason for the transition (useful for tests/logging).
    """

    status: FlowRunStatus
    finished_at: Optional[str]
    state: dict[str, Any]
    note: Optional[str] = None


def resolve_flow_transition(
    record: FlowRunRecord, health: Any, now: Optional[str] = None
) -> TransitionDecision:
    """Derive the flow status from worker liveness and inner ticket_engine state.

    This is intentionally pure and side-effect free to keep recovery/test logic simple.
    """

    now = now or now_iso()
    state: dict[str, Any] = record.state or {}
    engine_raw = state.get("ticket_engine") if isinstance(state, dict) else {}
    engine: dict[str, Any] = engine_raw if isinstance(engine_raw, dict) else {}
    inner_status = engine.get("status")
    reason_code = engine.get("reason_code")

    # 1) Inner engine completion takes priority over worker liveness for active flows.
    if record.status == FlowRunStatus.RUNNING:
        if inner_status == "paused":
            state = ensure_reason_summary(state, status=FlowRunStatus.PAUSED)
            return TransitionDecision(
                status=FlowRunStatus.PAUSED,
                finished_at=None,
                state=state,
                note="engine-paused",
            )

        if inner_status == "completed":
            return TransitionDecision(
                status=FlowRunStatus.COMPLETED,
                finished_at=now,
                state=state,
                note="engine-completed",
            )

        # 2) Worker liveness overrides for active flows (only if engine not completed).
        if not health.is_alive:
            new_status = FlowRunStatus.FAILED
            state = ensure_reason_summary(
                state, status=new_status, default="Worker died"
            )
            return TransitionDecision(
                status=new_status, finished_at=now, state=state, note="worker-dead"
            )

        return TransitionDecision(
            status=FlowRunStatus.RUNNING, finished_at=None, state=state, note="running"
        )

    # Handle STOPPING case separately - worker liveness check still applies.
    if record.status == FlowRunStatus.STOPPING and not health.is_alive:
        state = ensure_reason_summary(
            state, status=FlowRunStatus.STOPPED, default="Worker stopped"
        )
        return TransitionDecision(
            status=FlowRunStatus.STOPPED,
            finished_at=now,
            state=state,
            note="worker-dead",
        )

    if record.status == FlowRunStatus.PAUSED:
        if inner_status == "completed":
            return TransitionDecision(
                status=FlowRunStatus.COMPLETED,
                finished_at=now,
                state=state,
                note="paused-engine-completed",
            )

        if (
            inner_status in (None, "running")
            and reason_code != "user_pause"
            and health.is_alive
        ):
            # Treat as stale pause; resume and clear pause metadata.
            engine.pop("reason", None)
            engine.pop("reason_details", None)
            engine.pop("reason_code", None)
            state.pop("reason_summary", None)
            engine["status"] = "running"
            state["ticket_engine"] = engine
            return TransitionDecision(
                status=FlowRunStatus.RUNNING,
                finished_at=None,
                state=state,
                note="stale-pause-resumed",
            )

        if not health.is_alive:
            return TransitionDecision(
                status=FlowRunStatus.PAUSED,
                finished_at=None,
                state=state,
                note="paused-worker-dead",
            )

        state = ensure_reason_summary(state, status=FlowRunStatus.PAUSED)
        return TransitionDecision(
            status=FlowRunStatus.PAUSED, finished_at=None, state=state, note="paused"
        )

    # STOPPING/STOPPED/COMPLETED/FAILED: leave unchanged.
    if record.status.is_terminal() or record.status == FlowRunStatus.STOPPED:
        return TransitionDecision(
            status=record.status,
            finished_at=record.finished_at,
            state=state,
            note="terminal",
        )

    return TransitionDecision(
        status=record.status, finished_at=None, state=state, note="unchanged"
    )
