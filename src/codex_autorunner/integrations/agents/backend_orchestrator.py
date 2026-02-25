"""
Backend orchestrator that manages protocol-agnostic backend lifecycle.

The orchestrator sits between the Engine and backend adapters, handling
backend-specific concerns like supervisor management, event handling,
and session/thread tracking while exposing a clean, protocol-neutral
interface to the Engine.
"""

import asyncio
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional

from ...core.config import RepoConfig
from ...core.ports.agent_backend import AgentBackend
from ...core.ports.backend_orchestrator import (
    BackendOrchestrator as BackendOrchestratorProtocol,
)
from ...core.ports.run_event import RunEvent
from ...core.state import RunnerState
from ...integrations.app_server.threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from .wiring import AgentBackendFactory, BackendFactory

NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]
SessionIdGetter = Callable[[str], Optional[str]]
SessionIdSetter = Callable[[str, str], None]


@dataclass
class BackendContext:
    """Context for a backend run."""

    agent_id: str
    session_id: Optional[str]
    turn_id: Optional[str]
    thread_info: Optional[dict[str, Any]]


class BackendOrchestrator:
    """
    Orchestrates backend operations, keeping Engine protocol-agnostic.

    This class manages:
    - Backend factory and lifecycle
    - Backend-specific supervisors (Codex app server, OpenCode)
    - Backend-specific event handling and notification routing
    - Session/thread tracking for backends that support it
    """

    def __init__(
        self,
        repo_root: Path,
        config: RepoConfig,
        *,
        notification_handler: Optional[NotificationHandler] = None,
        logger: Optional[logging.Logger] = None,
    ):
        from .wiring import build_agent_backend_factory

        self._repo_root = repo_root
        self._config = config
        self._logger = logger or logging.getLogger("codex_autorunner.backend")
        self._notification_handler = notification_handler

        # Backend factory manages creation and caching of backends
        self._backend_factory: BackendFactory = build_agent_backend_factory(
            repo_root, config
        )

        # Active backend for current run
        self._active_backend: Optional[AgentBackend] = None

        # Context tracking
        self._context: Optional[BackendContext] = None

        # Session registry for backend-specific session tracking
        self._app_server_threads = AppServerThreadRegistry(
            default_app_server_threads_path(repo_root)
        )
        self._app_server_threads_lock = threading.Lock()

    async def get_backend(
        self,
        agent_id: str,
        state: RunnerState,
    ) -> AgentBackend:
        """Get a backend instance for the given agent."""
        backend = self._backend_factory(agent_id, state, self._notification_handler)
        self._active_backend = backend
        return backend

    async def start_session(
        self,
        agent_id: str,
        state: RunnerState,
        session_id: Optional[str] = None,
        workspace_root: Optional[Path] = None,
    ) -> str:
        """
        Start a backend session.

        Returns the session/thread ID.
        """
        backend = await self.get_backend(agent_id, state)

        effective_workspace = workspace_root or self._repo_root
        context: dict[str, Any] = {"workspace": str(effective_workspace)}
        if session_id:
            context["session_id"] = session_id

        target = {"workspace": str(effective_workspace)}

        session = await backend.start_session(target, context)

        # Track context
        self._context = BackendContext(
            agent_id=agent_id,
            session_id=session,
            turn_id=None,
            thread_info=None,
        )

        return session

    async def run_turn(
        self,
        agent_id: str,
        state: RunnerState,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_root: Optional[Path] = None,
    ) -> AsyncGenerator[RunEvent, None]:
        """
        Run a turn on the backend.

        Yields RunEvent objects.
        """
        reuse_session = bool(getattr(self._config, "autorunner_reuse_session", False))
        effective_session_id = session_id
        if reuse_session and session_key and not effective_session_id:
            effective_session_id = self.get_thread_id(session_key)
        if reuse_session and effective_session_id is None and self._context is not None:
            effective_session_id = self._context.session_id

        effective_session_id = await self.start_session(
            agent_id,
            state,
            session_id=effective_session_id,
            workspace_root=workspace_root,
        )
        if reuse_session and session_key and effective_session_id:
            self.set_thread_id(session_key, effective_session_id)

        backend = self._active_backend
        assert backend is not None, "backend should be initialized before run_turn"
        app_server_cfg = getattr(self._config, "app_server", None)
        turn_timeout_seconds = getattr(app_server_cfg, "turn_timeout_seconds", None)

        backend.configure(
            approval_policy=state.autorunner_approval_policy,
            approval_policy_default="never",
            sandbox_policy=state.autorunner_sandbox_mode,
            sandbox_policy_default="dangerFullAccess",
            model=model,
            reasoning=reasoning,
            reasoning_effort=reasoning,
            turn_timeout_seconds=turn_timeout_seconds,
            notification_handler=self._notification_handler,
            default_approval_decision=self._config.ticket_flow.default_approval_decision,
        )

        async for event in backend.run_turn_events(effective_session_id, prompt):
            yield event

            # Update context from events
            if hasattr(event, "session_id") and event.session_id:
                if self._context:
                    self._context.session_id = event.session_id

    async def interrupt(self, agent_id: str, state: RunnerState) -> None:
        """Interrupt the current backend session."""
        if self._context and self._context.session_id:
            backend = await self.get_backend(agent_id, state)
            await backend.interrupt(self._context.session_id)

    def get_context(self) -> Optional[BackendContext]:
        """Get the current backend context."""
        return self._context

    def get_last_turn_id(self) -> Optional[str]:
        """Get the last turn ID from the active backend."""
        if self._active_backend:
            return getattr(self._active_backend, "last_turn_id", None)
        if self._context:
            return self._context.turn_id
        return None

    def get_last_thread_info(self) -> Optional[dict[str, Any]]:
        """Get the last thread info from the active backend."""
        if self._active_backend:
            return getattr(self._active_backend, "last_thread_info", None)
        if self._context:
            return self._context.thread_info
        return None

    def get_last_token_total(self) -> Optional[dict[str, Any]]:
        """Get the last token total from the active backend."""
        if self._active_backend:
            return getattr(self._active_backend, "last_token_total", None)
        return None

    async def close_all(self) -> None:
        """Close all backends and clean up resources."""
        close_all = getattr(self._backend_factory, "close_all", None)
        if close_all:
            result = close_all()
            if asyncio.iscoroutine(result):
                await result
        self._active_backend = None
        self._context = None

    def update_context(
        self,
        *,
        turn_id: Optional[str] = None,
        thread_info: Optional[dict[str, Any]] = None,
    ) -> None:
        """Update the backend context with new information."""
        if self._context:
            if turn_id:
                self._context.turn_id = turn_id
            if thread_info:
                self._context.thread_info = thread_info

    def get_thread_id(self, session_key: str) -> Optional[str]:
        """Get the thread ID for a given session key."""
        with self._app_server_threads_lock:
            return self._app_server_threads.get_thread_id(session_key)

    def set_thread_id(self, session_key: str, thread_id: str) -> None:
        """Set the thread ID for a given session key."""
        with self._app_server_threads_lock:
            self._app_server_threads.set_thread_id(session_key, thread_id)

    def reset_thread_id(self, session_key: str) -> bool:
        """Reset the persisted thread ID for a given session key."""
        with self._app_server_threads_lock:
            return self._app_server_threads.reset_thread(session_key)

    def _agent_backend_factory(self) -> Optional[AgentBackendFactory]:
        if isinstance(self._backend_factory, AgentBackendFactory):
            return self._backend_factory
        return None

    def ensure_opencode_supervisor(self) -> Optional[Any]:
        """
        Ensure OpenCode supervisor exists.

        This method delegates to the backend factory for supervisor management,
        keeping Engine protocol-agnostic.
        """
        factory = self._agent_backend_factory()
        if factory is not None:
            return factory._ensure_opencode_supervisor()
        return None

    def build_app_server_supervisor(
        self, *, event_prefix: str, notification_handler: Optional[NotificationHandler]
    ) -> Optional[Any]:
        """
        Build a Codex app server supervisor factory.

        This method centralizes backend-specific supervisor creation, keeping
        Engine protocol-agnostic.
        """
        from .wiring import build_app_server_supervisor_factory

        factory_fn = build_app_server_supervisor_factory(
            self._config, logger=self._logger
        )
        return factory_fn(event_prefix, notification_handler)


def build_backend_orchestrator(
    repo_root: Path, config: RepoConfig
) -> BackendOrchestratorProtocol:
    """
    Build a BackendOrchestrator for protocol-agnostic backend management.
    """
    return BackendOrchestrator(
        repo_root=repo_root,
        config=config,
        notification_handler=None,
        logger=logging.getLogger("codex_autorunner.backend"),
    )


__all__ = [
    "BackendOrchestrator",
    "BackendContext",
    "build_backend_orchestrator",
]
