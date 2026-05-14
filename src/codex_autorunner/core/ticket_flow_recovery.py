from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

from .text_utils import _iso_now


class RecoveryFacetName(str, Enum):
    COMMIT_BARRIER = "commit_barrier"
    RESTART = "restart"
    WORKER_HEALTH = "worker_health"
    STALE_ALIVE = "stale_alive"
    DISPATCH_PAUSE = "dispatch_pause"
    TERMINAL_FAILURE = "terminal_failure"


class RecoveryFacetStatus(str, Enum):
    CLEAR = "clear"
    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    RESOLVED = "resolved"


class RecoveryIntentSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class RecoveryFacet:
    name: RecoveryFacetName
    status: RecoveryFacetStatus
    attention_required: bool = False
    reason: Optional[str] = None
    recommended_actions: tuple[str, ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name.value,
            "status": self.status.value,
            "attention_required": self.attention_required,
            "reason": self.reason,
            "recommended_actions": list(self.recommended_actions),
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class RecoveryProjection:
    schema_version: int
    run_id: str
    primary_state: str
    attention_required: bool
    facets: Mapping[str, RecoveryFacet]
    recommended_actions: tuple[str, ...] = ()
    observed_at: str = field(default_factory=_iso_now)

    def active_facets(self) -> tuple[RecoveryFacet, ...]:
        return tuple(
            facet
            for facet in self.facets.values()
            if facet.status != RecoveryFacetStatus.CLEAR
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "primary_state": self.primary_state,
            "attention_required": self.attention_required,
            "recommended_actions": list(self.recommended_actions),
            "observed_at": self.observed_at,
            "facets": {name: facet.to_dict() for name, facet in self.facets.items()},
        }


@dataclass(frozen=True)
class RecoveryNotificationIntent:
    intent_id: str
    run_id: str
    event_type: str
    severity: RecoveryIntentSeverity
    reason: str
    recommended_actions: tuple[str, ...] = ()
    cooldown_seconds: int = 3600
    resolved: bool = False
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "severity": self.severity.value,
            "reason": self.reason,
            "recommended_actions": list(self.recommended_actions),
            "cooldown_seconds": self.cooldown_seconds,
            "resolved": self.resolved,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class RecoveryNotificationIntentRecord:
    intent_id: str
    run_id: str
    event_type: str
    severity: str
    reason: str
    recommended_actions: tuple[str, ...]
    cooldown_seconds: int
    resolved: bool
    first_seen_at: str
    last_observed_at: str
    last_notified_at: Optional[str]
    resolved_at: Optional[str]
    observed_count: int
    payload: Mapping[str, Any]
    delivery_attempts: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "severity": self.severity,
            "reason": self.reason,
            "recommended_actions": list(self.recommended_actions),
            "cooldown_seconds": self.cooldown_seconds,
            "resolved": self.resolved,
            "first_seen_at": self.first_seen_at,
            "last_observed_at": self.last_observed_at,
            "last_notified_at": self.last_notified_at,
            "resolved_at": self.resolved_at,
            "observed_count": self.observed_count,
            "payload": dict(self.payload),
            "delivery_attempts": dict(self.delivery_attempts),
        }


