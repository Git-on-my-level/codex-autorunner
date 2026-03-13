from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from codex_autorunner.agents.opencode.runtime import OpenCodeTurnOutput
from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisor
from codex_autorunner.core.config import TicketFlowConfig
from codex_autorunner.core.ports.agent_backend import AgentEvent
from codex_autorunner.core.ports.run_event import Completed, Started
from codex_autorunner.core.state import RunnerState
from codex_autorunner.integrations.agents.backend_orchestrator import (
    BackendOrchestrator,
)
from codex_autorunner.integrations.agents.opencode_backend import OpenCodeBackend


def _make_mock_client() -> MagicMock:
    client = MagicMock()
    client.close = AsyncMock()
    return client


class TestOpenCodeBackendClose:
    def test_close_returns_awaitable_when_client_exists(self) -> None:
        backend = OpenCodeBackend(base_url="http://localhost:8080")
        mock_client = _make_mock_client()
        backend._client = mock_client

        result = backend.close()

        assert result is not None
        import inspect

        assert inspect.isawaitable(result)
        if inspect.iscoroutine(result):
            result.close()

    @pytest.mark.anyio
    async def test_close_sets_client_to_none_after_close(self) -> None:
        backend = OpenCodeBackend(base_url="http://localhost:8080")
        mock_client = _make_mock_client()
        backend._client = mock_client

        result = backend.close()
        if result:
            await result

        assert backend._client is None

    def test_close_returns_none_when_no_client(self) -> None:
        backend = OpenCodeBackend(supervisor=MagicMock(), workspace_root=Path("/tmp"))
        assert backend._client is None

        result = backend.close()

        assert result is None

    @pytest.mark.anyio
    async def test_close_handles_client_close_exception(self) -> None:
        backend = OpenCodeBackend(base_url="http://localhost:8080")
        mock_client = _make_mock_client()
        mock_client.close.side_effect = Exception("close failed")
        backend._client = mock_client

        result = backend.close()
        if result:
            await result

    def test_close_does_not_close_supervisor(self) -> None:
        mock_supervisor = MagicMock(spec=OpenCodeSupervisor)
        backend = OpenCodeBackend(
            supervisor=mock_supervisor, workspace_root=Path("/tmp")
        )

        backend.close()

        mock_supervisor.close_all.assert_not_called()


class TestOpenCodeBackendLifecycle:
    def test_backend_with_base_url_creates_client(self) -> None:
        backend = OpenCodeBackend(base_url="http://localhost:8080")

        assert backend._client is not None

    def test_backend_without_base_url_waits_for_supervisor(self) -> None:
        mock_supervisor = MagicMock(spec=OpenCodeSupervisor)
        mock_client = _make_mock_client()
        mock_supervisor.get_client = AsyncMock(return_value=mock_client)

        backend = OpenCodeBackend(
            supervisor=mock_supervisor, workspace_root=Path("/tmp")
        )

        assert backend._client is None

    @pytest.mark.anyio
    async def test_ensure_client_gets_client_from_supervisor(self) -> None:
        mock_supervisor = MagicMock(spec=OpenCodeSupervisor)
        mock_client = _make_mock_client()
        mock_supervisor.get_client = AsyncMock(return_value=mock_client)

        backend = OpenCodeBackend(
            supervisor=mock_supervisor, workspace_root=Path("/tmp")
        )

        client = await backend._ensure_client()

        assert client is mock_client
        mock_supervisor.get_client.assert_called_once()
        # When using supervisor, backend does not cache the client
        # to handle supervisor restarts properly


class TestSafeCloseClient:
    @pytest.mark.anyio
    async def test_safe_close_client_closes_client(self) -> None:
        from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisor

        supervisor = OpenCodeSupervisor(["opencode", "serve"])
        mock_client = _make_mock_client()

        await supervisor._safe_close_client(mock_client)

        mock_client.close.assert_called_once()

    @pytest.mark.anyio
    async def test_safe_close_client_handles_none(self) -> None:
        from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisor

        supervisor = OpenCodeSupervisor(["opencode", "serve"])

        # Should not raise
        await supervisor._safe_close_client(None)

    @pytest.mark.anyio
    async def test_safe_close_client_handles_exception(self) -> None:
        from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisor

        supervisor = OpenCodeSupervisor(["opencode", "serve"])
        mock_client = _make_mock_client()
        mock_client.close.side_effect = Exception("close failed")

        # Should not raise
        await supervisor._safe_close_client(mock_client)


