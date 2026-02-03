from __future__ import annotations

import inspect
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ...core.config import RepoConfig
from ...core.ports.agent_backend import AgentBackend
from ...core.state import RunnerState
from ...core.utils import build_opencode_supervisor
from ...workspace import canonical_workspace_root, workspace_id_for_path
from ..app_server.env import build_app_server_env
from ..app_server.supervisor import WorkspaceAppServerSupervisor
from .codex_backend import CodexAppServerBackend
from .opencode_backend import OpenCodeBackend

NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]
BackendFactory = Callable[
    [str, RunnerState, Optional[NotificationHandler]], AgentBackend
]
SupervisorFactory = Callable[[str, Optional[NotificationHandler]], Any]


def _build_workspace_env(
    repo_root: Path,
    config: RepoConfig,
    *,
    event_prefix: str,
    logger: logging.Logger,
) -> dict[str, str]:
    workspace_root = canonical_workspace_root(repo_root)
    workspace_id = workspace_id_for_path(workspace_root)
    state_dir = config.app_server.state_root / workspace_id
    state_dir.mkdir(parents=True, exist_ok=True)
    return build_app_server_env(
        config.app_server.command,
        workspace_root,
        state_dir,
        logger=logger,
        event_prefix=event_prefix,
    )


class AgentBackendFactory:
    def __init__(self, repo_root: Path, config: RepoConfig) -> None:
        self._repo_root = repo_root
        self._config = config
        self._logger = logging.getLogger("codex_autorunner.app_server")
        self._backend_cache: dict[str, AgentBackend] = {}
        self._opencode_supervisor: Optional[Any] = None

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

            env = _build_workspace_env(
                self._repo_root,
                self._config,
                event_prefix="autorunner",
                logger=self._logger,
            )

            cached = self._backend_cache.get(agent_id)
            if cached is None:
                cached = CodexAppServerBackend(
                    command=self._config.app_server.command,
                    cwd=self._repo_root,
                    env=env,
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    turn_timeout_seconds=None,
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
                    notification_handler=notification_handler,
                    logger=self._logger,
                )
                self._backend_cache[agent_id] = cached
            else:
                if isinstance(cached, CodexAppServerBackend):
                    cached.configure(
                        approval_policy=approval_policy,
                        sandbox_policy=sandbox_policy,
                        model=model,
                        reasoning_effort=reasoning_effort,
                        turn_timeout_seconds=None,
                        notification_handler=notification_handler,
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
        opencode_command = self._config.agent_serve_command("opencode")
        opencode_binary = None
        try:
            opencode_binary = self._config.agent_binary("opencode")
        except Exception:
            opencode_binary = None
        agent_config = self._config.agents.get("opencode")
        subagent_models = agent_config.subagent_models if agent_config else None
        supervisor = build_opencode_supervisor(
            opencode_command=opencode_command,
            opencode_binary=opencode_binary,
            workspace_root=self._repo_root,
            logger=self._logger,
            request_timeout=self._config.app_server.request_timeout,
            max_handles=self._config.app_server.max_handles,
            idle_ttl_seconds=self._config.app_server.idle_ttl_seconds,
            session_stall_timeout_seconds=self._config.opencode.session_stall_timeout_seconds,
            max_text_chars=self._config.opencode.max_text_chars,
            base_env=None,
            subagent_models=subagent_models,
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


def build_agent_backend_factory(repo_root: Path, config: RepoConfig) -> BackendFactory:
    return AgentBackendFactory(repo_root, config)


def build_app_server_supervisor_factory(
    config: RepoConfig,
    *,
    logger: Optional[logging.Logger] = None,
) -> SupervisorFactory:
    app_logger = logger or logging.getLogger("codex_autorunner.app_server")

    def factory(
        event_prefix: str, notification_handler: Optional[NotificationHandler]
    ) -> WorkspaceAppServerSupervisor:
        if not config.app_server.command:
            raise ValueError("app_server.command is required for supervisor")

        def _env_builder(
            workspace_root: Path, _workspace_id: str, state_dir: Path
        ) -> dict[str, str]:
            state_dir.mkdir(parents=True, exist_ok=True)
            return build_app_server_env(
                config.app_server.command,
                workspace_root,
                state_dir,
                logger=app_logger,
                event_prefix=event_prefix,
            )

        return WorkspaceAppServerSupervisor(
            config.app_server.command,
            state_root=config.app_server.state_root,
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
        )

    return factory


__all__ = [
    "AgentBackendFactory",
    "build_agent_backend_factory",
    "build_app_server_supervisor_factory",
]
