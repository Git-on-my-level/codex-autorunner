from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.config import DEFAULT_REPO_CONFIG, _parse_app_server_config
from codex_autorunner.integrations.app_server.client import TurnResult
from codex_autorunner.integrations.app_server.supervisor import (
    WorkspaceAppServerSupervisor,
)
from codex_autorunner.tickets.agent_pool import AgentPool, AgentTurnRequest


class _DummyTurnHandle:
    def __init__(self, client):
        self._client = client

    async def wait(self, *, timeout=None):
        result = TurnResult(
            turn_id="t-1",
            status="ok",
            agent_messages=["ok"],
            errors=[],
            raw_events=[],
        )
        result.error = None
        result.duration_seconds = 0.0
        result.token_usage = {}
        return result


class _DummyClient:
    def __init__(self):
        self.thread_start_calls = []
        self.turn_start_called = False

    async def thread_start(self, *, cwd, approvalPolicy, sandbox):
        self.thread_start_calls.append(
            {"cwd": cwd, "approvalPolicy": approvalPolicy, "sandbox": sandbox}
        )
        return {"id": "thread-1"}

    async def turn_start(self, thread_id, message):
        self.turn_start_called = True
        return _DummyTurnHandle(self)


class _CaptureSupervisor:
    def __init__(self, *_, default_approval_decision="accept", **__):
        self.default_approval_decision = default_approval_decision
        self.client = _DummyClient()

    async def get_client(self, workspace_root: Path):
        return self.client


@pytest.mark.asyncio
async def test_agent_pool_respects_ticket_flow_approval_defaults(
    monkeypatch, tmp_path: Path
):
    captured = {}

    def _capture_supervisor(*args, **kwargs):
        sup = _CaptureSupervisor(*args, **kwargs)
        captured["default_approval_decision"] = sup.default_approval_decision
        return sup

    monkeypatch.setattr(
        "codex_autorunner.tickets.agent_pool.WorkspaceAppServerSupervisor",
        _capture_supervisor,
    )

    app_server_cfg = _parse_app_server_config(
        None, tmp_path, DEFAULT_REPO_CONFIG["app_server"]
    )
    cfg = SimpleNamespace(
        app_server=app_server_cfg,
        opencode=SimpleNamespace(session_stall_timeout_seconds=None),
        ticket_flow={"approval_mode": "safe", "default_approval_decision": "cancel"},
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]

    result = await pool._run_codex_turn(
        AgentTurnRequest(agent_id="codex", prompt="hi", workspace_root=tmp_path)
    )

    assert result.text == "ok"
    assert captured["default_approval_decision"] == "cancel"
    client = pool._app_server_supervisor.client  # type: ignore[attr-defined]
    assert client.thread_start_calls[0]["approvalPolicy"] == "on-request"


@pytest.mark.asyncio
async def test_agent_pool_uses_yolo_policy_for_ticket_flow(monkeypatch, tmp_path: Path):
    supervisor = _CaptureSupervisor()

    async def _dummy_get_client(_self, workspace_root):
        return supervisor.client

    monkeypatch.setattr(
        WorkspaceAppServerSupervisor,
        "get_client",
        _dummy_get_client,
    )
    # Avoid creating new supervisor inside AgentPool.
    monkeypatch.setattr(
        "codex_autorunner.tickets.agent_pool.WorkspaceAppServerSupervisor",
        lambda *a, **k: supervisor,
    )

    app_server_cfg = _parse_app_server_config(
        None, tmp_path, DEFAULT_REPO_CONFIG["app_server"]
    )
    cfg = SimpleNamespace(
        app_server=app_server_cfg,
        opencode=SimpleNamespace(session_stall_timeout_seconds=None),
        ticket_flow={"approval_mode": "yolo", "default_approval_decision": "accept"},
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]

    await pool._run_codex_turn(
        AgentTurnRequest(agent_id="codex", prompt="hi", workspace_root=tmp_path)
    )

    assert supervisor.client.thread_start_calls[0]["approvalPolicy"] == "never"
