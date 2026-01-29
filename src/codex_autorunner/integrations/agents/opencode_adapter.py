import logging
from pathlib import Path
from typing import MutableMapping, Optional

from ...agents.opencode.client import OpenCodeClient
from ...agents.opencode.supervisor import OpenCodeSupervisor

_logger = logging.getLogger(__name__)


class OpenCodeAdapterOrchestrator:
    """
    Orchestrates OpenCode backend sessions using OpenCodeSupervisor.

    This adapter wraps the OpenCodeSupervisor to provide an AgentBackend-compatible
    interface for use by the Engine.
    """

    def __init__(
        self,
        *,
        opencode_command: Optional[list[str]] = None,
        opencode_binary: Optional[str] = None,
        workspace_root: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
        request_timeout: Optional[float] = None,
        max_handles: Optional[int] = None,
        idle_ttl_seconds: Optional[float] = None,
        session_stall_timeout_seconds: Optional[float] = None,
        base_env: Optional[MutableMapping[str, str]] = None,
        subagent_models: Optional[dict[str, str]] = None,
    ):
        self._opencode_command = opencode_command
        self._opencode_binary = opencode_binary
        self._workspace_root = workspace_root
        self._logger = logger or _logger
        self._request_timeout = request_timeout
        self._max_handles = max_handles
        self._idle_ttl_seconds = idle_ttl_seconds
        self._session_stall_timeout_seconds = session_stall_timeout_seconds
        self._base_env = base_env
        self._subagent_models = subagent_models

        self._supervisor: Optional[OpenCodeSupervisor] = None
        self._client: Optional[OpenCodeClient] = None
        self._session_id: Optional[str] = None

    async def ensure_supervisor(self) -> Optional[OpenCodeSupervisor]:
        """Ensure the OpenCode supervisor is initialized."""
        if self._supervisor is None:
            self._supervisor = self._build_supervisor()
        return self._supervisor

    async def get_client(self, workspace_root: Path) -> OpenCodeClient:
        """Get or create an OpenCode client for the given workspace."""
        supervisor = await self.ensure_supervisor()
        if supervisor is None:
            raise RuntimeError(
                "OpenCode is not configured: neither opencode_command nor opencode_binary is set"
            )
        if self._client is None:
            self._client = await supervisor.get_client(workspace_root)
        return self._client

    async def close_all(self) -> None:
        """Close the supervisor and clean up resources."""
        if self._supervisor is not None:
            await self._supervisor.close_all()
            self._supervisor = None
        self._client = None
        self._session_id = None

    def _build_supervisor(self) -> Optional[OpenCodeSupervisor]:
        """Build the OpenCodeSupervisor instance."""
        command = list(self._opencode_command or [])
        if not command and self._opencode_binary:
            command = [
                self._opencode_binary,
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                "0",
            ]

        if not command:
            return None

        username = None
        password = None
        if self._base_env is not None:
            username = self._base_env.get("OPENCODE_SERVER_USERNAME")
            password = self._base_env.get("OPENCODE_SERVER_PASSWORD")
            if password and not username:
                username = "opencode"

        return OpenCodeSupervisor(
            command,
            logger=self._logger,
            request_timeout=self._request_timeout,
            max_handles=self._max_handles,
            idle_ttl_seconds=self._idle_ttl_seconds,
            session_stall_timeout_seconds=self._session_stall_timeout_seconds,
            username=username if password else None,
            password=password if password else None,
            base_env=self._base_env,
            subagent_models=self._subagent_models,
        )
