from __future__ import annotations

from dataclasses import replace

import pytest

from codex_autorunner.adapters.chat.canonical_turns import (
    build_surface_turn_execution_request,
)
from codex_autorunner.adapters.chat.command_contract import COMMAND_CONTRACT
from codex_autorunner.core.orchestration import MessageRequest, TurnExecutionRequest


def _message_request() -> MessageRequest:
    return MessageRequest(
        target_id="thread-1",
        target_kind="thread",
        message_text="review this change",
        busy_policy="queue",
        model="gpt-5.4",
        reasoning="high",
        approval_mode="never",
        input_items=[{"type": "text", "text": "review this change"}],
        metadata={"runtime_prompt": "runtime review prompt"},
    )


def test_discord_and_telegram_surface_turns_preserve_equivalent_canonical_fields() -> (
    None
):
    common = {
        "workspace_root": "/repo",
        "agent": "codex",
        "approval_policy": "never",
        "sandbox_policy": "dangerFullAccess",
        "profile": "reviewer",
    }

    discord = build_surface_turn_execution_request(
        _message_request(),
        request_id="discord-request",
        surface_kind="discord",
        surface_key="channel-1",
        client_request_id="client-discord",
        **common,
    )
    telegram = build_surface_turn_execution_request(
        _message_request(),
        request_id="telegram-request",
        surface_kind="telegram",
        surface_key="chat-1/thread-2",
        client_request_id="client-telegram",
        **common,
    )

    for field in (
        "target_id",
        "target_kind",
        "workspace_root",
        "request_kind",
        "busy_policy",
        "prompt_text",
        "input_items",
        "agent",
        "profile",
        "model",
        "reasoning",
        "approval_policy",
        "approval_mode",
        "sandbox_policy",
        "metadata",
    ):
        assert getattr(discord, field) == getattr(telegram, field)
    assert discord.origin.surface_kind == "discord"
    assert telegram.origin.surface_kind == "telegram"


def test_surface_turn_builder_validates_opencode_provider_model_payload() -> None:
    request = replace(_message_request(), model="zai-coding-plan/glm-5.1")

    turn_request = build_surface_turn_execution_request(
        request,
        request_id="opencode-request",
        workspace_root="/repo",
        surface_kind="discord",
        surface_key="channel-1",
        agent="opencode",
        approval_policy="never",
        sandbox_policy="dangerFullAccess",
    )

    assert turn_request.model_payload == {
        "providerID": "zai-coding-plan",
        "modelID": "glm-5.1",
    }


def test_surface_turn_builder_records_codex_default_model_when_implicit() -> None:
    request = replace(_message_request(), model=None)

    turn_request = build_surface_turn_execution_request(
        request,
        request_id="codex-request",
        workspace_root="/repo",
        surface_kind="discord",
        surface_key="channel-1",
        agent="codex",
        approval_policy="never",
        sandbox_policy="dangerFullAccess",
    )

    assert turn_request.model == "gpt-5.5"


def test_surface_turn_builder_prefers_configured_default_model() -> None:
    request = replace(_message_request(), model=None)

    turn_request = build_surface_turn_execution_request(
        request,
        request_id="codex-request",
        workspace_root="/repo",
        surface_kind="discord",
        surface_key="channel-1",
        agent="codex",
        approval_policy="never",
        sandbox_policy="dangerFullAccess",
        configured_default_model="gpt-configured",
    )

    assert turn_request.model == "gpt-configured"


def test_surface_turn_builder_prefers_explicit_model_over_configured_default() -> None:
    request = replace(_message_request(), model="gpt-explicit")

    turn_request = build_surface_turn_execution_request(
        request,
        request_id="codex-request",
        workspace_root="/repo",
        surface_kind="discord",
        surface_key="channel-1",
        agent="codex",
        approval_policy="never",
        sandbox_policy="dangerFullAccess",
        configured_default_model="gpt-configured",
    )

    assert turn_request.model == "gpt-explicit"


def test_surface_turn_builder_leaves_unknown_agent_model_unset() -> None:
    request = replace(_message_request(), model=None)

    turn_request = build_surface_turn_execution_request(
        request,
        request_id="custom-agent-request",
        workspace_root="/repo",
        surface_kind="discord",
        surface_key="channel-1",
        agent="custom-agent",
        approval_policy="never",
        sandbox_policy="dangerFullAccess",
    )

    assert turn_request.model is None
    assert turn_request.model_payload == {}


@pytest.mark.parametrize("command_id", ["car.new", "car.files.inbox"])
def test_stable_and_partial_command_metadata_can_build_valid_surface_turns(
    command_id: str,
) -> None:
    entry = next(item for item in COMMAND_CONTRACT if item.id == command_id)
    assert entry.status in {"stable", "partial"}
    assert entry.telegram_commands

    turn_request = build_surface_turn_execution_request(
        _message_request(),
        request_id=f"{command_id}:request",
        workspace_root="/repo",
        surface_kind="telegram",
        surface_key=f"command:{entry.telegram_commands[0]}",
        agent="codex",
        approval_policy="never",
        sandbox_policy="dangerFullAccess",
        origin_metadata={
            "command_id": entry.id,
            "command_status": entry.status,
            "requires_bound_workspace": entry.requires_bound_workspace,
        },
    )

    assert isinstance(
        TurnExecutionRequest.from_mapping(turn_request.to_dict()),
        TurnExecutionRequest,
    )
    assert turn_request.origin.metadata["command_id"] == command_id
