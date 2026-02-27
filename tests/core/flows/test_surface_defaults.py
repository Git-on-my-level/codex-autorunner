from __future__ import annotations

from codex_autorunner.core.flows.surface_defaults import (
    normalize_flow_action,
    should_route_flow_read_to_hub_overview,
)


def test_normalize_flow_action_defaults_to_status() -> None:
    assert normalize_flow_action("") == "status"
    assert normalize_flow_action(None) == "status"


def test_route_status_to_hub_for_pma_or_unbound() -> None:
    assert should_route_flow_read_to_hub_overview(
        action="status",
        pma_enabled=True,
        has_workspace_binding=True,
    )
    assert should_route_flow_read_to_hub_overview(
        action="status",
        pma_enabled=False,
        has_workspace_binding=False,
    )
    assert should_route_flow_read_to_hub_overview(
        action="runs",
        pma_enabled=True,
        has_workspace_binding=True,
    )


def test_do_not_route_non_status_actions_or_explicit_target() -> None:
    assert not should_route_flow_read_to_hub_overview(
        action="issue",
        pma_enabled=True,
        has_workspace_binding=True,
    )
    assert not should_route_flow_read_to_hub_overview(
        action="status",
        pma_enabled=True,
        has_workspace_binding=False,
        has_explicit_target=True,
    )
