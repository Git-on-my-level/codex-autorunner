from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from ..core.flows.models import FlowEventType

EmitEventFn = Callable[[FlowEventType, dict[str, Any]], None]


@dataclass(frozen=True)
class AgentTurnRequest:
    agent_id: str  # "codex" | "opencode"
    prompt: str
    workspace_root: Path
    conversation_id: Optional[str] = None
    # Optional, agent-specific extras.
    options: Optional[dict[str, Any]] = None
    # Optional flow event emitter (for live streaming).
    emit_event: Optional[EmitEventFn] = None
    # Optional list of additional messages to send in the same turn.
    # Each message is a dict with a "text" field. Agents that support
    # multiple messages will receive all of them; others may queue them.
    additional_messages: Optional[list[dict[str, Any]]] = None


@dataclass(frozen=True)
class AgentTurnResult:
    agent_id: str
    conversation_id: str
    turn_id: str
    text: str
    error: Optional[str] = None
    raw: Optional[dict[str, Any]] = None


class AgentPool(Protocol):
    """Port for ticket-flow agent execution."""

    async def run_turn(self, req: AgentTurnRequest) -> AgentTurnResult: ...

    async def close_all(self) -> None: ...
