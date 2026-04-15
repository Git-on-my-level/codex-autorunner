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
from .runner_commit import process_commit_required
from .runner_execution import (
    capture_git_state,
    capture_git_state_after,
    compute_loop_guard,
    execute_turn,
    should_pause_for_loop,
)
from .runner_post_turn import (
    archive_dispatch_and_create_summary,
    build_pause_result,
    check_ticket_frontmatter,
    checkpoint_git,
    create_runner_pause_dispatch,
    handle_frontmatter_recheck,
)
from .runner_prompt import build_prompt
from .runner_selection import (
    TicketSelectionError,
    select_ticket,
    validate_ticket_for_execution,
)

__all__ = [
    "DEFAULT_MAX_TOTAL_TURNS",
    "AgentPool",
    "AgentTurnRequest",
    "AgentTurnResult",
    "TicketContextEntry",
    "TicketDoc",
    "TicketFrontmatter",
    "TicketResult",
    "TicketRunConfig",
    "TicketRunner",
    "TicketSelectionError",
    "archive_dispatch_and_create_summary",
    "build_pause_result",
    "build_prompt",
    "capture_git_state",
    "capture_git_state_after",
    "check_ticket_frontmatter",
    "checkpoint_git",
    "compute_loop_guard",
    "create_runner_pause_dispatch",
    "execute_turn",
    "handle_frontmatter_recheck",
    "process_commit_required",
    "select_ticket",
    "should_pause_for_loop",
    "validate_ticket_for_execution",
]
