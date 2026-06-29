from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from .models import FlowRunStatus

FlowActionName = str

_ACTIVE_STATUSES = {
    FlowRunStatus.PENDING.value,
    FlowRunStatus.RUNNING.value,
    FlowRunStatus.STOPPING.value,
}
_TERMINAL_STATUSES = {
    FlowRunStatus.COMPLETED.value,
    FlowRunStatus.FAILED.value,
    FlowRunStatus.STOPPED.value,
    FlowRunStatus.SUPERSEDED.value,
}
_UNHEALTHY_WORKER_STATUSES = {"dead", "mismatch", "invalid", "absent"}


@dataclass(frozen=True)
class FlowActionPolicySnapshot:
    status: Optional[str] = None
    worker_health_status: Optional[str] = None
    retire_mode: str = "blocked"
    has_run: bool = False
    has_open_tickets: bool = False
    has_queue_scope: bool = True


@dataclass(frozen=True)
class FlowActionDescriptor:
    action: FlowActionName
    enabled: bool
    label: str
    tone: str = "secondary"
    style: str = "secondary"
    requires_confirmation: bool = False
    disabled_reason: Optional[str] = None
    surface_visibility: dict[str, bool] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data["disabled_reason"] is None:
            data.pop("disabled_reason")
        if data["surface_visibility"] is None:
            data.pop("surface_visibility")
        return data


def _normalize_status(value: Any) -> Optional[str]:
    raw = getattr(value, "value", value)
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


def _disabled(label: str, reason: str) -> tuple[bool, str, str]:
    return False, label, reason


def build_flow_action_policy(
    snapshot: FlowActionPolicySnapshot,
) -> list[FlowActionDescriptor]:
    status = _normalize_status(snapshot.status)
    has_run = bool(snapshot.has_run)
    is_paused = status == FlowRunStatus.PAUSED.value
    is_active = status in _ACTIVE_STATUSES
    is_terminal = status in _TERMINAL_STATUSES
    worker_unhealthy = snapshot.worker_health_status in _UNHEALTHY_WORKER_STATUSES

    actions: list[FlowActionDescriptor] = []

    start_enabled = (
        snapshot.has_queue_scope
        and snapshot.has_open_tickets
        and (not has_run or is_terminal)
    )
    actions.append(
        FlowActionDescriptor(
            action="start",
            enabled=start_enabled,
            label="Start queue",
            tone="primary",
            style="primary",
            disabled_reason=(
                None
                if start_enabled
                else (
                    "No open tickets"
                    if not snapshot.has_open_tickets
                    else "Ticket flow is already active"
                )
            ),
            surface_visibility={"queue": True, "flow_status": False},
        )
    )

    resume_enabled = has_run and is_paused
    actions.append(
        FlowActionDescriptor(
            action="resume",
            enabled=resume_enabled,
            label="Resume",
            tone="success",
            style="success",
            disabled_reason=None if resume_enabled else "Run is not paused",
            surface_visibility={"queue": True, "flow_status": True},
        )
    )

    stop_enabled = has_run and is_active
    actions.append(
        FlowActionDescriptor(
            action="stop",
            enabled=stop_enabled,
            label="Stop",
            tone="danger",
            style="danger",
            disabled_reason=None if stop_enabled else "No active flow run",
            surface_visibility={"queue": True, "flow_status": True},
        )
    )

    recover_enabled = has_run and is_active and worker_unhealthy
    actions.append(
        FlowActionDescriptor(
            action="recover",
            enabled=recover_enabled,
            label="Recover",
            tone="warning",
            style="secondary",
            disabled_reason=(
                None
                if recover_enabled
                else (
                    "Worker is healthy"
                    if has_run and is_active
                    else "No active flow run to recover"
                )
            ),
            surface_visibility={"queue": False, "flow_status": True},
        )
    )

    restart_enabled = has_run and (is_paused or is_terminal)
    actions.append(
        FlowActionDescriptor(
            action="restart",
            enabled=restart_enabled,
            label="Restart",
            tone="secondary",
            style="secondary",
            requires_confirmation=True,
            disabled_reason=None if restart_enabled else "No restartable flow run",
            surface_visibility={"queue": True, "flow_status": True},
        )
    )

    retire_enabled = has_run and snapshot.retire_mode in {"ready", "confirm"}
    actions.append(
        FlowActionDescriptor(
            action="retire",
            enabled=retire_enabled,
            label="Retire",
            tone="secondary",
            style="secondary",
            requires_confirmation=snapshot.retire_mode == "confirm",
            disabled_reason=(
                None
                if retire_enabled
                else (
                    "No flow run to retire"
                    if not has_run
                    else "Retire is blocked while the run is active"
                )
            ),
            surface_visibility={"queue": False, "flow_status": True},
        )
    )

    refresh_enabled = has_run and not is_paused
    actions.append(
        FlowActionDescriptor(
            action="refresh",
            enabled=refresh_enabled,
            label="Refresh",
            tone="secondary",
            style="secondary",
            disabled_reason=None if refresh_enabled else "No refreshable flow run",
            surface_visibility={"queue": False, "flow_status": True},
        )
    )

    return actions


def build_flow_action_policy_payload(
    snapshot: FlowActionPolicySnapshot,
) -> list[dict[str, Any]]:
    return [descriptor.to_dict() for descriptor in build_flow_action_policy(snapshot)]


def flow_action_descriptors_for_surface(
    descriptors: list[FlowActionDescriptor],
    surface: str,
    *,
    enabled_only: bool = False,
) -> list[FlowActionDescriptor]:
    result: list[FlowActionDescriptor] = []
    for descriptor in descriptors:
        visibility = descriptor.surface_visibility or {}
        if visibility.get(surface) is not True:
            continue
        if enabled_only and not descriptor.enabled:
            continue
        result.append(descriptor)
    return result


__all__ = [
    "FlowActionDescriptor",
    "FlowActionPolicySnapshot",
    "build_flow_action_policy",
    "build_flow_action_policy_payload",
    "flow_action_descriptors_for_surface",
]
