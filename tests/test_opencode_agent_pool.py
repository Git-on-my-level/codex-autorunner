import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.config import TicketFlowConfig
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


class _BlockingFakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.closed = False
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def run_turn(self, agent_id, state, prompt, **kwargs):
        call_index = len(self.calls)
        self.calls.append(
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "approval_policy": state.autorunner_approval_policy,
                "sandbox_mode": state.autorunner_sandbox_mode,
                **kwargs,
            }
        )
        session_id = (
            kwargs.get("session_id")
            if isinstance(kwargs.get("session_id"), str) and kwargs.get("session_id")
            else f"session-{call_index + 1}"
        )
        yield Started(
            timestamp="now",
            session_id=session_id,
            turn_id=f"turn-{call_index + 1}",
        )
        if call_index == 0:
            self.first_started.set()
            await self.release_first.wait()
        yield Completed(
            timestamp="now",
            final_message=f"done-{call_index + 1}",
        )

    def get_context(self):
        return None

    def get_last_turn_id(self):
        return "turn-fallback"

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
    cfg = SimpleNamespace(
        root=tmp_path,
        ticket_flow=TicketFlowConfig(
            approval_mode="yolo",
            default_approval_decision="accept",
            include_previous_ticket_context=False,
        ),
    )
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
    assert result.conversation_id
    assert result.turn_id == "turn-1"
    assert result.raw["final_status"] == "completed"
    assert result.raw["log_lines"] == ["log line"]
    assert result.raw["token_usage"] == {"input": 3, "output": 5}
    assert isinstance(result.raw["execution_id"], str)
    assert result.raw["backend_thread_id"] == "session-ctx"

    assert [event_type for event_type, _ in emitted] == [
        FlowEventType.AGENT_STREAM_DELTA,
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.TOKEN_USAGE,
    ]
    first_output_event = emitted[1][1]["message"]
    second_output_event = emitted[2][1]["message"]
    assert first_output_event["method"] == "outputDelta"
    assert first_output_event["params"]["deltaType"] == "assistant_stream"
    assert second_output_event["method"] == "outputDelta"
    assert second_output_event["params"]["deltaType"] == "log_line"


@pytest.mark.asyncio
async def test_run_turn_handles_failure_and_fallback_turn_id(tmp_path: Path):
    events = [
        Started(timestamp="now", session_id="session-1", turn_id=None),
        Failed(timestamp="now", error_message="boom"),
    ]
    cfg = SimpleNamespace(
        root=tmp_path,
        ticket_flow=TicketFlowConfig(
            approval_mode="review",
            default_approval_decision="accept",
            include_previous_ticket_context=False,
        ),
    )
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
    assert result.raw["final_status"] == "failed"
    assert result.raw["log_lines"] == []
    assert result.raw["token_usage"] is None
    assert isinstance(result.raw["execution_id"], str)
    assert result.raw["backend_thread_id"] == "session-ctx"


@pytest.mark.asyncio
async def test_run_turn_passes_model_reasoning_session_and_merges_messages(
    tmp_path: Path,
):
    events = [
        Started(timestamp="now", session_id="session-1", turn_id="turn-1"),
        Completed(timestamp="now", final_message="done"),
    ]
    cfg = SimpleNamespace(
        root=tmp_path,
        ticket_flow=TicketFlowConfig(
            approval_mode="review",
            default_approval_decision="accept",
            include_previous_ticket_context=False,
        ),
    )
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
async def test_run_turn_queues_busy_delegated_thread_and_reuses_orchestration_thread_id(
    tmp_path: Path,
):
    cfg = SimpleNamespace(
        root=tmp_path,
        ticket_flow=TicketFlowConfig(
            approval_mode="yolo",
            default_approval_decision="accept",
            include_previous_ticket_context=False,
        ),
    )
    pool = DefaultAgentPool(cfg)  # type: ignore[arg-type]
    fake = _BlockingFakeOrchestrator()
    pool._backend_orchestrator = fake  # type: ignore[assignment]

    first_task = asyncio.create_task(
        pool.run_turn(
            AgentTurnRequest(
                agent_id="codex",
                prompt="first",
                workspace_root=tmp_path,
            )
        )
    )
    await fake.first_started.wait()

    thread = pool._thread_store.list_threads(agent="codex", limit=1)[0]
    thread_id = str(thread["managed_thread_id"])

    second_task = asyncio.create_task(
        pool.run_turn(
            AgentTurnRequest(
                agent_id="codex",
                prompt="second",
                workspace_root=tmp_path,
                conversation_id=thread_id,
            )
        )
    )
    await asyncio.sleep(0)

    queued = pool._thread_store.list_queued_turns(thread_id)
    assert len(queued) == 1
    assert len(fake.calls) == 1

    fake.release_first.set()
    first_result = await first_task
    second_result = await second_task

    assert first_result.conversation_id == thread_id
    assert second_result.conversation_id == thread_id
    assert first_result.text == "done-1"
    assert second_result.text == "done-2"
    assert len(fake.calls) == 2
    assert fake.calls[1]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_close_all_delegates_to_orchestrator(tmp_path: Path):
    cfg = SimpleNamespace(
        root=tmp_path,
        ticket_flow=TicketFlowConfig(
            approval_mode="yolo",
            default_approval_decision="accept",
            include_previous_ticket_context=False,
        ),
    )
    pool = DefaultAgentPool(cfg)  # type: ignore[arg-type]
    fake = _FakeOrchestrator([])
    pool._backend_orchestrator = fake  # type: ignore[assignment]

    await pool.close_all()

    assert fake.closed is True
