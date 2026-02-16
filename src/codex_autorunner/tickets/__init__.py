"""Ticket-based workflow primitives.

This package provides a simple, file-backed orchestration layer built around
markdown tickets with YAML frontmatter.
"""

from .agent_pool import AgentPool, AgentTurnRequest, AgentTurnResult
from .models import (
    DEFAULT_MAX_TOTAL_TURNS,
    TicketContextEntry,
    TicketDoc,
    TicketFrontmatter,
    TicketResult,
    TicketRunConfig,
)
from .runner import TicketRunner

__all__ = [
    "DEFAULT_MAX_TOTAL_TURNS",
    "AgentPool",
    "AgentTurnRequest",
    "AgentTurnResult",
    "TicketDoc",
    "TicketContextEntry",
    "TicketFrontmatter",
    "TicketResult",
    "TicketRunConfig",
    "TicketRunner",
]
