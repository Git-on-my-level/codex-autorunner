from __future__ import annotations

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.core.orchestration import (
    build_agent_definition,
    get_agent_definition,
    list_agent_definitions,
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
            "durable_threads",
            "message_turns",
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
        frozenset(["durable_threads", "message_turns", "review"]),
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
        ["interrupt", "transcript_history"],
    )

    assert merged == frozenset(
        [
            "durable_threads",
            "message_turns",
            "event_streaming",
            "interrupt",
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
