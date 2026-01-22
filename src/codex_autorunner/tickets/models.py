from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence


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
    requires: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TicketDoc:
    path: Path
    index: int
    frontmatter: TicketFrontmatter
    body: str


@dataclass(frozen=True)
class UserMessage:
    mode: str  # "notify" | "pause"
    body: str
    title: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboxDispatch:
    seq: int
    message: UserMessage
    archived_dir: Path
    archived_files: tuple[Path, ...]


@dataclass(frozen=True)
class TicketRunConfig:
    ticket_dir: Path
    runs_dir: Path
    max_total_turns: int = 25
    max_lint_retries: int = 3
    auto_commit: bool = True
    checkpoint_message_template: str = (
        "CAR checkpoint: run={run_id} turn={turn} agent={agent}"
    )


@dataclass(frozen=True)
class TicketResult:
    """Return value of a single TicketRunner.step() call."""

    status: str  # "continue" | "paused" | "completed" | "failed"
    state: dict[str, Any]
    reason: Optional[str] = None
    dispatch: Optional[OutboxDispatch] = None
    current_ticket: Optional[str] = None
    agent_output: Optional[str] = None
    agent_id: Optional[str] = None
    agent_conversation_id: Optional[str] = None
    agent_turn_id: Optional[str] = None


def normalize_requires(requires: Optional[Sequence[Any]]) -> tuple[str, ...]:
    if not requires:
        return ()
    items: list[str] = []
    for item in requires:
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned:
                items.append(cleaned)
    # Preserve order but drop duplicates.
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return tuple(unique)
