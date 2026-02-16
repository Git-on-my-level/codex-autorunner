from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator

import pytest

from codex_autorunner.core.config import TicketFlowConfig
from codex_autorunner.core.ports.agent_backend import AgentBackend
from codex_autorunner.core.ports.run_event import Completed, RunEvent, now_iso
from codex_autorunner.core.state import RunnerState
from codex_autorunner.integrations.agents.backend_orchestrator import (
    BackendOrchestrator,
)


class _RecordingBackend(AgentBackend):
    def __init__(self) -> None:
        self.configure_calls: list[dict[str, Any]] = []

    def configure(self, **options: Any) -> None:
        self.configure_calls.append(dict(options))

    async def start_session(self, target: dict, context: dict) -> str:
        _ = target
        resumed = context.get("session_id")
        if isinstance(resumed, str) and resumed:
            return resumed
        return "session-123"

    async def run_turn(
        self, session_id: str, message: str
    ) -> AsyncGenerator[Any, None]:
        _ = session_id, message
        if False:
            yield None

    async def stream_events(self, session_id: str) -> AsyncGenerator[Any, None]:
        _ = session_id
        if False:
            yield None

    async def run_turn_events(
        self, session_id: str, message: str
    ) -> AsyncGenerator[RunEvent, None]:
        _ = message
        yield Completed(timestamp=now_iso(), final_message=f"ok:{session_id}")

    async def interrupt(self, session_id: str) -> None:
        _ = session_id

    async def final_messages(self, session_id: str) -> list[str]:
        _ = session_id
        return []

    async def request_approval(
        self, description: str, context: dict[str, Any] | None = None
    ) -> bool:
        _ = description, context
        return True


@pytest.mark.asyncio
async def test_backend_orchestrator_uses_generic_backend_configure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = _RecordingBackend()

    def _fake_factory(*_args: Any, **_kwargs: Any):
        def _build_backend(
            agent_id: str, state: RunnerState, notification_handler: Any
        ) -> AgentBackend:
            _ = agent_id, state, notification_handler
            return backend

        return _build_backend

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.build_agent_backend_factory",
        _fake_factory,
    )

    config = SimpleNamespace(
        autorunner_reuse_session=False,
        ticket_flow=TicketFlowConfig(
            approval_mode="yolo",
            default_approval_decision="cancel",
            include_previous_ticket_context=False,
        ),
    )
    orchestrator = BackendOrchestrator(repo_root=tmp_path, config=config)
    state = RunnerState(None, "idle", None, None, None)

    events = [
        event
        async for event in orchestrator.run_turn(
            "fake-agent",
            state,
            "hello",
            model="gpt-test",
            reasoning="high",
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], Completed)

    assert len(backend.configure_calls) == 1
    configure_call = backend.configure_calls[0]
    assert configure_call["approval_policy"] is None
    assert configure_call["approval_policy_default"] == "never"
    assert configure_call["sandbox_policy"] is None
    assert configure_call["sandbox_policy_default"] == "dangerFullAccess"
    assert configure_call["model"] == "gpt-test"
    assert configure_call["reasoning"] == "high"
    assert configure_call["reasoning_effort"] == "high"
    assert configure_call["default_approval_decision"] == "cancel"


def test_backend_orchestrator_run_turn_has_no_backend_isinstance_checks() -> None:
    source = inspect.getsource(BackendOrchestrator.run_turn)
    assert "isinstance(backend" not in source
