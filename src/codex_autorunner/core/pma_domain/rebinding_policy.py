"""Domain-owned rebinding policy for delivery after persisted dispatch decisions.

When a dispatch decision is persisted but the underlying binding changes before
delivery completes (e.g., a thread moves to a different Discord channel), the
domain must decide whether to:

1. Keep the original routes (binding was stable).
2. Rebuild routes from the new binding state.
3. Suppress delivery if the binding change invalidates the reason for delivery.

All decisions are pure functions.  Adapters call
``evaluate_rebinding_decision`` before executing delivery and record the
result in the delivery ledger as domain metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class RebindingDecision(str, Enum):
    KEEP_ORIGINAL = "keep_original"
    REBUILD_ROUTES = "rebuild_routes"
    SUPPRESS = "suppress"


@dataclass(frozen=True)
class RebindingContext:
    persisted_surface_kind: Optional[str] = None
    persisted_surface_key: Optional[str] = None
    persisted_route: Optional[str] = None
    persisted_delivery_mode: Optional[str] = None
    current_surface_kind: Optional[str] = None
    current_surface_key: Optional[str] = None
    delivery_state: str = "pending"
    attempt_number: int = 0
    managed_thread_id: Optional[str] = None


@dataclass(frozen=True)
class RebindingResult:
    decision: RebindingDecision
    domain_reason: str
    effective_surface_kind: Optional[str] = None
    effective_surface_key: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict, hash=False)


def evaluate_rebinding_decision(context: RebindingContext) -> RebindingResult:
    if context.delivery_state in ("succeeded", "suppressed", "abandoned", "failed"):
        return RebindingResult(
            decision=RebindingDecision.KEEP_ORIGINAL,
            domain_reason=f"terminal_state:{context.delivery_state}",
            effective_surface_kind=context.persisted_surface_kind,
            effective_surface_key=context.persisted_surface_key,
        )

    persisted_key = _normalize(context.persisted_surface_key)
    current_key = _normalize(context.current_surface_key)
    persisted_kind = _normalize(context.persisted_surface_kind)
    current_kind = _normalize(context.current_surface_kind)

    binding_unchanged = persisted_kind == current_kind and persisted_key == current_key

    if binding_unchanged:
        return RebindingResult(
            decision=RebindingDecision.KEEP_ORIGINAL,
            domain_reason="binding_unchanged",
            effective_surface_kind=context.persisted_surface_kind,
            effective_surface_key=context.persisted_surface_key,
        )

    route = context.persisted_route or "auto"
    if route == "explicit":
        return RebindingResult(
            decision=RebindingDecision.SUPPRESS,
            domain_reason="explicit_target_binding_changed",
            effective_surface_kind=context.current_surface_kind,
            effective_surface_key=context.current_surface_key,
            metadata={
                "rebinding_trigger": "explicit_binding_drift",
                "original_surface_kind": context.persisted_surface_kind,
                "original_surface_key": context.persisted_surface_key,
            },
        )

    if current_kind is not None and current_key is not None:
        return RebindingResult(
            decision=RebindingDecision.REBUILD_ROUTES,
            domain_reason="binding_changed_rebuild_from_current",
            effective_surface_kind=current_kind,
            effective_surface_key=current_key,
            metadata={
                "rebinding_trigger": "binding_drift",
                "original_surface_kind": context.persisted_surface_kind,
                "original_surface_key": context.persisted_surface_key,
            },
        )

    return RebindingResult(
        decision=RebindingDecision.KEEP_ORIGINAL,
        domain_reason="binding_changed_no_current_target_fallback",
        effective_surface_kind=context.persisted_surface_kind,
        effective_surface_key=context.persisted_surface_key,
        metadata={
            "rebinding_trigger": "binding_drift_no_replacement",
            "original_surface_kind": context.persisted_surface_kind,
            "original_surface_key": context.persisted_surface_key,
        },
    )


def _normalize(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "RebindingContext",
    "RebindingDecision",
    "RebindingResult",
    "evaluate_rebinding_decision",
]
