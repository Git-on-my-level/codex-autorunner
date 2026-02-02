from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

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


class _StubCodexClient:
    def __init__(self) -> None:
        self.thread_start_calls: list[dict[str, object]] = []
        self.thread_resume_calls: list[str] = []
        self.turn_start_calls: list[dict[str, object]] = []

    async def thread_start(self, *, cwd=None, approvalPolicy=None, sandbox=None):
        self.thread_start_calls.append(
            {
                "cwd": cwd,
                "approval_policy": approvalPolicy,
                "sandbox": sandbox,
            }
        )
        return {"id": "thread-1"}

    async def thread_resume(self, thread_id: str):
        self.thread_resume_calls.append(thread_id)

    async def turn_start(
        self,
        thread_id: str,
        text: str,
        *,
        input_items: Optional[list[dict[str, Any]]] = None,
        **kwargs,
    ):
        self.turn_start_calls.append(
            {
                "thread_id": thread_id,
                "text": text,
                "input_items": input_items,
                "kwargs": kwargs,
            }
        )

        class _TurnHandle:
            def __init__(self):
                self.turn_id = "turn-1"

            async def wait(self):
                class _Result:
                    turn_id = "turn-1"
                    status = "completed"
                    agent_messages = ["ok"]
                    errors = []

                return _Result()

        return _TurnHandle()


class _StubSupervisor:
    def __init__(self, client) -> None:
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


@pytest.mark.asyncio
async def test_opencode_turn_with_additional_messages(monkeypatch, tmp_path: Path):
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
        return OpenCodeTurnOutput(text="combined response")

    monkeypatch.setattr(
        "codex_autorunner.tickets.agent_pool.collect_opencode_output", _fake_collect
    )

    cfg = SimpleNamespace(
        app_server=None, opencode=SimpleNamespace(session_stall_timeout_seconds=None)
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]
    pool._opencode_supervisor = supervisor

    additional_messages = [
        {"text": "additional message 1"},
        {"text": "additional message 2"},
    ]

    result = await pool._run_opencode_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main prompt",
            workspace_root=tmp_path,
            additional_messages=additional_messages,
        )
    )

    assert len(client.prompt_calls) == 3
    assert client.prompt_calls[0]["message"] == "main prompt"
    assert client.prompt_calls[1]["message"] == "additional message 1"
    assert client.prompt_calls[2]["message"] == "additional message 2"
    assert result.text == "combined response"


@pytest.mark.asyncio
async def test_opencode_turn_filters_empty_additional_messages(
    monkeypatch, tmp_path: Path
):
    client = _StubOpencodeClient()
    supervisor = _StubSupervisor(client)

    async def _fake_collect(
        _client, *, session_id, workspace_path, model_payload=None, **kwargs
    ):
        return OpenCodeTurnOutput(text="ok")

    monkeypatch.setattr(
        "codex_autorunner.tickets.agent_pool.collect_opencode_output", _fake_collect
    )

    cfg = SimpleNamespace(
        app_server=None, opencode=SimpleNamespace(session_stall_timeout_seconds=None)
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]
    pool._opencode_supervisor = supervisor

    additional_messages = [
        {"text": "valid message"},
        {"text": ""},
        {"text": "   "},
        {"text": "another valid message"},
    ]

    await pool._run_opencode_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main",
            workspace_root=tmp_path,
            additional_messages=additional_messages,
        )
    )

    assert len(client.prompt_calls) == 3
    assert client.prompt_calls[0]["message"] == "main"
    assert client.prompt_calls[1]["message"] == "valid message"
    assert client.prompt_calls[2]["message"] == "another valid message"


