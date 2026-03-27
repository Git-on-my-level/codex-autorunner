from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..base import AgentHarness
from ..types import (
    AgentId,
    ConversationRef,
    RuntimeCapability,
    RuntimeCapabilityReport,
    TerminalTurnResult,
    TurnRef,
)

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
    ]
)


class HermesHarness(AgentHarness):
    agent_id = AgentId = AgentId("hermes")
    display_name = "Hermes"
    capabilities = HERMES_CAPABILITIES

    def __init__(self) -> None:
        pass

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
        _ = workspace_root, title
        raise NotImplementedError("HermesHarness.new_conversation not implemented")

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        _ = workspace_root, conversation_id
        raise NotImplementedError("HermesHarness.resume_conversation not implemented")

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
        _ = (
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode,
            sandbox_policy,
            input_items,
        )
        raise NotImplementedError("HermesHarness.start_turn not implemented")

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = workspace_root, conversation_id, turn_id, timeout
        raise NotImplementedError("HermesHarness.wait_for_turn not implemented")
