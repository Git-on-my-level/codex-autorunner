import asyncio
from typing import Callable, List, Optional, Tuple

import pytest

import codex_autorunner.core.engine as engine_module
from codex_autorunner.core.engine import Engine
from codex_autorunner.integrations.app_server.client import TurnResult


class FakeHandle:
    def __init__(
        self, result: TurnResult, event: Optional[asyncio.Event] = None
    ) -> None:
        self.turn_id = "turn-1"
        self.thread_id = "thread-1"
        self._result = result
        self._event = event
        self.wait_calls: List[Optional[float]] = []

    async def wait(self, *, timeout: Optional[float] = None) -> TurnResult:
        self.wait_calls.append(timeout)
        if self._event is not None:
            await self._event.wait()
        return self._result


class FakeClient:
    def __init__(self, handle_factory: Callable[[], FakeHandle]) -> None:
        self._handle_factory = handle_factory
        self.last_handle: Optional[FakeHandle] = None
        self.interrupt_calls: List[Tuple[str, Optional[str]]] = []
        self._thread_id = "thread-1"

    async def thread_resume(self, thread_id: str) -> dict:
        return {"id": thread_id}

    async def thread_start(self, _repo_root: str) -> dict:
        return {"id": self._thread_id}

    async def turn_start(self, *_args, **_kwargs) -> FakeHandle:
        self.last_handle = self._handle_factory()
        return self.last_handle

    async def turn_interrupt(
        self, turn_id: str, *, thread_id: Optional[str] = None
    ) -> dict:
        self.interrupt_calls.append((turn_id, thread_id))
        if self.last_handle and self.last_handle._event is not None:
            self.last_handle._event.set()
        return {"turn_id": turn_id, "thread_id": thread_id}


class FakeSupervisor:
    instances: list["FakeSupervisor"] = []
    shared_client: Optional[FakeClient] = None

    def __init__(self, *_args, **_kwargs) -> None:
        type(self).instances.append(self)

    async def get_client(self, _workspace_root) -> FakeClient:
        if self.shared_client is None:
            raise AssertionError("FakeSupervisor.shared_client not set")
        return self.shared_client

    async def close_all(self) -> None:
        return None


def _make_turn_result() -> TurnResult:
    return TurnResult(
        turn_id="turn-1",
        status=None,
        agent_messages=[],
        errors=[],
        raw_events=[],
    )


def test_app_server_supervisor_reused(repo, monkeypatch) -> None:
    engine = Engine(repo)
    FakeSupervisor.instances = []
    monkeypatch.setattr(engine_module, "WorkspaceAppServerSupervisor", FakeSupervisor)

    def env_builder(*_args, **_kwargs):
        return {}

    first = engine._ensure_app_server_supervisor(env_builder)
    second = engine._ensure_app_server_supervisor(env_builder)

    assert first is second
    assert len(FakeSupervisor.instances) == 1


@pytest.mark.anyio
async def test_autorunner_turn_timeout_uses_config(repo, monkeypatch) -> None:
    engine = Engine(repo)
    engine.config.app_server.turn_timeout_seconds = 42

    result = _make_turn_result()
    FakeSupervisor.shared_client = FakeClient(lambda: FakeHandle(result))
    monkeypatch.setattr(engine_module, "WorkspaceAppServerSupervisor", FakeSupervisor)

    exit_code = await engine._run_codex_app_server_async("prompt", 1)

    assert exit_code == 0
    assert FakeSupervisor.shared_client.last_handle.wait_calls == [None]


@pytest.mark.anyio
async def test_autorunner_stop_interrupts_turn(repo, monkeypatch) -> None:
    engine = Engine(repo)

    event = asyncio.Event()
    result = _make_turn_result()
    FakeSupervisor.shared_client = FakeClient(lambda: FakeHandle(result, event))
    monkeypatch.setattr(engine_module, "WorkspaceAppServerSupervisor", FakeSupervisor)

    engine.request_stop()
    try:
        exit_code = await engine._run_codex_app_server_async("prompt", 1)
    finally:
        engine.clear_stop_request()

    assert exit_code == 0
    assert FakeSupervisor.shared_client.interrupt_calls
    assert engine._last_run_interrupted is True


@pytest.mark.anyio
async def test_autorunner_timeout_interrupts_turn(repo, monkeypatch) -> None:
    engine = Engine(repo)
    engine.config.app_server.turn_timeout_seconds = 0.01

    event = asyncio.Event()
    result = _make_turn_result()
    FakeSupervisor.shared_client = FakeClient(lambda: FakeHandle(result, event))
    monkeypatch.setattr(engine_module, "WorkspaceAppServerSupervisor", FakeSupervisor)

    exit_code = await engine._run_codex_app_server_async("prompt", 1)

    assert exit_code == 1
    assert FakeSupervisor.shared_client.interrupt_calls
