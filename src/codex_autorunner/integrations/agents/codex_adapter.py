import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from ...integrations.app_server.client import CodexAppServerClient
from ...integrations.app_server.supervisor import WorkspaceAppServerSupervisor

_logger = logging.getLogger(__name__)

EnvBuilder = Callable[[Path, str, Path], Dict[str, str]]


class CodexAdapterOrchestrator:
    """
    Orchestrates Codex app-server backend sessions using WorkspaceAppServerSupervisor.

    This adapter wraps the WorkspaceAppServerSupervisor to provide an AgentBackend-compatible
    interface for use by the Engine.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        state_root: Path,
        env_builder: EnvBuilder,
        approval_handler: Optional[Any] = None,
        notification_handler: Optional[Any] = None,
        logger: Optional[logging.Logger] = None,
        auto_restart: bool = True,
        request_timeout: Optional[float] = None,
        turn_stall_timeout_seconds: Optional[float] = None,
        turn_stall_poll_interval_seconds: Optional[float] = None,
        turn_stall_recovery_min_interval_seconds: Optional[float] = None,
        default_approval_decision: str = "cancel",
        max_handles: Optional[int] = None,
        idle_ttl_seconds: Optional[float] = None,
    ):
        self._command = command
        self._state_root = state_root
        self._env_builder = env_builder
        self._approval_handler = approval_handler
        self._notification_handler = notification_handler
        self._logger = logger or _logger
        self._auto_restart = auto_restart
        self._request_timeout = request_timeout
        self._turn_stall_timeout_seconds = turn_stall_timeout_seconds
        self._turn_stall_poll_interval_seconds = turn_stall_poll_interval_seconds
        self._turn_stall_recovery_min_interval_seconds = (
            turn_stall_recovery_min_interval_seconds
        )
        self._default_approval_decision = default_approval_decision
        self._max_handles = max_handles
        self._idle_ttl_seconds = idle_ttl_seconds

        self._supervisor: Optional[WorkspaceAppServerSupervisor] = None
        self._client: Optional[CodexAppServerClient] = None

    async def ensure_supervisor(self) -> WorkspaceAppServerSupervisor:
        """Ensure the Codex app-server supervisor is initialized."""
        if self._supervisor is None:
            self._supervisor = WorkspaceAppServerSupervisor(
                self._command,
                state_root=self._state_root,
                env_builder=self._env_builder,
                approval_handler=self._approval_handler,
                notification_handler=self._notification_handler,
                logger=self._logger,
                auto_restart=self._auto_restart,
                request_timeout=self._request_timeout,
                turn_stall_timeout_seconds=self._turn_stall_timeout_seconds,
                turn_stall_poll_interval_seconds=self._turn_stall_poll_interval_seconds,
                turn_stall_recovery_min_interval_seconds=self._turn_stall_recovery_min_interval_seconds,
                default_approval_decision=self._default_approval_decision,
                max_handles=self._max_handles,
                idle_ttl_seconds=self._idle_ttl_seconds,
            )
        return self._supervisor

    async def get_client(self, workspace_root: Path) -> CodexAppServerClient:
        """Get or create a Codex app-server client for the given workspace."""
        supervisor = await self.ensure_supervisor()
        return await supervisor.get_client(workspace_root)

    async def close_all(self) -> None:
        """Close the supervisor and clean up resources."""
        if self._supervisor is not None:
            await self._supervisor.close_all()
            self._supervisor = None
        self._client = None
