from __future__ import annotations

import inspect
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ...core.config import RepoConfig
from ...core.destinations import DockerDestination
from ...core.ports.agent_backend import AgentBackend
from ...core.state import RunnerState
from ..app_server.env import build_app_server_env
from ..app_server.supervisor import WorkspaceAppServerSupervisor
from .codex_backend import CodexAppServerBackend
from .destination_wrapping import (
    resolve_destination_from_config,
    wrap_command_for_destination,
)
from .opencode_backend import OpenCodeBackend
from .opencode_supervisor_factory import build_opencode_supervisor_from_repo_config

NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]
BackendFactory = Callable[
    [str, RunnerState, Optional[NotificationHandler]], AgentBackend
]
SupervisorFactory = Callable[[str, Optional[NotificationHandler]], Any]


class AgentBackendFactory:
    def __init__(self, repo_root: Path, config: RepoConfig) -> None:
        self._repo_root = repo_root
        self._config = config
        self._logger = logging.getLogger("codex_autorunner.app_server")
        self._destination = resolve_destination_from_config(
            getattr(config, "effective_destination", {"kind": "local"})
        )
        self._backend_cache: dict[str, AgentBackend] = {}
        self._opencode_supervisor: Optional[Any] = None
        self._codex_supervisor: Optional[WorkspaceAppServerSupervisor] = None

    def __call__(
        self,
        agent_id: str,
        state: RunnerState,
        notification_handler: Optional[NotificationHandler],
    ) -> AgentBackend:
        if agent_id == "codex":
            if not self._config.app_server.command:
                raise ValueError("app_server.command is required for codex backend")

            approval_policy = state.autorunner_approval_policy or "never"
            sandbox_mode = state.autorunner_sandbox_mode or "dangerFullAccess"
            if sandbox_mode == "workspaceWrite":
                sandbox_policy: Any = {
                    "type": "workspaceWrite",
                    "writableRoots": [str(self._repo_root)],
                    "networkAccess": bool(state.autorunner_workspace_write_network),
                }
            else:
                sandbox_policy = sandbox_mode

            model = state.autorunner_model_override or self._config.codex_model
            reasoning_effort = (
                state.autorunner_effort_override or self._config.codex_reasoning
            )

            default_approval_decision = (
                self._config.ticket_flow.default_approval_decision
            )
            turn_timeout_seconds = self._config.app_server.turn_timeout_seconds

            cached = self._backend_cache.get(agent_id)
            if cached is None:
                cached = CodexAppServerBackend(
                    supervisor=self._ensure_codex_supervisor(),
                    workspace_root=self._repo_root,
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    turn_timeout_seconds=turn_timeout_seconds,
                    auto_restart=self._config.app_server.auto_restart,
                    request_timeout=self._config.app_server.request_timeout,
                    turn_stall_timeout_seconds=self._config.app_server.turn_stall_timeout_seconds,
                    turn_stall_poll_interval_seconds=self._config.app_server.turn_stall_poll_interval_seconds,
                    turn_stall_recovery_min_interval_seconds=self._config.app_server.turn_stall_recovery_min_interval_seconds,
                    max_message_bytes=self._config.app_server.client.max_message_bytes,
                    oversize_preview_bytes=self._config.app_server.client.oversize_preview_bytes,
                    max_oversize_drain_bytes=self._config.app_server.client.max_oversize_drain_bytes,
                    restart_backoff_initial_seconds=self._config.app_server.client.restart_backoff_initial_seconds,
                    restart_backoff_max_seconds=self._config.app_server.client.restart_backoff_max_seconds,
                    restart_backoff_jitter_ratio=self._config.app_server.client.restart_backoff_jitter_ratio,
                    output_policy=self._config.app_server.output.policy,
                    notification_handler=notification_handler,
                    logger=self._logger,
                    default_approval_decision=default_approval_decision,
                )
                self._backend_cache[agent_id] = cached
            else:
                if isinstance(cached, CodexAppServerBackend):
                    cached.configure(
                        approval_policy=approval_policy,
                        sandbox_policy=sandbox_policy,
                        model=model,
                        reasoning_effort=reasoning_effort,
                        turn_timeout_seconds=turn_timeout_seconds,
                        notification_handler=notification_handler,
                        default_approval_decision=default_approval_decision,
                    )
            return cached

        if agent_id == "opencode":
            agent_cfg = self._config.agents.get("opencode")
            base_url = agent_cfg.base_url if agent_cfg else None
            username = os.environ.get("OPENCODE_SERVER_USERNAME")
            password = os.environ.get("OPENCODE_SERVER_PASSWORD")
            if password and not username:
                username = "opencode"
            auth = (username, password) if username and password else None

            cached = self._backend_cache.get(agent_id)
            if cached is None:
                if not base_url:
                    supervisor = self._ensure_opencode_supervisor()
                    if supervisor is None:
                        raise ValueError("opencode backend is not configured")
                    cached = OpenCodeBackend(
                        supervisor=supervisor,
                        workspace_root=self._repo_root,
                        auth=auth,
                        timeout=self._config.app_server.request_timeout,
                        model=state.autorunner_model_override,
                        reasoning=state.autorunner_effort_override,
                        approval_policy=state.autorunner_approval_policy,
                        session_stall_timeout_seconds=self._config.opencode.session_stall_timeout_seconds,
                        logger=self._logger,
                    )
                else:
                    cached = OpenCodeBackend(
                        base_url=base_url,
                        workspace_root=self._repo_root,
                        auth=auth,
                        timeout=self._config.app_server.request_timeout,
                        model=state.autorunner_model_override,
                        reasoning=state.autorunner_effort_override,
                        approval_policy=state.autorunner_approval_policy,
                        session_stall_timeout_seconds=self._config.opencode.session_stall_timeout_seconds,
                        logger=self._logger,
                    )
                self._backend_cache[agent_id] = cached
            else:
                if isinstance(cached, OpenCodeBackend):
                    cached.configure(
                        model=state.autorunner_model_override,
                        reasoning=state.autorunner_effort_override,
                        approval_policy=state.autorunner_approval_policy,
                    )
            return cached

        raise ValueError(f"Unsupported agent backend: {agent_id}")

    def _ensure_opencode_supervisor(self) -> Optional[Any]:
        if self._opencode_supervisor is not None:
            return self._opencode_supervisor
        opencode_command_override: Optional[list[str]] = None
        if isinstance(self._destination, DockerDestination):
            agent_cmd = self._config.agent_serve_command("opencode")
            if not agent_cmd:
                opencode_binary = self._config.agent_binary("opencode")
                agent_cmd = [
                    opencode_binary,
                    "serve",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    "0",
                ]
            wrapped = wrap_command_for_destination(
                command=agent_cmd,
                destination=self._destination,
                repo_root=self._repo_root,
            )
            opencode_command_override = wrapped.command
        supervisor = build_opencode_supervisor_from_repo_config(
            self._config,
            workspace_root=self._repo_root,
            logger=self._logger,
            base_env=None,
            command_override=opencode_command_override,
        )
        self._opencode_supervisor = supervisor
        return supervisor

    async def close_all(self) -> None:
        backends = list(self._backend_cache.values())
        self._backend_cache = {}
        for backend in backends:
            close = getattr(backend, "close", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result
        if self._opencode_supervisor is not None:
            try:
                await self._opencode_supervisor.close_all()
            except Exception:
                self._logger.warning(
                    "Failed closing opencode supervisor", exc_info=True
                )
            self._opencode_supervisor = None
        if self._codex_supervisor is not None:
            try:
                await self._codex_supervisor.close_all()
            except Exception:
                self._logger.warning("Failed closing codex supervisor", exc_info=True)
            self._codex_supervisor = None

    def _ensure_codex_supervisor(self) -> WorkspaceAppServerSupervisor:
        if self._codex_supervisor is not None:
            return self._codex_supervisor
        if not self._config.app_server.command:
            raise ValueError("app_server.command is required for codex backend")

        supervisor_command = list(self._config.app_server.command)
        state_root = self._config.app_server.state_root
        if isinstance(self._destination, DockerDestination):
            wrapped = wrap_command_for_destination(
                command=supervisor_command,
                destination=self._destination,
                repo_root=self._repo_root,
            )
            supervisor_command = wrapped.command
            if wrapped.state_root_override is not None:
                state_root = wrapped.state_root_override

        def _env_builder(
            workspace_root: Path, _workspace_id: str, state_dir: Path
        ) -> dict[str, str]:
            state_dir.mkdir(parents=True, exist_ok=True)
            return build_app_server_env(
                supervisor_command,
                workspace_root,
                state_dir,
                logger=self._logger,
                event_prefix="autorunner",
            )

        self._codex_supervisor = WorkspaceAppServerSupervisor(
            supervisor_command,
            state_root=state_root,
            env_builder=_env_builder,
            logger=self._logger,
            auto_restart=self._config.app_server.auto_restart,
            max_handles=self._config.app_server.max_handles,
            idle_ttl_seconds=self._config.app_server.idle_ttl_seconds,
            request_timeout=self._config.app_server.request_timeout,
            turn_stall_timeout_seconds=self._config.app_server.turn_stall_timeout_seconds,
            turn_stall_poll_interval_seconds=self._config.app_server.turn_stall_poll_interval_seconds,
            turn_stall_recovery_min_interval_seconds=self._config.app_server.turn_stall_recovery_min_interval_seconds,
            max_message_bytes=self._config.app_server.client.max_message_bytes,
            oversize_preview_bytes=self._config.app_server.client.oversize_preview_bytes,
            max_oversize_drain_bytes=self._config.app_server.client.max_oversize_drain_bytes,
            restart_backoff_initial_seconds=self._config.app_server.client.restart_backoff_initial_seconds,
            restart_backoff_max_seconds=self._config.app_server.client.restart_backoff_max_seconds,
            restart_backoff_jitter_ratio=self._config.app_server.client.restart_backoff_jitter_ratio,
            output_policy=self._config.app_server.output.policy,
        )
        return self._codex_supervisor


def build_agent_backend_factory(repo_root: Path, config: RepoConfig) -> BackendFactory:
    return AgentBackendFactory(repo_root, config)


def build_app_server_supervisor_factory(
    config: RepoConfig,
    *,
    logger: Optional[logging.Logger] = None,
) -> SupervisorFactory:
    app_logger = logger or logging.getLogger("codex_autorunner.app_server")
    destination = resolve_destination_from_config(
        getattr(config, "effective_destination", {"kind": "local"})
    )

    def factory(
        event_prefix: str, notification_handler: Optional[NotificationHandler]
    ) -> WorkspaceAppServerSupervisor:
        if not config.app_server.command:
            raise ValueError("app_server.command is required for supervisor")

        supervisor_command = list(config.app_server.command)
        state_root = config.app_server.state_root
        if isinstance(destination, DockerDestination):
            wrapped = wrap_command_for_destination(
                command=supervisor_command,
                destination=destination,
                repo_root=config.root,
            )
            supervisor_command = wrapped.command
            if wrapped.state_root_override is not None:
                state_root = wrapped.state_root_override

        def _env_builder(
            workspace_root: Path, _workspace_id: str, state_dir: Path
        ) -> dict[str, str]:
            state_dir.mkdir(parents=True, exist_ok=True)
            return build_app_server_env(
                supervisor_command,
                workspace_root,
                state_dir,
                logger=app_logger,
                event_prefix=event_prefix,
            )

        return WorkspaceAppServerSupervisor(
            supervisor_command,
            state_root=state_root,
            env_builder=_env_builder,
            logger=app_logger,
            notification_handler=notification_handler,
            auto_restart=config.app_server.auto_restart,
            max_handles=config.app_server.max_handles,
            idle_ttl_seconds=config.app_server.idle_ttl_seconds,
            request_timeout=config.app_server.request_timeout,
            turn_stall_timeout_seconds=config.app_server.turn_stall_timeout_seconds,
            turn_stall_poll_interval_seconds=config.app_server.turn_stall_poll_interval_seconds,
            turn_stall_recovery_min_interval_seconds=config.app_server.turn_stall_recovery_min_interval_seconds,
            max_message_bytes=config.app_server.client.max_message_bytes,
            oversize_preview_bytes=config.app_server.client.oversize_preview_bytes,
            max_oversize_drain_bytes=config.app_server.client.max_oversize_drain_bytes,
            restart_backoff_initial_seconds=config.app_server.client.restart_backoff_initial_seconds,
            restart_backoff_max_seconds=config.app_server.client.restart_backoff_max_seconds,
            restart_backoff_jitter_ratio=config.app_server.client.restart_backoff_jitter_ratio,
            output_policy=config.app_server.output.policy,
        )

    return factory


__all__ = [
    "AgentBackendFactory",
    "build_agent_backend_factory",
    "build_app_server_supervisor_factory",
]