class TestOpenCodeBackendTurnLifecycle:
    def test_mark_turn_started_called_when_supervisor_configured(self) -> None:
        mock_supervisor = MagicMock()
        mock_supervisor.mark_turn_started = AsyncMock()
        mock_supervisor.mark_turn_finished = AsyncMock()

        backend = OpenCodeBackend(
            supervisor=mock_supervisor, workspace_root=Path("/tmp")
        )

        # Check that _mark_turn_started calls the supervisor
        import asyncio

        asyncio.get_event_loop().run_until_complete(backend._mark_turn_started())

        mock_supervisor.mark_turn_started.assert_called_once()

    def test_mark_turn_finished_called_when_supervisor_configured(self) -> None:
        mock_supervisor = MagicMock()
        mock_supervisor.mark_turn_started = AsyncMock()
        mock_supervisor.mark_turn_finished = AsyncMock()

        backend = OpenCodeBackend(
            supervisor=mock_supervisor, workspace_root=Path("/tmp")
        )

        import asyncio

        asyncio.get_event_loop().run_until_complete(backend._mark_turn_finished())

        mock_supervisor.mark_turn_finished.assert_called_once()

    def test_mark_turn_not_called_when_no_supervisor(self) -> None:
        backend = OpenCodeBackend(base_url="http://localhost:8080")

        import asyncio

        # Should not raise
        asyncio.get_event_loop().run_until_complete(backend._mark_turn_started())
        asyncio.get_event_loop().run_until_complete(backend._mark_turn_finished())

    def test_ensure_client_no_cache_when_using_supervisor(self) -> None:
        mock_supervisor = MagicMock()
        mock_client = _make_mock_client()
        mock_supervisor.get_client = AsyncMock(return_value=mock_client)

        backend = OpenCodeBackend(
            supervisor=mock_supervisor, workspace_root=Path("/tmp")
        )

        # When using supervisor, _client should remain None
        # because we always get fresh clients from supervisor
        assert backend._client is None

        import asyncio

        asyncio.get_event_loop().run_until_complete(backend._ensure_client())

        # Client should NOT be cached when using supervisor
        assert backend._client is None
        mock_supervisor.get_client.assert_called_once()

    def test_ensure_client_uses_cache_when_no_supervisor(self) -> None:
        backend = OpenCodeBackend(base_url="http://localhost:8080")

        # When using base_url (no supervisor), client should be cached
        assert backend._client is not None

    @pytest.mark.anyio
    async def test_run_turn_events_ensures_client_before_marking_turn_started(
        self,
    ) -> None:
        backend = OpenCodeBackend(
            supervisor=MagicMock(),
            workspace_root=Path("/tmp"),
        )
        observed: list[str] = []

        async def _fake_ensure_client():
            observed.append("ensure_client")
            return _make_mock_client()

        async def _fake_mark_turn_started():
            observed.append("mark_turn_started")

        async def _fake_mark_turn_finished():
            observed.append("mark_turn_finished")

        async def _fake_run_turn_events_impl(_session_id: str, _message: str):
            observed.append("run_turn_events_impl")
            yield Started(timestamp="now", session_id="session-1")

        backend._ensure_client = _fake_ensure_client  # type: ignore[method-assign]
        backend._mark_turn_started = _fake_mark_turn_started  # type: ignore[method-assign]
        backend._mark_turn_finished = _fake_mark_turn_finished  # type: ignore[method-assign]
        backend._run_turn_events_impl = _fake_run_turn_events_impl  # type: ignore[method-assign]

        events = [event async for event in backend.run_turn_events("session-1", "Ping")]

        assert len(events) == 1
        assert observed == [
            "ensure_client",
            "mark_turn_started",
            "run_turn_events_impl",
            "mark_turn_finished",
        ]


