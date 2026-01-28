import asyncio
from typing import AsyncGenerator, Callable, Optional

import pytest

from codex_autorunner.core.engine import Engine
from codex_autorunner.core.ports.agent_backend import (
    AgentBackend,
    AgentEvent,
    now_iso,
)
from codex_autorunner.core.ports.run_event import (
    Completed,
    OutputDelta,
    RunEvent,
    Started,
)
from codex_autorunner.core.state import RunnerState


class FakeSupervisor:
    instances: list["FakeSupervisor"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        type(self).instances.append(self)

    async def close_all(self) -> None:
        return None


class FakeBackend(AgentBackend):
    def __init__(
        self,
        *,
        thread_id: str = "thread-1",
        turn_id: str = "turn-1",
        wait_event: Optional[asyncio.Event] = None,
    ) -> None:
        self._thread_id = thread_id
        self._turn_id = turn_id
        self._wait_event = wait_event
        self._interrupt_event: Optional[asyncio.Event] = None
        self.interrupt_calls: list[str] = []
        self.last_turn_id: Optional[str] = None
        self.last_thread_info: Optional[dict] = None

    async def start_session(self, target: dict, context: dict) -> str:
        session_id = context.get("session_id") or self._thread_id
        self._thread_id = session_id
        return session_id

    async def run_turn(
        self, session_id: str, message: str
    ) -> AsyncGenerator[AgentEvent, None]:
        if False:
            yield None  # pragma: no cover
        raise NotImplementedError

    async def run_turn_events(
        self, session_id: str, message: str
    ) -> AsyncGenerator[RunEvent, None]:
        self._interrupt_event = asyncio.Event()
        if session_id:
            self._thread_id = session_id
        self.last_turn_id = self._turn_id
        self.last_thread_info = {"id": self._thread_id}
        yield Started(timestamp=now_iso(), session_id=self._thread_id)
        yield OutputDelta(
            timestamp=now_iso(), content=message, delta_type="user_message"
        )
        if self._wait_event is not None:
            wait_task = asyncio.create_task(self._wait_event.wait())
            interrupt_task = asyncio.create_task(self._interrupt_event.wait())
            done, pending = await asyncio.wait(
                [wait_task, interrupt_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if self._interrupt_event.is_set():
                return
        yield Completed(timestamp=now_iso(), final_message="done")

    async def stream_events(self, session_id: str) -> AsyncGenerator[AgentEvent, None]:
        if False:
            yield None  # pragma: no cover
        raise NotImplementedError

    async def interrupt(self, session_id: str) -> None:
        self.interrupt_calls.append(session_id)
        if self._interrupt_event is not None:
            self._interrupt_event.set()
        if self._wait_event is not None:
            self._wait_event.set()

    async def final_messages(self, session_id: str) -> list[str]:
        return []

    async def request_approval(
        self, description: str, context: Optional[dict] = None
    ) -> bool:
        raise NotImplementedError


def _backend_factory_for(
    backend: FakeBackend,
) -> tuple[
    Callable[[str, RunnerState, Optional[Callable[[dict], object]]], AgentBackend],
    list[str],
]:
    calls: list[str] = []

    def factory(
        agent_id: str,
        _state: RunnerState,
        _notification_handler: Optional[Callable[[dict], object]],
    ) -> AgentBackend:
        calls.append(agent_id)
        return backend

    return factory, calls


def test_app_server_supervisor_reused(repo) -> None:
    engine = Engine(
        repo,
        app_server_supervisor_factory=lambda *_args, **_kwargs: FakeSupervisor(),
    )
    FakeSupervisor.instances = []
    first = engine._ensure_app_server_supervisor("autorunner")
    second = engine._ensure_app_server_supervisor("autorunner")

    assert first is second
    assert len(FakeSupervisor.instances) == 1


@pytest.mark.anyio
async def test_autorunner_reuses_supervisor_across_turns(repo) -> None:
    backend = FakeBackend()
    backend_factory, calls = _backend_factory_for(backend)
    engine = Engine(repo, backend_factory=backend_factory)
    engine.config.app_server.command = ["codex", "app-server"]

    exit_code = await engine._run_codex_app_server_async("prompt", 1)
    assert exit_code == 0

    exit_code = await engine._run_codex_app_server_async("prompt", 2)
    assert exit_code == 0

    assert calls == ["codex", "codex"]


@pytest.mark.anyio
async def test_autorunner_turn_timeout_uses_config(repo) -> None:
    backend = FakeBackend()
    backend_factory, _calls = _backend_factory_for(backend)
    engine = Engine(repo, backend_factory=backend_factory)
    engine.config.app_server.turn_timeout_seconds = 42
    engine.config.app_server.command = ["codex", "app-server"]

    exit_code = await engine._run_codex_app_server_async("prompt", 1)

    assert exit_code == 0
    assert not backend.interrupt_calls


@pytest.mark.anyio
async def test_autorunner_stop_interrupts_turn(repo) -> None:
    event = asyncio.Event()
    backend = FakeBackend(wait_event=event)
    backend_factory, _calls = _backend_factory_for(backend)
    engine = Engine(repo, backend_factory=backend_factory)
    engine.config.app_server.command = ["codex", "app-server"]

    engine.request_stop()
    try:
        exit_code = await engine._run_codex_app_server_async("prompt", 1)
    finally:
        engine.clear_stop_request()

    assert exit_code == 0
    assert backend.interrupt_calls
    assert engine._last_run_interrupted is True


@pytest.mark.anyio
async def test_autorunner_timeout_interrupts_turn(repo) -> None:
    event = asyncio.Event()
    backend = FakeBackend(wait_event=event)
    backend_factory, _calls = _backend_factory_for(backend)
    engine = Engine(repo, backend_factory=backend_factory)
    engine.config.app_server.turn_timeout_seconds = 0.01
    engine.config.app_server.command = ["codex", "app-server"]

    exit_code = await engine._run_codex_app_server_async("prompt", 1)

    assert exit_code == 1
    assert backend.interrupt_calls
