from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .models import TicketFrontmatter


@dataclass(frozen=True)
class SelectedTicket:
    """Selected ticket with validated frontmatter and resolved path."""

    path: Path
    rel_path: str
    frontmatter: TicketFrontmatter
    is_user_agent: bool = False
    is_done: bool = False


@dataclass(frozen=True)
class TicketSelectionResult:
    """Result of ticket selection and validation phase."""

    selected: Optional[SelectedTicket] = None
    status: str = "continue"  # "continue" | "paused" | "completed"
    state_updates: dict[str, Any] = field(default_factory=dict)
    pause_reason: Optional[str] = None
    pause_reason_code: Optional[str] = None
    pause_reason_details: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    reset_commit_state: bool = False


@dataclass(frozen=True)
class ValidatedTicket:
    """Validated ticket ready for execution."""

    path: Path
    rel_path: str
    ticket_doc: Any
    skip_execution: bool = False


@dataclass(frozen=True)
class TicketValidationResult:
    """Result of ticket validation for execution."""

    validated: Optional[ValidatedTicket] = None
    status: str = "continue"  # "continue" | "paused"
    pause_reason: Optional[str] = None
    pause_reason_code: Optional[str] = None
    pause_reason_details: Optional[str] = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PromptInputs:
    """Inputs required for prompt assembly."""

    ticket_path: Path
    ticket_rel_path: str
    ticket_doc: Any
    last_agent_output: Optional[str] = None
    last_checkpoint_error: Optional[str] = None
    commit_required: bool = False
    commit_attempt: int = 0
    commit_max_attempts: int = 2
    outbox_paths: Any = None
    lint_errors: Optional[list[str]] = None
    reply_context: Optional[str] = None
    requested_context: Optional[str] = None
    previous_ticket_content: Optional[str] = None
    prior_no_change_turns: int = 0
    dispatch_dir: Optional[Path] = None
    dispatch_path: Optional[Path] = None


@dataclass(frozen=True)
class PromptResult:
    """Result of prompt assembly phase."""

    prompt: str
    ticket_turns: int
    total_turns: int


@dataclass(frozen=True)
class TurnExecutionInputs:
    """Inputs required for turn execution."""

    agent_id: str
    prompt: str
    workspace_root: Path
    conversation_id: Optional[str] = None
    options: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class TurnExecutionResult:
    """Result of turn execution phase."""

    success: bool
    error: Optional[str] = None
    text: Optional[str] = None
    agent_id: Optional[str] = None
    conversation_id: Optional[str] = None
    turn_id: Optional[str] = None
    is_network_error: bool = False
    should_retry: bool = False
    network_retries: int = 0
    head_before_turn: Optional[str] = None
    head_after_turn: Optional[str] = None
    clean_after_turn: Optional[bool] = None
    status_after_turn: Optional[str] = None
    agent_committed_this_turn: Optional[bool] = None
    repo_fingerprint_before: Optional[str] = None
    repo_fingerprint_after: Optional[str] = None


@dataclass(frozen=True)
class PostTurnInputs:
    """Inputs required for post-turn reconciliation."""

    workspace_root: Path
    repo_id: str
    run_id: str
    current_ticket_id: str
    dispatch_seq: int
    agent_output: str
    agent_id: str
    total_turns: int
    head_before_turn: Optional[str]
    turn_diff_stats: Optional[dict[str, Any]] = None
    emit_event: Any = None


@dataclass(frozen=True)
class PostTurnResult:
    """Result of post-turn reconciliation phase."""

    status: str = "continue"  # "continue" | "paused" | "completed" | "failed"
    dispatch: Any = None
    state_updates: dict[str, Any] = field(default_factory=dict)
    loop_guard_updates: dict[str, Any] = field(default_factory=dict)
    pause_reason: Optional[str] = None
    pause_reason_code: Optional[str] = None
    pause_reason_details: Optional[str] = None
    ticket_done: bool = False
    commit_required: bool = False
    commit_pending: bool = False
    commit_retries: int = 0
    checkpoint_error: Optional[str] = None
    lint_updates: Optional[dict[str, Any]] = None
    updated_frontmatter: Any = None
    lint_errors: Optional[list[str]] = None


