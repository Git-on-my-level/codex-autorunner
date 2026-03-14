from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.agents.types import (
    ConversationRef,
    RuntimeCapability,
    TerminalTurnResult,
    TurnRef,
)
from codex_autorunner.core.config import (
    DEFAULT_REPO_CONFIG,
    TicketFlowConfig,
    _parse_app_server_config,
)
from codex_autorunner.integrations.agents.agent_pool_impl import DefaultAgentPool
from codex_autorunner.tickets.agent_pool import AgentTurnRequest


class _FakeHarness:
    def __init__(self):
        self.calls = []
        self._turn_id = 0

    display_name = "Fake Harness"
    capabilities = frozenset(
        [
            RuntimeCapability("durable_threads"),
            RuntimeCapability("message_turns"),
        ]
    )

    async def ensure_ready(self, workspace_root: Path) -> None:
        _ = workspace_root

    def supports(self, capability: str) -> bool:
        return RuntimeCapability(capability) in self.capabilities

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        _ = workspace_root, title
        return ConversationRef(agent="codex", id="thread-1")

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        _ = workspace_root
        return ConversationRef(agent="codex", id=conversation_id)

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> TurnRef:
        _ = workspace_root, model, reasoning, input_items
        self._turn_id += 1
        self.calls.append(
            {
                "conversation_id": conversation_id,
                "prompt": prompt,
                "approval_mode": approval_mode,
                "sandbox_policy": sandbox_policy,
            }
        )
        return TurnRef(conversation_id=conversation_id, turn_id=f"turn-{self._turn_id}")

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = workspace_root, conversation_id, turn_id, timeout
        return TerminalTurnResult(status="ok", assistant_text="ok", raw_events=[])


def _descriptor() -> AgentDescriptor:
    return AgentDescriptor(
        id="codex",
        name="Codex",
        capabilities=frozenset(
            {
                RuntimeCapability("durable_threads"),
                RuntimeCapability("message_turns"),
            }
        ),
        make_harness=lambda ctx: ctx.fake_harness,
    )


@pytest.mark.asyncio
async def test_agent_pool_respects_ticket_flow_approval_defaults(tmp_path: Path):
    app_server_cfg = _parse_app_server_config(
        None, tmp_path, DEFAULT_REPO_CONFIG["app_server"]
    )
    cfg = SimpleNamespace(
        root=tmp_path,
        app_server=app_server_cfg,
        opencode=SimpleNamespace(session_stall_timeout_seconds=None),
        ticket_flow=TicketFlowConfig(
            approval_mode="review",
            default_approval_decision="cancel",
            include_previous_ticket_context=False,
        ),
    )
    pool = DefaultAgentPool(cfg)  # type: ignore[arg-type]
    fake = _FakeHarness()
    pool._agent_descriptors_override = {"codex": _descriptor()}  # type: ignore[attr-defined]
    pool._harness_context_override = SimpleNamespace(fake_harness=fake)  # type: ignore[attr-defined]

    result = await pool.run_turn(
        AgentTurnRequest(agent_id="codex", prompt="hi", workspace_root=tmp_path)
    )

    assert result.text == "ok"
    assert fake.calls[0]["approval_mode"] == "on-request"
    assert fake.calls[0]["sandbox_policy"] == "workspaceWrite"


@pytest.mark.asyncio
async def test_agent_pool_uses_yolo_policy_for_ticket_flow(tmp_path: Path):
    app_server_cfg = _parse_app_server_config(
        None, tmp_path, DEFAULT_REPO_CONFIG["app_server"]
    )
    cfg = SimpleNamespace(
        root=tmp_path,
        app_server=app_server_cfg,
        opencode=SimpleNamespace(session_stall_timeout_seconds=None),
        ticket_flow=TicketFlowConfig(
            approval_mode="yolo",
            default_approval_decision="accept",
            include_previous_ticket_context=False,
        ),
    )
    pool = DefaultAgentPool(cfg)  # type: ignore[arg-type]
    fake = _FakeHarness()
    pool._agent_descriptors_override = {"codex": _descriptor()}  # type: ignore[attr-defined]
    pool._harness_context_override = SimpleNamespace(fake_harness=fake)  # type: ignore[attr-defined]

    await pool.run_turn(
        AgentTurnRequest(agent_id="codex", prompt="hi", workspace_root=tmp_path)
    )

    assert fake.calls[0]["approval_mode"] == "never"
    assert fake.calls[0]["sandbox_policy"] == "dangerFullAccess"


def test_parse_app_server_output_policy_default(tmp_path: Path) -> None:
    app_server_cfg = _parse_app_server_config(
        None, tmp_path, DEFAULT_REPO_CONFIG["app_server"]
    )
    assert app_server_cfg.output.policy == "final_only"


def test_parse_app_server_output_policy_override(tmp_path: Path) -> None:
    app_server_cfg = _parse_app_server_config(
        {"output": {"policy": "all_agent_messages"}},
        tmp_path,
        DEFAULT_REPO_CONFIG["app_server"],
    )
    assert app_server_cfg.output.policy == "all_agent_messages"


def test_parse_app_server_output_policy_invalid_falls_back(tmp_path: Path) -> None:
    app_server_cfg = _parse_app_server_config(
        {"output": {"policy": "invalid"}},
        tmp_path,
        DEFAULT_REPO_CONFIG["app_server"],
    )
    assert app_server_cfg.output.policy == "final_only"