class _SessionClientStub:
    def __init__(self) -> None:
        self.created_session_ids: list[str] = []
        self.disposed_session_ids: list[str] = []
        self.sent_session_ids: list[str] = []
        self.requested_session_ids: list[str] = []

    async def create_session(self, *, title: str, directory: str) -> dict[str, str]:
        _ = (title, directory)
        session_id = f"session-{len(self.created_session_ids) + 1}"
        self.created_session_ids.append(session_id)
        return {"sessionId": session_id}

    async def get_session(self, session_id: str) -> dict[str, str]:
        self.requested_session_ids.append(session_id)
        return {"sessionId": session_id}

    async def dispose(self, session_id: str) -> None:
        self.disposed_session_ids.append(session_id)

    async def send_message(self, session_id: str, **_kwargs: object) -> None:
        self.sent_session_ids.append(session_id)

    async def prompt_async(
        self, *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        return {}


@pytest.mark.anyio
async def test_run_turn_disposes_temporary_session_and_resets_state() -> None:
    backend = OpenCodeBackend(base_url="http://localhost:8080")
    client = _SessionClientStub()
    backend._client = client

    async def _fake_events():
        yield AgentEvent.message_complete("done")

    backend._yield_events_until_completion = _fake_events  # type: ignore[method-assign]

    first_events = [event async for event in backend.run_turn("", "hello")]
    second_events = [event async for event in backend.run_turn("", "again")]

    assert first_events[-1].data["final_message"] == "done"
    assert second_events[-1].data["final_message"] == "done"
    assert client.created_session_ids == ["session-1", "session-2"]
    assert client.disposed_session_ids == ["session-1", "session-2"]
    assert client.sent_session_ids == ["session-1", "session-2"]
    assert backend._session_id is None
    assert backend._temporary_session_id is None


@pytest.mark.anyio
async def test_run_turn_events_keeps_resumed_session_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from codex_autorunner.integrations.agents import opencode_backend as backend_module

    backend = OpenCodeBackend(base_url="http://localhost:8080", workspace_root=tmp_path)
    client = _SessionClientStub()
    backend._client = client

    async def _fake_collect(*_args: object, **_kwargs: object) -> OpenCodeTurnOutput:
        ready_event = _kwargs.get("ready_event")
        if ready_event is not None:
            ready_event.set()
        return OpenCodeTurnOutput(text="done")

    async def _fake_missing_env(*_args: object, **_kwargs: object) -> list[str]:
        return []

    monkeypatch.setattr(backend_module, "collect_opencode_output", _fake_collect)
    monkeypatch.setattr(backend_module, "opencode_missing_env", _fake_missing_env)
    monkeypatch.setattr(
        backend_module, "build_turn_id", lambda session_id: f"{session_id}:turn"
    )

    session_id = await backend.start_session(
        target={},
        context={"workspace": str(tmp_path), "session_id": "resume-1"},
    )
    events = [event async for event in backend.run_turn_events(session_id, "hello")]

    assert isinstance(events[-1], Completed)
    assert client.requested_session_ids == ["resume-1"]
    assert client.created_session_ids == []
    assert client.disposed_session_ids == []
    assert backend._session_id == "resume-1"
    assert backend._temporary_session_id is None


@pytest.mark.anyio
async def test_backend_orchestrator_reuses_fresh_opencode_session_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from codex_autorunner.integrations.agents import opencode_backend as backend_module

    backend = OpenCodeBackend(base_url="http://localhost:8080", workspace_root=tmp_path)
    client = _SessionClientStub()
    backend._client = client

    async def _fake_collect(*_args: object, **_kwargs: object) -> OpenCodeTurnOutput:
        ready_event = _kwargs.get("ready_event")
        if ready_event is not None:
            ready_event.set()
        return OpenCodeTurnOutput(text="done")

    async def _fake_missing_env(*_args: object, **_kwargs: object) -> list[str]:
        return []

    async def _fake_get_session(session_id: str) -> dict[str, str]:
        client.requested_session_ids.append(session_id)
        if session_id in client.disposed_session_ids:
            raise RuntimeError("session missing")
        return {"sessionId": session_id}

    monkeypatch.setattr(backend_module, "collect_opencode_output", _fake_collect)
    monkeypatch.setattr(backend_module, "opencode_missing_env", _fake_missing_env)
    monkeypatch.setattr(
        backend_module, "build_turn_id", lambda session_id: f"{session_id}:turn"
    )
    client.get_session = _fake_get_session  # type: ignore[method-assign]

    config = SimpleNamespace(
        autorunner_reuse_session=True,
        app_server=SimpleNamespace(turn_timeout_seconds=321.0),
        ticket_flow=TicketFlowConfig(
            approval_mode="yolo",
            default_approval_decision="accept",
            include_previous_ticket_context=False,
        ),
    )
    orchestrator = BackendOrchestrator(repo_root=tmp_path, config=config)
    orchestrator._backend_factory = lambda _agent_id, _state, _handler: backend
    state = RunnerState(None, "idle", None, None, None)

    first_events = [
        event
        async for event in orchestrator.run_turn(
            "opencode", state, "hello", session_key="autorunner.opencode"
        )
    ]
    second_events = [
        event
        async for event in orchestrator.run_turn(
            "opencode", state, "again", session_key="autorunner.opencode"
        )
    ]

    assert isinstance(first_events[-1], Completed)
    assert isinstance(second_events[-1], Completed)
    assert client.created_session_ids == ["session-1"]
    assert client.requested_session_ids == ["session-1"]
    assert client.disposed_session_ids == []
