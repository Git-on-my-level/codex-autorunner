"""Shared chat-surface presentation contract for adapter implementations.

This module belongs to `integrations/chat` because it defines adapter-layer UX
semantics shared by multiple transports. It may depend on control-plane state
definitions, but it must remain presentation-only: labels, phase grouping, and
affordances live here; durable lifecycle authority does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ...core.orchestration.chat_operation_state import (
    CHAT_OPERATION_TERMINAL_STATES,
    ChatOperationState,
)

ChatUxPhase = Literal["pending", "blocked", "active", "terminal"]

CHAT_UX_CONTRACT_VERSION = "chat-ux-foundation-v1"


@dataclass(frozen=True)
class ChatUxStateDescriptor:
    """Adapter-facing metadata for rendering a shared chat operation state."""

    state: ChatOperationState
    phase: ChatUxPhase
    title: str
    show_spinner: bool
    allow_interrupt: bool
    allow_retry: bool
    terminal: bool


CHAT_UX_STATE_DESCRIPTORS: dict[ChatOperationState, ChatUxStateDescriptor] = {
    ChatOperationState.RECEIVED: ChatUxStateDescriptor(
        state=ChatOperationState.RECEIVED,
        phase="pending",
        title="Received",
        show_spinner=True,
        allow_interrupt=False,
        allow_retry=False,
        terminal=False,
    ),
    ChatOperationState.ROUTING: ChatUxStateDescriptor(
        state=ChatOperationState.ROUTING,
        phase="pending",
        title="Routing",
        show_spinner=True,
        allow_interrupt=False,
        allow_retry=False,
        terminal=False,
    ),
    ChatOperationState.BLOCKED: ChatUxStateDescriptor(
        state=ChatOperationState.BLOCKED,
        phase="blocked",
        title="Waiting for input",
        show_spinner=False,
        allow_interrupt=False,
        allow_retry=False,
        terminal=False,
    ),
    ChatOperationState.QUEUED: ChatUxStateDescriptor(
        state=ChatOperationState.QUEUED,
        phase="pending",
        title="Queued",
        show_spinner=True,
        allow_interrupt=True,
        allow_retry=False,
        terminal=False,
    ),
    ChatOperationState.RUNNING: ChatUxStateDescriptor(
        state=ChatOperationState.RUNNING,
        phase="active",
        title="Running",
        show_spinner=True,
        allow_interrupt=True,
        allow_retry=False,
        terminal=False,
    ),
    ChatOperationState.DELIVERING: ChatUxStateDescriptor(
        state=ChatOperationState.DELIVERING,
        phase="active",
        title="Delivering",
        show_spinner=True,
        allow_interrupt=False,
        allow_retry=False,
        terminal=False,
    ),
    ChatOperationState.COMPLETED: ChatUxStateDescriptor(
        state=ChatOperationState.COMPLETED,
        phase="terminal",
        title="Completed",
        show_spinner=False,
        allow_interrupt=False,
        allow_retry=False,
        terminal=True,
    ),
    ChatOperationState.INTERRUPTED: ChatUxStateDescriptor(
        state=ChatOperationState.INTERRUPTED,
        phase="terminal",
        title="Interrupted",
        show_spinner=False,
        allow_interrupt=False,
        allow_retry=True,
        terminal=True,
    ),
    ChatOperationState.FAILED: ChatUxStateDescriptor(
        state=ChatOperationState.FAILED,
        phase="terminal",
        title="Failed",
        show_spinner=False,
        allow_interrupt=False,
        allow_retry=True,
        terminal=True,
    ),
    ChatOperationState.CANCELLED: ChatUxStateDescriptor(
        state=ChatOperationState.CANCELLED,
        phase="terminal",
        title="Cancelled",
        show_spinner=False,
        allow_interrupt=False,
        allow_retry=True,
        terminal=True,
    ),
}


def get_chat_ux_state_descriptor(state: ChatOperationState) -> ChatUxStateDescriptor:
    """Return the shared adapter-facing descriptor for a control-plane state."""

    return CHAT_UX_STATE_DESCRIPTORS[state]


def is_terminal_chat_ux_state(state: ChatOperationState) -> bool:
    """Mirror terminal state detection for adapter rendering code."""

    return state in CHAT_OPERATION_TERMINAL_STATES


__all__ = [
    "CHAT_UX_CONTRACT_VERSION",
    "CHAT_UX_STATE_DESCRIPTORS",
    "ChatUxPhase",
    "ChatUxStateDescriptor",
    "get_chat_ux_state_descriptor",
    "is_terminal_chat_ux_state",
]
