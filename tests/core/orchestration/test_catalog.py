from __future__ import annotations

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.core.orchestration import (
    build_agent_definition,
    get_agent_definition,
    list_agent_definitions,
    map_agent_capabilities,
)


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
        ["threads", "turns", "review", "model_listing", "event_streaming"]
    )

    assert capabilities == frozenset(
        [
            "durable_threads",
            "message_turns",
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
        workspace_root="/tmp/repo",
        available=False,
    )

    assert definition.agent_id == "codex"
    assert definition.runtime_kind == "codex"
    assert definition.display_name == "Codex"
    assert definition.repo_id == "repo-1"
    assert definition.workspace_root == "/tmp/repo"
    assert definition.available is False
    assert definition.capabilities == frozenset(
        ["durable_threads", "message_turns", "review"]
    )


def test_list_and_lookup_agent_definitions_are_pma_agnostic() -> None:
    descriptors = {
        "opencode": _make_descriptor(
            "opencode",
            "OpenCode",
            frozenset(["threads", "turns", "event_streaming"]),
        ),
        "codex": _make_descriptor(
            "codex",
            "Codex",
            frozenset(["threads", "turns", "review", "approvals"]),
        ),
    }

    definitions = list_agent_definitions(
        descriptors,
        repo_id="repo-1",
        workspace_root="/tmp/repo",
        availability={"codex": True, "opencode": False},
    )

    assert [definition.display_name for definition in definitions] == [
        "Codex",
        "OpenCode",
    ]
    assert definitions[0].runtime_kind == "codex"
    assert definitions[1].available is False

    lookup = get_agent_definition(
        descriptors,
        "codex",
        repo_id="repo-1",
        workspace_root="/tmp/repo",
        availability={"codex": True},
    )

    assert lookup is not None
    assert lookup.agent_id == "codex"
    assert "approvals" in lookup.capabilities