@pytest.mark.asyncio
async def test_opencode_turn_handles_non_dict_messages(monkeypatch, tmp_path: Path):
    client = _StubOpencodeClient()
    supervisor = _StubSupervisor(client)

    async def _fake_collect(
        _client, *, session_id, workspace_path, model_payload=None, **kwargs
    ):
        return OpenCodeTurnOutput(text="ok")

    monkeypatch.setattr(
        "codex_autorunner.tickets.agent_pool.collect_opencode_output", _fake_collect
    )

    cfg = SimpleNamespace(
        app_server=None, opencode=SimpleNamespace(session_stall_timeout_seconds=None)
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]
    pool._opencode_supervisor = supervisor

    additional_messages = [
        {"text": "valid message"},
        "invalid string",
        {"text": "another valid"},
        None,
    ]

    await pool._run_opencode_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main",
            workspace_root=tmp_path,
            additional_messages=additional_messages,
        )
    )

    assert len(client.prompt_calls) == 3
    assert client.prompt_calls[0]["message"] == "main"
    assert client.prompt_calls[1]["message"] == "valid message"
    assert client.prompt_calls[2]["message"] == "another valid"


@pytest.mark.asyncio
async def test_opencode_turn_applies_model_to_additional_messages(
    monkeypatch, tmp_path: Path
):
    client = _StubOpencodeClient()
    supervisor = _StubSupervisor(client)

    async def _fake_collect(
        _client, *, session_id, workspace_path, model_payload=None, **kwargs
    ):
        return OpenCodeTurnOutput(text="ok")

    monkeypatch.setattr(
        "codex_autorunner.tickets.agent_pool.collect_opencode_output", _fake_collect
    )

    cfg = SimpleNamespace(
        app_server=None, opencode=SimpleNamespace(session_stall_timeout_seconds=None)
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]
    pool._opencode_supervisor = supervisor

    additional_messages = [{"text": "msg1"}, {"text": "msg2"}]

    await pool._run_opencode_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main",
            workspace_root=tmp_path,
            options={"model": "provider/custom-model", "reasoning": "detailed"},
            additional_messages=additional_messages,
        )
    )

    expected_model = split_model_id("provider/custom-model")
    assert len(client.prompt_calls) == 3
    for call in client.prompt_calls:
        assert call["model"] == expected_model
        assert call["variant"] == "detailed"


@pytest.mark.asyncio
async def test_codex_turn_with_additional_messages(monkeypatch, tmp_path: Path):
    client = _StubCodexClient()
    supervisor = _StubSupervisor(client)

    cfg = SimpleNamespace(
        app_server=SimpleNamespace(
            command=["test"],
            state_root=tmp_path,
            auto_restart=False,
            max_handles=1,
            idle_ttl_seconds=300,
            request_timeout=30,
            turn_stall_timeout_seconds=60,
            turn_stall_poll_interval_seconds=2,
            turn_stall_recovery_min_interval_seconds=10,
            client=SimpleNamespace(
                max_message_bytes=50 * 1024 * 1024,
                oversize_preview_bytes=4096,
                max_oversize_drain_bytes=100 * 1024 * 1024,
                restart_backoff_initial_seconds=0.5,
                restart_backoff_max_seconds=30,
                restart_backoff_jitter_ratio=0.1,
            ),
        ),
        opencode=SimpleNamespace(session_stall_timeout_seconds=None),
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]
    pool._app_server_supervisor = supervisor

    additional_messages = [
        {"text": "additional message 1"},
        {"text": "additional message 2"},
    ]

    result = await pool._run_codex_turn(
        AgentTurnRequest(
            agent_id="codex",
            prompt="main prompt",
            workspace_root=tmp_path,
            additional_messages=additional_messages,
        )
    )

    assert len(client.turn_start_calls) == 1
    assert client.turn_start_calls[0]["text"] == "main prompt"
    assert client.turn_start_calls[0]["input_items"] == [
        {"type": "text", "text": "main prompt"},
        {"type": "text", "text": "additional message 1"},
        {"type": "text", "text": "additional message 2"},
    ]
    assert result.text == "ok"


