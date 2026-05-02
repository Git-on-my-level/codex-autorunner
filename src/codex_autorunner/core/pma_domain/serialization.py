from __future__ import annotations

from typing import Any, Mapping, Optional

from .models import (
    PmaDispatchAttempt,
    PmaDispatchDecision,
    PmaOriginContext,
    _normalize_text,
)

# ---------------------------------------------------------------------------
# PmaOriginContext
# ---------------------------------------------------------------------------


def normalize_pma_origin_context(value: Any) -> Optional[PmaOriginContext]:
    if not isinstance(value, dict):
        return None
    origin = PmaOriginContext(
        thread_id=_normalize_text(value.get("thread_id")),
        lane_id=_normalize_text(value.get("lane_id")),
        agent=_normalize_text(value.get("agent")),
        profile=_normalize_text(value.get("profile")),
    )
    return None if origin.is_empty() else origin


# ---------------------------------------------------------------------------
# PmaDispatchDecision / PmaDispatchAttempt
# ---------------------------------------------------------------------------


def normalize_pma_dispatch_attempt(data: Any) -> Optional[PmaDispatchAttempt]:
    if isinstance(data, PmaDispatchAttempt):
        return data
    if not isinstance(data, Mapping):
        return None
    route = _normalize_text(data.get("route"))
    delivery_mode = _normalize_text(data.get("delivery_mode"))
    surface_kind = _normalize_text(data.get("surface_kind"))
    if route is None or delivery_mode is None or surface_kind is None:
        return None
    workspace_root_raw = _normalize_text(data.get("workspace_root"))
    return PmaDispatchAttempt(
        route=route,
        delivery_mode=delivery_mode,
        surface_kind=surface_kind,
        surface_key=_normalize_text(data.get("surface_key")),
        repo_id=_normalize_text(data.get("repo_id")),
        workspace_root=workspace_root_raw,
    )


def normalize_pma_dispatch_decision(data: Any) -> Optional[PmaDispatchDecision]:
    if isinstance(data, PmaDispatchDecision):
        return data
    if not isinstance(data, Mapping):
        return None
    requested_delivery = _normalize_text(data.get("requested_delivery"))
    if requested_delivery is None:
        return None
    attempts_raw = data.get("attempts")
    attempts: list[PmaDispatchAttempt] = []
    if isinstance(attempts_raw, (list, tuple)):
        for entry in attempts_raw:
            attempt = normalize_pma_dispatch_attempt(entry)
            if attempt is not None:
                attempts.append(attempt)
    return PmaDispatchDecision(
        requested_delivery=requested_delivery,
        suppress_publish=bool(data.get("suppress_publish")),
        attempts=tuple(attempts),
    )


def pma_dispatch_decision_to_dict(decision: PmaDispatchDecision) -> dict[str, Any]:
    return {
        "requested_delivery": decision.requested_delivery,
        "suppress_publish": bool(decision.suppress_publish),
        "attempts": [
            {
                "route": attempt.route,
                "delivery_mode": attempt.delivery_mode,
                "surface_kind": attempt.surface_kind,
                "surface_key": attempt.surface_key,
                "repo_id": attempt.repo_id,
                "workspace_root": attempt.workspace_root,
            }
            for attempt in decision.attempts
        ],
    }
