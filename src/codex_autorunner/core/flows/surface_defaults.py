from __future__ import annotations

from typing import Optional

_HUB_OVERVIEW_FLOW_ACTIONS = frozenset({"status", "runs"})


def normalize_flow_action(action: Optional[str]) -> str:
    normalized = str(action or "").strip().lower()
    if not normalized:
        return "status"
    return normalized


def should_route_flow_read_to_hub_overview(
    *,
    action: Optional[str],
    pma_enabled: bool,
    has_workspace_binding: bool,
    has_explicit_target: bool = False,
) -> bool:
    """Return True when a surface should default to hub-level flow overview."""
    if has_explicit_target:
        return False
    normalized = normalize_flow_action(action)
    if normalized not in _HUB_OVERVIEW_FLOW_ACTIONS:
        return False
    return bool(pma_enabled) or not bool(has_workspace_binding)
