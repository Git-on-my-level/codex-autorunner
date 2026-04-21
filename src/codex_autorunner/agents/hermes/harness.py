from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...core.orchestration.interfaces import FreshConversationRequiredError
from ...core.text_utils import _normalize_optional_text
from ..acp import ACPMissingSessionError
from ..base import AgentHarness
from ..types import (
    AgentId,
    ConversationRef,
    RuntimeCapability,
    RuntimeCapabilityReport,
    TerminalTurnResult,
    TurnRef,
)
from .supervisor import HermesSupervisor

_logger = logging.getLogger(__name__)

HERMES_RUNTIME_ID = "hermes"
HERMES_ACP_COMMAND = "acp"

HERMES_CAPABILITIES = frozenset(
    [
        RuntimeCapability("durable_threads"),
        RuntimeCapability("message_turns"),
        RuntimeCapability("interrupt"),
        RuntimeCapability("active_thread_discovery"),
        RuntimeCapability("event_streaming"),
        RuntimeCapability("approvals"),
    ]
)


class HermesHarness(AgentHarness):
    agent_id: AgentId = AgentId("hermes")
    display_name = "Hermes"
    capabilities = HERMES_CAPABILITIES

    def __init__(self, supervisor: HermesSupervisor) -> None:
        self._supervisor = supervisor

    def _conversation_ref_from_session(self, session: Any) -> ConversationRef:
        raw = getattr(session, "raw", None)
        raw_mapping = raw if isinstance(raw, dict) else {}
        summary = _normalize_optional_text(getattr(session, "summary", None))
        if summary is None and raw_mapping:
            for key in ("summary", "subtitle", "description"):
                summary = _normalize_optional_text(raw_mapping.get(key))
                if summary:
                    break
        return ConversationRef(
            agent=self.agent_id,
            id=str(getattr(session, "session_id", "") or ""),
            title=_normalize_optional_text(getattr(session, "title", None)),
            summary=summary,
        )

    async def ensure_ready(self, workspace_root: Path) -> None:
        await self._supervisor.ensure_ready(workspace_root)

    async def runtime_capability_report(
        self, workspace_root: Path
    ) -> RuntimeCapabilityReport:
        _ = workspace_root
        return RuntimeCapabilityReport(capabilities=self.capabilities)

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        session = await self._supervisor.create_session(workspace_root, title=title)
        return self._conversation_ref_from_session(session)

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        sessions = await self._supervisor.list_sessions(workspace_root)
        return [self._conversation_ref_from_session(session) for session in sessions]

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        try:
            session = await self._supervisor.resume_session(
                workspace_root, conversation_id
            )
        except ACPMissingSessionError as exc:
            raise FreshConversationRequiredError(
                (
                    f"Hermes session '{conversation_id}' could not be resumed; "
                    "refresh the conversation binding"
                ),
                conversation_id=conversation_id,
                operation="resume_conversation",
                status_code=exc.code,
            ) from exc
        return self._conversation_ref_from_session(session)

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
        _ = reasoning, sandbox_policy, input_items
        turn_id = await self._supervisor.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model=model,
            approval_mode=approval_mode,
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
        resolved_turn_id = str(turn_id or "").strip()
        if not resolved_turn_id:
            raise ValueError("Hermes wait_for_turn requires a turn id")
        return await self._supervisor.wait_for_turn(
            workspace_root,
            conversation_id,
            resolved_turn_id,
            timeout=timeout,
        )

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        await self._supervisor.interrupt_turn(workspace_root, conversation_id, turn_id)

    async def recover_stalled_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: str,
    ) -> Optional[TerminalTurnResult]:
        return await self._supervisor.recover_turn_from_session_store(
            workspace_root,
            conversation_id,
            turn_id,
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
        return await self._supervisor.list_turn_events_snapshot(turn_id)
