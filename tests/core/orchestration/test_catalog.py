from __future__ import annotations

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.core.orchestration import (
    build_agent_definition,
    build_native_target_definition,
    get_agent_definition,
    get_native_target_definition,
    is_helper_subsystem,
    is_native_target,
    list_agent_definitions,
    list_native_target_definitions,
    map_agent_capabilities,
    merge_agent_capabilities,
)

_WORKSPACE_ROOT = "/workspace/repo"


def _make_descriptor(
    agent_id: str,
    name: str,
    capabilities: frozenset[str],
) -> AgentDescriptor:
    return AgentDescriptor(
        id=agent_id,
        name=name,
        capabilities=capabilities,  # type: ignore[arg-type]
        make_harness=lambda _ctx: None,  # type: ignore[return-value]
    )


def test_map_agent_capabilities_uses_orchestration_vocabulary() -> None:
    capabilities = map_agent_capabilities(
        [
            "threads",
            "turns",
            "interrupt",
            "active_thread_discovery",
            "review",
            "model_listing",
            "event_streaming",
        ]
    )

    assert capabilities == frozenset(
        [
            "durable_threads",
            "message_turns",
            "interrupt",
            "active_thread_discovery",
            "review",
            "model_listing",
            "event_streaming",
        ]
    )


def test_build_agent_definition_preserves_registry_boundary() -> None:
    descriptor = _make_descriptor(
        "codex",
        "Codex",
        frozenset(["threads", "turns", "review"]),
    )

    definition = build_agent_definition(
        descriptor,
        repo_id="repo-1",
        workspace_root=_WORKSPACE_ROOT,
        available=False,
    )

    assert definition.agent_id == "codex"
    assert definition.runtime_kind == "codex"
    assert definition.display_name == "Codex"
    assert definition.repo_id == "repo-1"
    assert definition.workspace_root == _WORKSPACE_ROOT
    assert definition.available is False
    assert definition.capabilities == frozenset(
        ["durable_threads", "message_turns", "review"]
    )


def test_merge_agent_capabilities_keeps_optional_runtime_features_visible() -> None:
    merged = merge_agent_capabilities(
        ["durable_threads", "message_turns", "event_streaming"],
        ["interrupt", "structured_event_streaming", "transcript_history"],
    )

    assert merged == frozenset(
        [
            "durable_threads",
            "message_turns",
            "event_streaming",
            "interrupt",
            "structured_event_streaming",
            "transcript_history",
        ]
    )


def test_list_and_lookup_agent_definitions_are_pma_agnostic() -> None:
    descriptors = {
        "opencode": _make_descriptor(
            "opencode",
            "OpenCode",
            frozenset(["durable_threads", "message_turns", "event_streaming"]),
        ),
        "codex": _make_descriptor(
            "codex",
            "Codex",
            frozenset(["durable_threads", "message_turns", "review", "approvals"]),
        ),
    }

    definitions = list_agent_definitions(
        descriptors,
        repo_id="repo-1",
        workspace_root=_WORKSPACE_ROOT,
        availability={"codex": True, "opencode": False},
        runtime_capability_reports={
            "codex": ["interrupt", "active_thread_discovery"],
        },
    )

    assert [definition.display_name for definition in definitions] == [
        "Codex",
        "OpenCode",
    ]
    assert definitions[0].runtime_kind == "codex"
    assert definitions[1].available is False
    assert "interrupt" in definitions[0].capabilities

    lookup = get_agent_definition(
        descriptors,
        "codex",
        repo_id="repo-1",
        workspace_root=_WORKSPACE_ROOT,
        availability={"codex": True},
        runtime_capability_reports={
            "codex": ["interrupt", "active_thread_discovery"],
        },
    )

    assert lookup is not None
    assert lookup.agent_id == "codex"
    assert "approvals" in lookup.capabilities
    assert "active_thread_discovery" in lookup.capabilities


def test_native_target_definitions_list_ticket_flow_and_pma() -> None:
    definitions = list_native_target_definitions()
    display_names = [d.display_name for d in definitions]
    target_kinds = [d.target_kind for d in definitions]

    assert "Ticket Flow" in display_names
    assert "PMA" in display_names
    assert "ticket_flow" in target_kinds
    assert "pma" in target_kinds


def test_native_target_definition_has_correct_target_family() -> None:
    ticket_flow = get_native_target_definition("ticket_flow")
    assert ticket_flow is not None
    assert ticket_flow.target_kind == "ticket_flow"
    assert ticket_flow.display_name == "Ticket Flow"
    assert ticket_flow.description is not None
    assert "workflow" in ticket_flow.description.lower()

    pma = get_native_target_definition("pma")
    assert pma is not None
    assert pma.target_kind == "pma"
    assert pma.display_name == "PMA"
    assert pma.description is not None
    assert "thread" in pma.description.lower()


def test_build_native_target_definition_with_custom_availability() -> None:
    definition = build_native_target_definition(
        "ticket_flow",
        repo_id="repo-1",
        workspace_root=_WORKSPACE_ROOT,
        available=False,
    )

    assert definition.target_id == "ticket_flow"
    assert definition.repo_id == "repo-1"
    assert definition.workspace_root == _WORKSPACE_ROOT
    assert definition.available is False


def test_is_native_target_distinguishes_from_runtime_agents() -> None:
    assert is_native_target("ticket_flow") is True
    assert is_native_target("pma") is True
    assert is_native_target("codex") is False
    assert is_native_target("opencode") is False
    assert is_native_target("unknown") is False


def test_is_helper_subsystem_identifies_internal_plumbing() -> None:
    assert is_helper_subsystem("dispatch_handler") is True
    assert is_helper_subsystem("reactive_debouncer") is True
    assert is_helper_subsystem("event_projection") is True
    assert is_helper_subsystem("transcript_mirror_service") is True

    assert is_helper_subsystem("ticket_flow") is False
    assert is_helper_subsystem("pma") is False
    assert is_helper_subsystem("codex") is False
    assert is_helper_subsystem("opencode") is False


def test_native_target_catalog_groups_targets_by_family() -> None:
    from codex_autorunner.core.orchestration.catalog import NativeTargetCatalog

    catalog = NativeTargetCatalog(
        repo_id="repo-1",
        workspace_root=_WORKSPACE_ROOT,
        availability={"ticket_flow": True, "pma": False},
    )

    definitions = catalog.list_definitions()
    assert len(definitions) == 2

    ticket_flow = catalog.get_definition("ticket_flow")
    assert ticket_flow is not None
    assert ticket_flow.available is True

    pma = catalog.get_definition("pma")
    assert pma is not None
    assert pma.available is False
