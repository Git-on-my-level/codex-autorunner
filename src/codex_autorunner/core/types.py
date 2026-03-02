"""Core type definitions.

This module provides type definitions that were previously in Engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Mapping, Optional, Protocol

if TYPE_CHECKING:
    from .ports.agent_backend import AgentBackend
    from .state import RunnerState


class NotificationHandler(Protocol):
    def __call__(self, payload: Mapping[str, object]) -> Awaitable[None]: ...


class BackendFactory(Protocol):
    def __call__(
        self,
        agent_id: str,
        state: "RunnerState",
        notification_handler: Optional[NotificationHandler],
    ) -> "AgentBackend": ...


class AppServerSupervisorFactory(Protocol):
    def __call__(
        self,
        event_prefix: str,
        notification_handler: Optional[NotificationHandler],
    ) -> Any: ...


__all__ = [
    "NotificationHandler",
    "BackendFactory",
    "AppServerSupervisorFactory",
]
