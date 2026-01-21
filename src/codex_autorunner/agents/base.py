from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional, Protocol

from .types import AgentId, ConversationRef, ModelCatalog, TurnRef


class AgentHarness(Protocol):
    agent_id: AgentId
    display_name: str

    async def ensure_ready(self, workspace_root: Path) -> None: ...

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog: ...

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef: ...

    async def list_conversations(
        self, workspace_root: Path
    ) -> list[ConversationRef]: ...

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef: ...

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> TurnRef: ...

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> TurnRef: ...

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None: ...

    def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[str]: ...


__all__ = ["AgentHarness"]