def build_recovery_projection(
    *,
    run_id: str,
    state: str,
    recovery_state: Optional[str],
    attention_required: bool,
    recommended_actions: list[str],
    worker_status: Optional[str],
    blocking_reason: Optional[str],
    restart_attempts: int,
    restart_max_attempts: Optional[int],
    restart_exhausted: bool,
    commit_barrier_pending: bool,
    commit_barrier: Optional[Mapping[str, Any]],
    stale_alive: Optional[Mapping[str, Any]],
    dispatch_pause_pending: bool,
    terminal_failure: bool,
    crash_reason: Optional[str] = None,
    reap_reason: Optional[str] = None,
) -> RecoveryProjection:
    actions = tuple(recommended_actions)
    primary_state = recovery_state or state
    facets: dict[str, RecoveryFacet] = {}

    commit_barrier_exhausted = bool(
        commit_barrier_pending
        and isinstance(commit_barrier, Mapping)
        and (
            commit_barrier.get("exhausted")
            or commit_barrier.get("resolution_state") == "exhausted"
        )
    )
    facets[RecoveryFacetName.COMMIT_BARRIER.value] = RecoveryFacet(
        RecoveryFacetName.COMMIT_BARRIER,
        (
            RecoveryFacetStatus.EXHAUSTED
            if commit_barrier_exhausted
            else (
                RecoveryFacetStatus.ACTIVE
                if commit_barrier_pending
                else RecoveryFacetStatus.CLEAR
            )
        ),
        attention_required=commit_barrier_pending,
        reason=(
            "commit-barrier-retry-budget-exhausted"
            if commit_barrier_exhausted
            else (
                "done-current-ticket-has-uncommitted-worktree-changes"
                if commit_barrier_pending
                else None
            )
        ),
        recommended_actions=actions if commit_barrier_pending else (),
        data=dict(commit_barrier or {}),
    )

    restart_status = RecoveryFacetStatus.CLEAR
    if restart_exhausted:
        restart_status = RecoveryFacetStatus.EXHAUSTED
    elif restart_attempts > 0:
        restart_status = RecoveryFacetStatus.ACTIVE
    facets[RecoveryFacetName.RESTART.value] = RecoveryFacet(
        RecoveryFacetName.RESTART,
        restart_status,
        attention_required=restart_exhausted,
        reason="restart-attempts-exhausted" if restart_exhausted else None,
        recommended_actions=actions if restart_exhausted else (),
        data={
            "attempts": restart_attempts,
            "max_attempts": restart_max_attempts,
            "exhausted": restart_exhausted,
        },
    )

    worker_attention = bool(worker_status in {"dead_unexpected", "stale_alive"})
    facets[RecoveryFacetName.WORKER_HEALTH.value] = RecoveryFacet(
        RecoveryFacetName.WORKER_HEALTH,
        RecoveryFacetStatus.ACTIVE if worker_attention else RecoveryFacetStatus.CLEAR,
        attention_required=worker_attention,
        reason=blocking_reason if worker_attention else None,
        recommended_actions=actions if worker_attention else (),
        data={
            "worker_status": worker_status,
            "crash_reason": crash_reason,
            "reap_reason": reap_reason,
        },
    )

    stale_alive_active = bool(stale_alive)
    facets[RecoveryFacetName.STALE_ALIVE.value] = RecoveryFacet(
        RecoveryFacetName.STALE_ALIVE,
        RecoveryFacetStatus.ACTIVE if stale_alive_active else RecoveryFacetStatus.CLEAR,
        attention_required=bool(worker_status == "stale_alive"),
        reason=(
            _normalize_optional_text(stale_alive.get("reason"))
            if isinstance(stale_alive, Mapping)
            else None
        ),
        recommended_actions=actions if worker_status == "stale_alive" else (),
        data=dict(stale_alive or {}),
    )

    facets[RecoveryFacetName.DISPATCH_PAUSE.value] = RecoveryFacet(
        RecoveryFacetName.DISPATCH_PAUSE,
        (
            RecoveryFacetStatus.ACTIVE
            if dispatch_pause_pending
            else RecoveryFacetStatus.CLEAR
        ),
        attention_required=dispatch_pause_pending,
        reason="waiting-for-user-dispatch-reply" if dispatch_pause_pending else None,
        recommended_actions=actions if dispatch_pause_pending else (),
        data={"pending": dispatch_pause_pending},
    )

    facets[RecoveryFacetName.TERMINAL_FAILURE.value] = RecoveryFacet(
        RecoveryFacetName.TERMINAL_FAILURE,
        RecoveryFacetStatus.ACTIVE if terminal_failure else RecoveryFacetStatus.CLEAR,
        attention_required=terminal_failure,
        reason=blocking_reason if terminal_failure else None,
        recommended_actions=actions if terminal_failure else (),
        data={"crash_reason": crash_reason, "reap_reason": reap_reason},
    )

    return RecoveryProjection(
        schema_version=1,
        run_id=run_id,
        primary_state=primary_state,
        attention_required=attention_required,
        facets=facets,
        recommended_actions=actions,
    )


