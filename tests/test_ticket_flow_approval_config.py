from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.config import DEFAULT_REPO_CONFIG, _parse_app_server_config
from codex_autorunner.core.ports.run_event import Completed, Started
from codex_autorunner.integrations.agents.agent_pool_impl import DefaultAgentPool
from codex_autorunner.tickets.agent_pool import AgentTurnRequest


class _FakeOrchestrator:
    def __init__(self):
        self.calls = []

    async def run_turn(self, agent_id, state, prompt, **kwargs):
        self.calls.append(
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "approval_policy": state.autorunner_approval_policy,
                "sandbox_mode": state.autorunner_sandbox_mode,
                **kwargs,
            }
        )
        yield Started(timestamp="now", session_id="thread-1")
        yield Completed(timestamp="now", final_message="ok")

    def get_context(self):
        return None

    def get_last_turn_id(self):
        return "turn-1"

    async def close_all(self):
        return None


@pytest.mark.asyncio
async def test_agent_pool_respects_ticket_flow_approval_defaults(tmp_path: Path):
    app_server_cfg = _parse_app_server_config(
        None, tmp_path, DEFAULT_REPO_CONFIG["app_server"]
    )
    cfg = SimpleNamespace(
        root=tmp_path,
        app_server=app_server_cfg,
        opencode=SimpleNamespace(session_stall_timeout_seconds=None),
        ticket_flow={"approval_mode": "safe", "default_approval_decision": "cancel"},
    )
    pool = DefaultAgentPool(cfg)  # type: ignore[arg-type]
    fake = _FakeOrchestrator()
    pool._backend_orchestrator = fake  # type: ignore[assignment]

    result = await pool.run_turn(
        AgentTurnRequest(agent_id="codex", prompt="hi", workspace_root=tmp_path)
    )

    assert result.text == "ok"
    assert fake.calls[0]["approval_policy"] == "on-request"
    assert fake.calls[0]["sandbox_mode"] == "workspaceWrite"


@pytest.mark.asyncio
async def test_agent_pool_uses_yolo_policy_for_ticket_flow(tmp_path: Path):
    app_server_cfg = _parse_app_server_config(
        None, tmp_path, DEFAULT_REPO_CONFIG["app_server"]
    )
    cfg = SimpleNamespace(
        root=tmp_path,
        app_server=app_server_cfg,
        opencode=SimpleNamespace(session_stall_timeout_seconds=None),
        ticket_flow={"approval_mode": "yolo", "default_approval_decision": "accept"},
    )
    pool = DefaultAgentPool(cfg)  # type: ignore[arg-type]
    fake = _FakeOrchestrator()
    pool._backend_orchestrator = fake  # type: ignore[assignment]

    await pool.run_turn(
        AgentTurnRequest(agent_id="codex", prompt="hi", workspace_root=tmp_path)
    )

    assert fake.calls[0]["approval_policy"] == "never"
    assert fake.calls[0]["sandbox_mode"] == "dangerFullAccess"


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