@dataclass(frozen=True)
class RunnerState:
    """Structured view of persisted runner state for type safety."""

    status: Optional[str] = None
    reason: Optional[str] = None
    reason_code: Optional[str] = None
    reason_details: Optional[str] = None
    current_ticket: Optional[str] = None
    ticket_turns: int = 0
    total_turns: int = 0
    reply_seq: int = 0
    dispatch_seq: int = 0
    last_agent_output: Optional[str] = None
    last_agent_id: Optional[str] = None
    last_agent_conversation_id: Optional[str] = None
    last_agent_turn_id: Optional[str] = None
    last_checkpoint_error: Optional[str] = None
    network_retry: Optional[dict[str, Any]] = None
    commit: Optional[dict[str, Any]] = None
    lint: Optional[dict[str, Any]] = None
    loop_guard: Optional[dict[str, Any]] = None
    pause_context: Optional[dict[str, Any]] = None
    ticket_thread_bindings: Optional[dict[str, Any]] = None
    ticket_thread_debug: Optional[dict[str, Any]] = None

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RunnerState:
        """Create RunnerState from persisted dict."""
        return RunnerState(
            status=data.get("status"),
            reason=data.get("reason"),
            reason_code=data.get("reason_code"),
            reason_details=data.get("reason_details"),
            current_ticket=data.get("current_ticket"),
            ticket_turns=int(data.get("ticket_turns") or 0),
            total_turns=int(data.get("total_turns") or 0),
            reply_seq=int(data.get("reply_seq") or 0),
            dispatch_seq=int(data.get("dispatch_seq") or 0),
            last_agent_output=data.get("last_agent_output"),
            last_agent_id=data.get("last_agent_id"),
            last_agent_conversation_id=data.get("last_agent_conversation_id"),
            last_agent_turn_id=data.get("last_agent_turn_id"),
            last_checkpoint_error=data.get("last_checkpoint_error"),
            network_retry=data.get("network_retry"),
            commit=data.get("commit"),
            lint=data.get("lint"),
            loop_guard=data.get("loop_guard"),
            pause_context=data.get("pause_context"),
            ticket_thread_bindings=data.get("ticket_thread_bindings"),
            ticket_thread_debug=data.get("ticket_thread_debug"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for persistence."""
        result: dict[str, Any] = {}
        if self.status is not None:
            result["status"] = self.status
        if self.reason is not None:
            result["reason"] = self.reason
        if self.reason_code is not None:
            result["reason_code"] = self.reason_code
        if self.reason_details is not None:
            result["reason_details"] = self.reason_details
        if self.current_ticket is not None:
            result["current_ticket"] = self.current_ticket
        if self.ticket_turns:
            result["ticket_turns"] = self.ticket_turns
        if self.total_turns:
            result["total_turns"] = self.total_turns
        if self.reply_seq:
            result["reply_seq"] = self.reply_seq
        if self.dispatch_seq:
            result["dispatch_seq"] = self.dispatch_seq
        if self.last_agent_output is not None:
            result["last_agent_output"] = self.last_agent_output
        if self.last_agent_id is not None:
            result["last_agent_id"] = self.last_agent_id
        if self.last_agent_conversation_id is not None:
            result["last_agent_conversation_id"] = self.last_agent_conversation_id
        if self.last_agent_turn_id is not None:
            result["last_agent_turn_id"] = self.last_agent_turn_id
        if self.last_checkpoint_error is not None:
            result["last_checkpoint_error"] = self.last_checkpoint_error
        if self.network_retry is not None:
            result["network_retry"] = self.network_retry
        if self.commit is not None:
            result["commit"] = self.commit
        if self.lint is not None:
            result["lint"] = self.lint
        if self.loop_guard is not None:
            result["loop_guard"] = self.loop_guard
        if self.pause_context is not None:
            result["pause_context"] = self.pause_context
        if self.ticket_thread_bindings is not None:
            result["ticket_thread_bindings"] = self.ticket_thread_bindings
        if self.ticket_thread_debug is not None:
            result["ticket_thread_debug"] = self.ticket_thread_debug
        return result
