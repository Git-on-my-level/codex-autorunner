"""Ticket-based workflow primitives.

This package provides a simple, file-backed orchestration layer built around
markdown tickets with YAML frontmatter.
"""

from .agent_pool import AgentPool, AgentTurnRequest, AgentTurnResult
from .models import TicketDoc, TicketFrontmatter, TicketResult, TicketRunConfig
from .runner import TicketRunner

__all__ = [
    "AgentPool",
    "AgentTurnRequest",
    "AgentTurnResult",
    "TicketDoc",
    "TicketFrontmatter",
    "TicketResult",
    "TicketRunConfig",
    "TicketRunner",
]
