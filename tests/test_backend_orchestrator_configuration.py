from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Optional

import pytest

from codex_autorunner.core.config import TicketFlowConfig
from codex_autorunner.core.ports.agent_backend import AgentBackend
from codex_autorunner.core.ports.run_event import Completed, RunEvent, Started, now_iso
from codex_autorunner.core.state import RunnerState
from codex_autorunner.integrations.agents.backend_orchestrator import (
    BackendContext,
    BackendOrchestrator,
)


class _RecordingBackend(AgentBackend):
    def __init__(self) -> None:
        self.configure_calls: list[dict[str, Any]] = []
        self.turn_input_items: Optional[list[dict[str, Any]]] = None
        self.reported_session_id: Optional[str] = None

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
        self,
        session_id: str,
        message: str,
        *,
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncGenerator[RunEvent, None]:
        _ = message
        self.turn_input_items = input_items
        if self.reported_session_id:
            yield Started(timestamp=now_iso(), session_id=self.reported_session_id)
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
        app_server=SimpleNamespace(turn_timeout_seconds=321.0),
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
    assert configure_call["reuse_session"] is False
    assert configure_call["model"] == "gpt-test"
    assert configure_call["reasoning"] == "high"
    assert configure_call["reasoning_effort"] == "high"
    assert configure_call["turn_timeout_seconds"] == 321.0
    assert configure_call["default_approval_decision"] == "cancel"


@pytest.mark.asyncio
async def test_backend_orchestrator_forwards_input_items_when_present(
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
        app_server=SimpleNamespace(turn_timeout_seconds=321.0),
        ticket_flow=TicketFlowConfig(
            approval_mode="yolo",
            default_approval_decision="cancel",
            include_previous_ticket_context=False,
        ),
    )
    orchestrator = BackendOrchestrator(repo_root=tmp_path, config=config)
    state = RunnerState(None, "idle", None, None, None)
    input_items = [
        {"type": "text", "text": "hello"},
        {"type": "localImage", "path": str(tmp_path / "screen.png")},
    ]

    _ = [
        event
        async for event in orchestrator.run_turn(
            "fake-agent",
            state,
            "hello",
            input_items=input_items,
        )
    ]

    assert backend.turn_input_items == input_items


@pytest.mark.asyncio
async def test_backend_orchestrator_reconciles_session_id_without_input_items(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = _RecordingBackend()
    backend.reported_session_id = "session-canonical"

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
        app_server=SimpleNamespace(turn_timeout_seconds=321.0),
        ticket_flow=TicketFlowConfig(
            approval_mode="yolo",
            default_approval_decision="cancel",
            include_previous_ticket_context=False,
        ),
    )
    orchestrator = BackendOrchestrator(repo_root=tmp_path, config=config)
    state = RunnerState(None, "idle", None, None, None)

    _ = [event async for event in orchestrator.run_turn("fake-agent", state, "hello")]

    context = orchestrator.get_context()
    assert context is not None
    assert context.session_id == "session-canonical"


def test_backend_orchestrator_run_turn_has_no_backend_isinstance_checks() -> None:
    source = inspect.getsource(BackendOrchestrator.run_turn)
    assert "isinstance(backend" not in source


@pytest.mark.asyncio
async def test_reset_thread_id_clears_runtime_state_for_codex_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fake_factory(*_args: Any, **_kwargs: Any):
        def _build_backend(
            agent_id: str, state: RunnerState, notification_handler: Any
        ) -> AgentBackend:
            _ = agent_id, state, notification_handler
            raise AssertionError("backend should not be created in this test")

        return _build_backend

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.build_agent_backend_factory",
        _fake_factory,
    )

    config = SimpleNamespace(
        autorunner_reuse_session=False,
        app_server=SimpleNamespace(turn_timeout_seconds=321.0),
        ticket_flow=TicketFlowConfig(
            approval_mode="yolo",
            default_approval_decision="accept",
            include_previous_ticket_context=False,
        ),
    )
    orchestrator = BackendOrchestrator(repo_root=tmp_path, config=config)
    orchestrator._context = BackendContext(
        agent_id="codex",
        session_id="thread-old",
        turn_id="turn-old",
        thread_info={"id": "thread-old"},
    )

    reset_calls: list[Optional[str]] = []

    class _Factory:
        def reset_session_state(self, *, agent_id: Optional[str] = None) -> None:
            reset_calls.append(agent_id)

    monkeypatch.setattr(orchestrator, "_agent_backend_factory", lambda: _Factory())

    cleared = orchestrator.reset_thread_id("file_chat.discord.channel.123456789abc")
    assert cleared is False
    assert reset_calls == ["codex"]
    assert orchestrator._context is not None
    assert orchestrator._context.session_id is None
    assert orchestrator._context.turn_id is None
    assert orchestrator._context.thread_info is None


@pytest.mark.asyncio
async def test_reset_thread_id_targets_opencode_runtime_for_opencode_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fake_factory(*_args: Any, **_kwargs: Any):
        def _build_backend(
            agent_id: str, state: RunnerState, notification_handler: Any
        ) -> AgentBackend:
            _ = agent_id, state, notification_handler
            raise AssertionError("backend should not be created in this test")

        return _build_backend

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.wiring.build_agent_backend_factory",
        _fake_factory,
    )

    config = SimpleNamespace(
        autorunner_reuse_session=False,
        app_server=SimpleNamespace(turn_timeout_seconds=321.0),
        ticket_flow=TicketFlowConfig(
            approval_mode="yolo",
            default_approval_decision="accept",
            include_previous_ticket_context=False,
        ),
    )
    orchestrator = BackendOrchestrator(repo_root=tmp_path, config=config)
    orchestrator._context = BackendContext(
        agent_id="codex",
        session_id="thread-old",
        turn_id="turn-old",
        thread_info={"id": "thread-old"},
    )

    reset_calls: list[Optional[str]] = []

    class _Factory:
        def reset_session_state(self, *, agent_id: Optional[str] = None) -> None:
            reset_calls.append(agent_id)

    monkeypatch.setattr(orchestrator, "_agent_backend_factory", lambda: _Factory())

    cleared = orchestrator.reset_thread_id(
        "file_chat.opencode.discord.channel.123456789abc"
    )
    assert cleared is False
    assert reset_calls == ["opencode"]
    assert orchestrator._context is not None
    # codex context should remain intact when resetting opencode state.
    assert orchestrator._context.session_id == "thread-old"
    assert orchestrator._context.turn_id == "turn-old"
    assert orchestrator._context.thread_info == {"id": "thread-old"}
