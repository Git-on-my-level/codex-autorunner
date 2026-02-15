from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.flows.models import FlowEventType
from codex_autorunner.core.ports.run_event import (
    Completed,
    Failed,
    OutputDelta,
    Started,
    TokenUsage,
)
from codex_autorunner.integrations.agents.agent_pool_impl import DefaultAgentPool
from codex_autorunner.tickets.agent_pool import AgentTurnRequest


class _FakeOrchestrator:
    def __init__(self, events):
        self.events = events
        self.calls: list[dict[str, object]] = []
        self.closed = False
        self.last_turn_id = "turn-fallback"

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
        for event in self.events:
            yield event

    def get_context(self):
        return SimpleNamespace(session_id="session-ctx")

    def get_last_turn_id(self):
        return self.last_turn_id

    async def close_all(self):
        self.closed = True


@pytest.mark.asyncio
async def test_run_turn_maps_events_to_result_and_emits(tmp_path: Path):
    events = [
        Started(timestamp="now", session_id="session-1", turn_id="turn-1"),
        OutputDelta(timestamp="now", content="hello", delta_type="assistant_stream"),
        OutputDelta(timestamp="now", content="log line", delta_type="log_line"),
        TokenUsage(timestamp="now", usage={"input": 3, "output": 5}),
        Completed(timestamp="now", final_message=""),
    ]
    cfg = SimpleNamespace(root=tmp_path, ticket_flow={"approval_mode": "yolo"})
    pool = DefaultAgentPool(cfg)  # type: ignore[arg-type]
    fake = _FakeOrchestrator(events)
    pool._backend_orchestrator = fake  # type: ignore[assignment]

    emitted = []

    def _emit(event_type: FlowEventType, payload: dict):
        emitted.append((event_type, payload))

    result = await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main prompt",
            workspace_root=tmp_path,
            emit_event=_emit,
        )
    )

    assert result.text == "hello"
    assert result.error is None
    assert result.conversation_id == "session-ctx"
    assert result.turn_id == "turn-1"
    assert result.raw == {
        "final_status": "completed",
        "log_lines": ["log line"],
        "token_usage": {"input": 3, "output": 5},
    }

    assert [event_type for event_type, _ in emitted] == [
        FlowEventType.AGENT_STREAM_DELTA,
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.TOKEN_USAGE,
    ]


@pytest.mark.asyncio
async def test_run_turn_handles_failure_and_fallback_turn_id(tmp_path: Path):
    events = [
        Started(timestamp="now", session_id="session-1", turn_id=None),
        Failed(timestamp="now", error_message="boom"),
    ]
    cfg = SimpleNamespace(root=tmp_path, ticket_flow={"approval_mode": "safe"})
    pool = DefaultAgentPool(cfg)  # type: ignore[arg-type]
    fake = _FakeOrchestrator(events)
    pool._backend_orchestrator = fake  # type: ignore[assignment]

    result = await pool.run_turn(
        AgentTurnRequest(
            agent_id="codex",
            prompt="main",
            workspace_root=tmp_path,
            conversation_id="session-in",
        )
    )

    assert result.error == "boom"
    assert result.turn_id == "turn-fallback"
    assert result.raw == {
        "final_status": "failed",
        "log_lines": [],
        "token_usage": None,
    }


@pytest.mark.asyncio
async def test_run_turn_passes_model_reasoning_session_and_merges_messages(
    tmp_path: Path,
):
    events = [
        Started(timestamp="now", session_id="session-1", turn_id="turn-1"),
        Completed(timestamp="now", final_message="done"),
    ]
    cfg = SimpleNamespace(root=tmp_path, ticket_flow={"approval_mode": "safe"})
    pool = DefaultAgentPool(cfg)  # type: ignore[arg-type]
    fake = _FakeOrchestrator(events)
    pool._backend_orchestrator = fake  # type: ignore[assignment]

    await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main",
            workspace_root=tmp_path,
            conversation_id="session-42",
            options={
                "model": {"providerID": "provider", "modelID": "model-x"},
                "reasoning": "high",
            },
            additional_messages=[
                {"text": "more"},
                {"text": "  "},
                {"text": "end"},
            ],
        )
    )

    call = fake.calls[0]
    assert call["session_id"] == "session-42"
    assert call["model"] == "provider/model-x"
    assert call["reasoning"] == "high"
    assert call["prompt"] == "main\n\nmore\n\nend"
    assert call["approval_policy"] == "on-request"
    assert call["sandbox_mode"] == "workspaceWrite"


@pytest.mark.asyncio
async def test_close_all_delegates_to_orchestrator(tmp_path: Path):
    cfg = SimpleNamespace(root=tmp_path, ticket_flow={"approval_mode": "yolo"})
    pool = DefaultAgentPool(cfg)  # type: ignore[arg-type]
    fake = _FakeOrchestrator([])
    pool._backend_orchestrator = fake  # type: ignore[assignment]

    await pool.close_all()

    assert fake.closed is True
