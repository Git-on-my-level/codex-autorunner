from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisor
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
        assert backend._client is mock_client
        mock_supervisor.get_client.assert_called_once()


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
