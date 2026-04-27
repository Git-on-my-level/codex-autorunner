from __future__ import annotations

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.agents.types import normalize_runtime_capabilities
from codex_autorunner.core.orchestration.catalog import (
    build_agent_definition,
    merge_agent_capabilities,
)


def _make_descriptor(capabilities: frozenset[str]) -> AgentDescriptor:
    return AgentDescriptor(
        id="test-agent",
        name="Test Agent",
        capabilities=capabilities,  # type: ignore[arg-type]
        make_harness=lambda _ctx: None,  # type: ignore[return-value]
    )


def test_normalize_runtime_capabilities_normalizes_case_and_whitespace() -> None:
    capabilities = normalize_runtime_capabilities(
        [
            " durable_threads ",
            "MESSAGE_TURNS",
            "review",
            "active_thread_discovery",
        ]
    )

    assert capabilities == frozenset(
        [
            "durable_threads",
            "message_turns",
            "review",
            "active_thread_discovery",
        ]
    )


def test_agent_descriptor_normalizes_static_capabilities() -> None:
    descriptor = _make_descriptor(
        frozenset(["durable_threads", "message_turns", "review", "model_listing"])
    )

    assert descriptor.capabilities == frozenset(
        [
            "durable_threads",
            "message_turns",
            "review",
            "model_listing",
        ]
    )


def test_merge_agent_capabilities_adds_runtime_reported_features() -> None:
    merged = merge_agent_capabilities(
        ["durable_threads", "message_turns", "event_streaming"],
        ["interrupt", "active_thread_discovery"],
    )

    assert merged == frozenset(
        [
            "durable_threads",
            "message_turns",
            "event_streaming",
            "interrupt",
            "active_thread_discovery",
        ]
    )


def test_build_agent_definition_merges_static_and_runtime_capabilities() -> None:
    descriptor = _make_descriptor(
        frozenset(["durable_threads", "message_turns", "review"])
    )

    definition = build_agent_definition(
        descriptor,
        runtime_capabilities=["interrupt", "active_thread_discovery"],
    )

    assert definition.capabilities == frozenset(
        [
            "durable_threads",
            "message_turns",
            "review",
            "interrupt",
            "active_thread_discovery",
        ]
    )


def test_zeroclaw_descriptor_advertises_only_supported_durable_capabilities() -> None:
    from codex_autorunner.agents.registry import get_registered_agents

    descriptor = get_registered_agents()["zeroclaw"]

    assert descriptor.capabilities == frozenset(
        [
            "durable_threads",
            "message_turns",
            "active_thread_discovery",
            "event_streaming",
        ]
    )
