from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.agents.opencode.constants import DEFAULT_TICKET_MODEL
from codex_autorunner.agents.opencode.runtime import OpenCodeTurnOutput, split_model_id
from codex_autorunner.tickets.agent_pool import AgentPool, AgentTurnRequest


class _StubOpencodeClient:
    def __init__(self, session_id: str = "session-1") -> None:
        self.session_id = session_id
        self.prompt_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []

    async def create_session(self, *, title=None, directory=None):
        self.create_calls.append({"title": title, "directory": directory})
        return {"id": self.session_id}

    async def prompt_async(self, session_id, *, message, model=None, variant=None):
        self.prompt_calls.append(
            {
                "session_id": session_id,
                "message": message,
                "model": model,
                "variant": variant,
            }
        )
        return {"id": "turn-1"}


class _StubSupervisor:
    def __init__(self, client: _StubOpencodeClient) -> None:
        self.client = client

    async def get_client(self, workspace_root: Path):
        return self.client


@pytest.mark.asyncio
async def test_opencode_turn_respects_model_override(monkeypatch, tmp_path: Path):
    client = _StubOpencodeClient()
    supervisor = _StubSupervisor(client)
    calls: dict[str, object] = {}

    async def _fake_collect(
        _client, *, session_id, workspace_path, model_payload=None, **kwargs
    ):
        calls["collect"] = {
            "session_id": session_id,
            "workspace_path": workspace_path,
            "model_payload": model_payload,
        }
        return OpenCodeTurnOutput(text="ok")

    monkeypatch.setattr(
        "codex_autorunner.tickets.agent_pool.collect_opencode_output", _fake_collect
    )

    cfg = SimpleNamespace(
        app_server=None, opencode=SimpleNamespace(session_stall_timeout_seconds=None)
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]
    pool._opencode_supervisor = supervisor

    result = await pool._run_opencode_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="hello",
            workspace_root=tmp_path,
            options={"model": "provider/model-a", "reasoning": "fast"},
        )
    )

    expected_model = split_model_id("provider/model-a")
    assert client.prompt_calls[0]["model"] == expected_model
    assert client.prompt_calls[0]["variant"] == "fast"
    assert calls["collect"]["model_payload"] == expected_model
    assert result.text == "ok"


@pytest.mark.asyncio
async def test_opencode_turn_falls_back_to_default_model(monkeypatch, tmp_path: Path):
    client = _StubOpencodeClient()
    supervisor = _StubSupervisor(client)
    calls: dict[str, object] = {}

    async def _fake_collect(
        _client, *, session_id, workspace_path, model_payload=None, **kwargs
    ):
        calls["collect"] = {
            "session_id": session_id,
            "workspace_path": workspace_path,
            "model_payload": model_payload,
        }
        return OpenCodeTurnOutput(text="ok")

    monkeypatch.setattr(
        "codex_autorunner.tickets.agent_pool.collect_opencode_output", _fake_collect
    )

    cfg = SimpleNamespace(
        app_server=None, opencode=SimpleNamespace(session_stall_timeout_seconds=None)
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]
    pool._opencode_supervisor = supervisor

    await pool._run_opencode_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="hello",
            workspace_root=tmp_path,
            options=None,
        )
    )

    expected_default = split_model_id(DEFAULT_TICKET_MODEL)
    assert client.prompt_calls[0]["model"] == expected_default
    assert calls["collect"]["model_payload"] == expected_default