@pytest.mark.asyncio
async def test_codex_turn_filters_empty_additional_messages(
    monkeypatch, tmp_path: Path
):
    client = _StubCodexClient()
    supervisor = _StubSupervisor(client)

    cfg = SimpleNamespace(
        app_server=SimpleNamespace(
            command=["test"],
            state_root=tmp_path,
            auto_restart=False,
            max_handles=1,
            idle_ttl_seconds=300,
            request_timeout=30,
            turn_stall_timeout_seconds=60,
            turn_stall_poll_interval_seconds=2,
            turn_stall_recovery_min_interval_seconds=10,
            client=SimpleNamespace(
                max_message_bytes=50 * 1024 * 1024,
                oversize_preview_bytes=4096,
                max_oversize_drain_bytes=100 * 1024 * 1024,
                restart_backoff_initial_seconds=0.5,
                restart_backoff_max_seconds=30,
                restart_backoff_jitter_ratio=0.1,
            ),
        ),
        opencode=SimpleNamespace(session_stall_timeout_seconds=None),
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]
    pool._app_server_supervisor = supervisor

    additional_messages = [
        {"text": "valid message"},
        {"text": ""},
        {"text": "   "},
        {"text": "another valid message"},
    ]

    await pool._run_codex_turn(
        AgentTurnRequest(
            agent_id="codex",
            prompt="main",
            workspace_root=tmp_path,
            additional_messages=additional_messages,
        )
    )

    assert len(client.turn_start_calls) == 1
    assert client.turn_start_calls[0]["input_items"] == [
        {"type": "text", "text": "main"},
        {"type": "text", "text": "valid message"},
        {"type": "text", "text": "another valid message"},
    ]


@pytest.mark.asyncio
async def test_codex_turn_handles_non_dict_messages(monkeypatch, tmp_path: Path):
    client = _StubCodexClient()
    supervisor = _StubSupervisor(client)

    cfg = SimpleNamespace(
        app_server=SimpleNamespace(
            command=["test"],
            state_root=tmp_path,
            auto_restart=False,
            max_handles=1,
            idle_ttl_seconds=300,
            request_timeout=30,
            turn_stall_timeout_seconds=60,
            turn_stall_poll_interval_seconds=2,
            turn_stall_recovery_min_interval_seconds=10,
            client=SimpleNamespace(
                max_message_bytes=50 * 1024 * 1024,
                oversize_preview_bytes=4096,
                max_oversize_drain_bytes=100 * 1024 * 1024,
                restart_backoff_initial_seconds=0.5,
                restart_backoff_max_seconds=30,
                restart_backoff_jitter_ratio=0.1,
            ),
        ),
        opencode=SimpleNamespace(session_stall_timeout_seconds=None),
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]
    pool._app_server_supervisor = supervisor

    additional_messages = [
        {"text": "valid message"},
        "invalid string",
        {"text": "another valid"},
        None,
    ]

    await pool._run_codex_turn(
        AgentTurnRequest(
            agent_id="codex",
            prompt="main",
            workspace_root=tmp_path,
            additional_messages=additional_messages,
        )
    )

    assert len(client.turn_start_calls) == 1
    assert client.turn_start_calls[0]["input_items"] == [
        {"type": "text", "text": "main"},
        {"type": "text", "text": "valid message"},
        {"type": "text", "text": "another valid"},
    ]


@pytest.mark.asyncio
async def test_codex_turn_without_additional_messages(monkeypatch, tmp_path: Path):
    client = _StubCodexClient()
    supervisor = _StubSupervisor(client)

    cfg = SimpleNamespace(
        app_server=SimpleNamespace(
            command=["test"],
            state_root=tmp_path,
            auto_restart=False,
            max_handles=1,
            idle_ttl_seconds=300,
            request_timeout=30,
            turn_stall_timeout_seconds=60,
            turn_stall_poll_interval_seconds=2,
            turn_stall_recovery_min_interval_seconds=10,
            client=SimpleNamespace(
                max_message_bytes=50 * 1024 * 1024,
                oversize_preview_bytes=4096,
                max_oversize_drain_bytes=100 * 1024 * 1024,
                restart_backoff_initial_seconds=0.5,
                restart_backoff_max_seconds=30,
                restart_backoff_jitter_ratio=0.1,
            ),
        ),
        opencode=SimpleNamespace(session_stall_timeout_seconds=None),
    )
    pool = AgentPool(cfg)  # type: ignore[arg-type]
    pool._app_server_supervisor = supervisor

    await pool._run_codex_turn(
        AgentTurnRequest(
            agent_id="codex",
            prompt="main prompt",
            workspace_root=tmp_path,
            additional_messages=None,
        )
    )

    assert len(client.turn_start_calls) == 1
    assert client.turn_start_calls[0]["text"] == "main prompt"
    assert client.turn_start_calls[0]["input_items"] is None
