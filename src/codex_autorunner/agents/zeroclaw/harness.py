from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..base import AgentHarness
from ..types import (
    AgentId,
    ConversationRef,
    RuntimeCapability,
    RuntimeCapabilityReport,
    TerminalTurnResult,
    TurnRef,
)
from .supervisor import ZeroClawSupervisor

ZEROCLAW_CAPABILITIES = frozenset(
    [
        RuntimeCapability("durable_threads"),
        RuntimeCapability("message_turns"),
        RuntimeCapability("active_thread_discovery"),
        RuntimeCapability("event_streaming"),
    ]
)


class ZeroClawHarness(AgentHarness):
    agent_id: AgentId = AgentId("zeroclaw")
    display_name = "ZeroClaw"
    capabilities = ZEROCLAW_CAPABILITIES

    def __init__(self, supervisor: ZeroClawSupervisor) -> None:
        self._supervisor = supervisor

    async def ensure_ready(self, workspace_root: Path) -> None:
        _ = workspace_root

    async def runtime_capability_report(
        self, workspace_root: Path
    ) -> RuntimeCapabilityReport:
        _ = workspace_root
        return RuntimeCapabilityReport(capabilities=self.capabilities)

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        session_id = await self._supervisor.create_session(workspace_root, title=title)
        return ConversationRef(agent=self.agent_id, id=session_id)

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        session_ids = await self._supervisor.list_sessions(workspace_root)
        return [
            ConversationRef(agent=self.agent_id, id=session_id)
            for session_id in session_ids
        ]

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        session_id = await self._supervisor.attach_session(
            workspace_root,
            conversation_id,
        )
        return ConversationRef(agent=self.agent_id, id=session_id)

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
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> TurnRef:
        _ = reasoning, approval_mode, sandbox_policy, input_items
        turn_id = await self._supervisor.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model=model,
        )
        return TurnRef(conversation_id=conversation_id, turn_id=turn_id)

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        if not turn_id:
            raise ValueError("ZeroClaw wait_for_turn requires a turn id")
        return await self._supervisor.wait_for_turn(
            workspace_root,
            conversation_id,
            turn_id,
            timeout=timeout,
        )

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in self._supervisor.stream_turn_events(
            workspace_root,
            conversation_id,
            turn_id,
        ):
            yield event

    async def list_progress_events(
        self, conversation_id: str, turn_id: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        _ = conversation_id, kwargs
        return await self._supervisor.list_turn_events_by_turn_id(turn_id)


__all__ = ["ZEROCLAW_CAPABILITIES", "ZeroClawHarness"]
