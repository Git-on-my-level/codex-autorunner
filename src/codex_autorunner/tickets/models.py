from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_MAX_TOTAL_TURNS = 50


@dataclass(frozen=True)
class TicketFrontmatter:
    """Parsed, validated ticket frontmatter.

    Only a minimal set of keys are required for orchestration. Additional
    keys are preserved in `extra` for forward compatibility.
    """

    agent: str
    done: bool
    title: Optional[str] = None
    goal: Optional[str] = None
    # Optional model/reasoning overrides for this ticket.
    model: Optional[str] = None
    reasoning: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TicketDoc:
    path: Path
    index: int
    frontmatter: TicketFrontmatter
    body: str


@dataclass(frozen=True)
class Dispatch:
    """Agent-to-human communication dispatched via the outbox.

    A Dispatch is the canonical unit of agent→human communication. The mode
    determines whether it's informational or requires human action:
      - "notify": FYI, agent continues working
      - "pause": Handoff, agent yields and awaits human reply
    """

    mode: str  # "notify" | "pause"
    body: str
    title: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_handoff(self) -> bool:
        """True if this dispatch requires human action (mode='pause')."""
        return self.mode == "pause"


@dataclass(frozen=True)
class DispatchRecord:
    """Archived dispatch with sequence number and file references.

    This is the envelope/record created when a Dispatch is archived to the
    dispatch history directory.
    """

    seq: int
    dispatch: Dispatch
    archived_dir: Path
    archived_files: tuple[Path, ...]


@dataclass(frozen=True)
class TicketRunConfig:
    ticket_dir: Path
    runs_dir: Path
    max_total_turns: int = DEFAULT_MAX_TOTAL_TURNS
    max_lint_retries: int = 3
    max_commit_retries: int = 2
    max_network_retries: int = 5
    auto_commit: bool = True
    prompt_max_bytes: int = 5 * 1024 * 1024  # 5 MB default budget
    checkpoint_message_template: str = (
        "CAR checkpoint: run={run_id} turn={turn} agent={agent}"
    )
    include_previous_ticket_context: bool = False


@dataclass(frozen=True)
class TicketResult:
    """Return value of a single TicketRunner.step() call."""

    status: str  # "continue" | "paused" | "completed" | "failed"
    state: dict[str, Any]
    reason: Optional[str] = None
    reason_details: Optional[str] = None  # Technical details (git status, etc.)
    dispatch: Optional[DispatchRecord] = None
    current_ticket: Optional[str] = None
    agent_output: Optional[str] = None
    agent_id: Optional[str] = None
    agent_conversation_id: Optional[str] = None
    agent_turn_id: Optional[str] = None