def build_recovery_notification_intents(
    projection: RecoveryProjection,
) -> tuple[RecoveryNotificationIntent, ...]:
    intents: list[RecoveryNotificationIntent] = []
    for facet in projection.active_facets():
        if not facet.attention_required:
            continue
        event_type = f"ticket_flow.{facet.name.value}.{facet.status.value}"
        intent_id = _intent_id(
            projection.run_id,
            event_type,
            _dedupe_key_for_facet(facet),
        )
        intents.append(
            RecoveryNotificationIntent(
                intent_id=intent_id,
                run_id=projection.run_id,
                event_type=event_type,
                severity=_severity_for_facet(facet),
                reason=facet.reason or event_type,
                recommended_actions=facet.recommended_actions,
                cooldown_seconds=_cooldown_for_facet(facet),
                resolved=False,
                payload={
                    "projection_schema_version": projection.schema_version,
                    "primary_state": projection.primary_state,
                    "facet": facet.to_dict(),
                },
            )
        )
    return tuple(intents)


def _intent_id(run_id: str, event_type: str, dedupe_key: str) -> str:
    raw = json.dumps(
        {"run_id": run_id, "event_type": event_type, "dedupe_key": dedupe_key},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"ticket_flow_recovery:{digest}"


def _dedupe_key_for_facet(facet: RecoveryFacet) -> str:
    data = facet.data
    if facet.name == RecoveryFacetName.COMMIT_BARRIER:
        return _first_text(
            data,
            ("barrier_epoch", "epoch", "token", "current_ticket", "ticket"),
            default="active",
        )
    if facet.name == RecoveryFacetName.RESTART:
        return _first_text(data, ("restart_epoch", "epoch"), default=facet.status.value)
    if facet.name == RecoveryFacetName.DISPATCH_PAUSE:
        return _first_text(data, ("dispatch_seq", "seq"), default="pending")
    if facet.name == RecoveryFacetName.TERMINAL_FAILURE:
        return _first_text(data, ("failure_epoch", "crash_reason"), default="failure")
    if facet.name in {
        RecoveryFacetName.WORKER_HEALTH,
        RecoveryFacetName.STALE_ALIVE,
    }:
        return _first_text(data, ("worker_epoch", "pid"), default=facet.name.value)
    return str(facet.name.value)


def _severity_for_facet(facet: RecoveryFacet) -> RecoveryIntentSeverity:
    if (
        facet.name
        in {
            RecoveryFacetName.RESTART,
            RecoveryFacetName.TERMINAL_FAILURE,
            RecoveryFacetName.WORKER_HEALTH,
        }
        and facet.attention_required
    ):
        return RecoveryIntentSeverity.CRITICAL
    if facet.attention_required:
        return RecoveryIntentSeverity.WARNING
    return RecoveryIntentSeverity.INFO


def _cooldown_for_facet(facet: RecoveryFacet) -> int:
    if facet.name == RecoveryFacetName.COMMIT_BARRIER:
        return 6 * 60 * 60
    if facet.name == RecoveryFacetName.RESTART:
        return 24 * 60 * 60
    return 60 * 60


def _first_text(data: Mapping[str, Any], keys: tuple[str, ...], *, default: str) -> str:
    for key in keys:
        candidate = _normalize_optional_text(data.get(key))
        if candidate is not None:
            return candidate
    return default


def _normalize_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "RecoveryFacet",
    "RecoveryFacetName",
    "RecoveryFacetStatus",
    "RecoveryIntentSeverity",
    "RecoveryNotificationIntent",
    "RecoveryNotificationIntentRecord",
    "RecoveryProjection",
    "build_recovery_notification_intents",
    "build_recovery_projection",
]
