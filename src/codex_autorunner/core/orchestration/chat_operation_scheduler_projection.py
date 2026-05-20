"""Compatibility exports for Discord interaction lifecycle projections."""

from __future__ import annotations

from .discord_interaction_lifecycle import (
    discord_execution_status_to_chat_operation_state,
    discord_interaction_has_pending_delivery,
    discord_scheduler_state_to_chat_operation_state,
    discord_scheduler_terminal_outcome,
)

__all__ = [
    "discord_execution_status_to_chat_operation_state",
    "discord_interaction_has_pending_delivery",
    "discord_scheduler_state_to_chat_operation_state",
    "discord_scheduler_terminal_outcome",
]
