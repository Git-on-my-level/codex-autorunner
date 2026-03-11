from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisor
from codex_autorunner.core.ports.run_event import Started
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
