from __future__ import annotations

import pytest

from codex_autorunner.agents.types import AgentId
from codex_autorunner.core.domain.refs import AgentRef, ScopeRef, SurfaceRef
from codex_autorunner.core.orchestration import (
    AgentDefinition,
    BackendBinding,
    Binding,
    ExecutionRecord,
    FlowTarget,
    MessageRequest,
    Thread,
    ThreadTarget,
)


def test_agent_definition_serializes_defaults() -> None:
    definition = AgentDefinition(
        agent_id=AgentId("codex"),
        display_name="Codex",
        runtime_kind="codex",
    )

    assert definition.capabilities == frozenset()
    assert definition.available is True
    assert definition.to_dict() == {
        "agent_id": "codex",
        "available": True,
        "capabilities": frozenset(),
        "default_model": None,
        "description": None,
        "display_name": "Codex",
        "repo_id": None,
        "runtime_kind": "codex",
        "workspace_root": None,
    }


def test_thread_target_normalizes_managed_thread_mapping() -> None:
    target = ThreadTarget.from_mapping(
        {
            "managed_thread_id": "mt-1",
            "agent": "codex",
            "repo_id": "repo-1",
            "workspace_root": "/tmp/repo",
            "name": "Backlog Thread",
            "normalized_status": "running",
            "metadata": {"thread_kind": "ticket_flow"},
        }
    )

    assert target.thread_target_id == "mt-1"
    assert target.agent_id == AgentId("codex")
    assert target.resource_kind == "repo"
    assert target.resource_id == "repo-1"
    assert target.status == "running"
    assert target.thread_kind == "ticket_flow"
    assert target.to_dict()["backend_thread_id"] is None
    assert target.to_dict()["agent"] == "codex"


def test_thread_target_normalizes_agent_id_round_trip_field() -> None:
    target = ThreadTarget.from_mapping(
        {
            "thread_target_id": "mt-2",
            "agent_id": "hermes",
            "workspace_root": "/tmp/repo",
            "metadata": {"agent_profile": "m4-pma"},
        }
    )

    assert target.agent_id == AgentId("hermes")
    assert target.agent_profile == "m4-pma"


def test_binding_normalizes_surface_mapping() -> None:
    binding = Binding.from_mapping(
        {
            "binding_id": "binding-1",
            "surface_kind": "telegram",
            "surface_key": "chat:topic",
            "thread_id": "thread-1",
            "agent": "opencode",
            "repo_id": "repo-1",
            "mode": "reuse",
        }
    )

    assert binding.thread_target_id == "thread-1"
    assert binding.agent_id == AgentId("opencode")
    assert binding.resource_kind == "repo"
    assert binding.resource_id == "repo-1"
    assert binding.to_dict()["surface_kind"] == "telegram"


def test_thread_target_preserves_agent_workspace_owner() -> None:
    target = ThreadTarget.from_mapping(
        {
            "managed_thread_id": "mt-zc-1",
            "agent": "codex",
            "resource_kind": "agent_workspace",
            "resource_id": "zc-main",
            "workspace_root": "/tmp/runtimes/zeroclaw/zc-main",
            "normalized_status": "idle",
        }
    )

    assert target.resource_kind == "agent_workspace"
    assert target.resource_id == "zc-main"
    assert target.repo_id is None


def test_thread_model_preserves_canonical_refs_and_legacy_aliases() -> None:
    thread = Thread(
        id="thread-1",
        scope=ScopeRef(kind="repo", id="repo-1"),
        surface=SurfaceRef(kind="discord", key="guild:channel"),
        agent=AgentRef(agent_id="codex", profile="pma"),
        backend_binding=BackendBinding(
            backend_thread_id="backend-1",
            backend_runtime_instance_id="runtime-1",
        ),
        display_name="Backlog",
    )

    payload = thread.to_dict()

    assert payload["managed_thread_id"] == "thread-1"
    assert payload["thread_target_id"] == "thread-1"
    assert payload["scope_urn"] == "repo:repo-1"
    assert payload["surface_urn"] == "discord:guild%3Achannel"
    assert payload["agent"] == "codex"
    assert payload["agent_ref"] == {"agent_id": "codex", "profile": "pma"}
    assert payload["backend_thread_id"] == "backend-1"
    assert payload["backend_runtime_instance_id"] == "runtime-1"
    assert payload["status"] == "idle"
    assert payload["normalized_status"] == "idle"


def test_thread_model_hydrates_from_legacy_owner_fields() -> None:
    thread = Thread.from_mapping(
        {
            "managed_thread_id": "thread-1",
            "agent": "codex",
            "repo_id": "repo-1",
            "surface_urn": "discord:guild%3Achannel",
            "name": "Backlog",
            "normalized_status": "running",
        }
    )

    assert thread.scope == ScopeRef(kind="repo", id="repo-1")
    assert thread.surface == SurfaceRef(kind="discord", key="guild:channel")
    assert thread.runtime_status == "running"


def test_binding_requires_thread_target_id() -> None:
    with pytest.raises(ValueError, match="thread target id"):
        Binding.from_mapping(
            {
                "binding_id": "binding-1",
                "surface_kind": "discord",
                "surface_key": "chan-1",
            }
        )


def test_message_execution_and_flow_targets_serialize() -> None:
    request = MessageRequest(
        target_id="thread-1",
        target_kind="thread",
        message_text="Ship it",
        kind="review",
        model="gpt-5",
        reasoning="medium",
    )
    execution = ExecutionRecord(
        execution_id="exec-1",
        target_id="thread-1",
        target_kind="thread",
        request_kind="review",
        status="running",
        backend_id="turn-1",
    )
    flow = FlowTarget(
        flow_target_id="ticket-flow",
        flow_type="ticket_flow",
        display_name="Ticket Flow",
        repo_id="repo-1",
    )

    assert request.to_dict()["kind"] == "review"
    assert execution.to_dict()["request_kind"] == "review"
    assert execution.to_dict()["backend_id"] == "turn-1"
    assert flow.to_dict()["flow_type"] == "ticket_flow"
