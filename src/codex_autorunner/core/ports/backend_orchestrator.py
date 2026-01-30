"""Protocol for backend orchestrators used by the Engine."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Optional, Protocol

from .run_event import RunEvent


class BackendOrchestrator(Protocol):
    def run_turn(
        self,
        *,
        agent_id: str,
        state: Any,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        session_key: str,
    ) -> AsyncGenerator[RunEvent, None]: ...

    async def interrupt(self, agent_id: str, state: Any) -> None: ...

    def get_thread_id(self, session_key: str) -> Optional[str]: ...

    def set_thread_id(self, session_key: str, thread_id: str) -> None: ...

    def build_app_server_supervisor(
        self,
        *,
        event_prefix: str,
        notification_handler: Optional[Any] = None,
    ) -> Optional[Any]: ...

    def ensure_opencode_supervisor(self) -> Optional[Any]: ...

    def get_last_turn_id(self) -> Optional[str]: ...

    def get_last_thread_info(self) -> Optional[dict[str, Any]]: ...

    def get_last_token_total(self) -> Optional[dict[str, Any]]: ...
