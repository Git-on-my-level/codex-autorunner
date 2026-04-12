from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .replies import render_reply_context
from .runner_execution import capture_git_state, execute_turn
from .runner_prompt import _truncate_text_by_bytes
from .runner_thread_bindings import (
    clear_ticket_thread_binding,
    is_stale_ticket_thread_error,
    resolve_ticket_thread_binding,
    store_ticket_thread_binding,
)

_logger = logging.getLogger(__name__)


def build_reply_context(
    *, reply_paths, last_seq: int, workspace_root: Path
) -> tuple[str, int]:
    return render_reply_context(
        reply_history_dir=reply_paths.reply_history_dir,
        last_seq=last_seq,
        workspace_root=workspace_root,
    )


def capture_pre_turn_git_state(
    *, workspace_root: Path
) -> tuple[Optional[str], Optional[str]]:
    git_state_before = capture_git_state(workspace_root=workspace_root)
    return (
        git_state_before["repo_fingerprint_before"],
        git_state_before["head_before_turn"],
    )


def load_previous_ticket_content(
    *,
    current_path: Path,
    ticket_paths: list[Path],
    include_previous_ticket_context: bool,
) -> Optional[str]:
    if not include_previous_ticket_context:
        return None
    try:
        if current_path not in ticket_paths:
            return None
        curr_idx = ticket_paths.index(current_path)
        if curr_idx <= 0:
            return None
        prev_path = ticket_paths[curr_idx - 1]
        content = prev_path.read_text(encoding="utf-8")
        return _truncate_text_by_bytes(content, 16384)
    except OSError:
        _logger.debug("failed to read previous ticket content", exc_info=True)
        return None


def build_turn_options(*, ticket_doc) -> dict[str, Any]:
    turn_options: dict[str, Any] = {}
    if ticket_doc.frontmatter.model:
        turn_options["model"] = ticket_doc.frontmatter.model
    if ticket_doc.frontmatter.reasoning:
        turn_options["reasoning"] = ticket_doc.frontmatter.reasoning
    return turn_options


def increment_turn_counters(
    *,
    state: dict[str, Any],
    ticket_turns: int,
) -> tuple[int, int]:
    total_turns = int(state.get("total_turns") or 0) + 1
    next_ticket_turns = ticket_turns + 1
    state["total_turns"] = total_turns
    state["ticket_turns"] = next_ticket_turns
    return total_turns, next_ticket_turns


def record_successful_turn_state(
    *,
    state: dict[str, Any],
    reply_seq: int,
    reply_max_seq: int,
    result,
    ticket_id: str,
    ticket_path: str,
    agent_id: str,
    profile: Optional[str],
    binding_decision: dict[str, Any],
) -> None:
    if reply_max_seq > reply_seq:
        state["reply_seq"] = reply_max_seq
    state["last_agent_output"] = result.text
    state.pop("network_retry", None)
    state["last_agent_id"] = result.agent_id
    state["last_agent_conversation_id"] = result.conversation_id
    state["last_agent_turn_id"] = result.turn_id
    store_ticket_thread_binding(
        state,
        ticket_id=ticket_id,
        ticket_path=ticket_path,
        agent_id=agent_id,
        profile=profile,
        thread_target_id=result.conversation_id,
        binding_decision=binding_decision,
    )


async def execute_turn_with_thread_binding_retry(
    *,
    agent_pool,
    workspace_root: Path,
    state: dict[str, Any],
    ticket_id: str,
    ticket_path: str,
    agent_id: str,
    profile: Optional[str],
    prompt: str,
    lint_retry_conversation_id: Optional[str],
    turn_options: Optional[dict[str, Any]],
    emit_event,
    max_network_retries: int,
    current_network_retries: int,
):
    reuse_conversation_id, binding_decision = resolve_ticket_thread_binding(
        state,
        ticket_id=ticket_id,
        ticket_path=ticket_path,
        agent_id=agent_id,
        profile=profile,
        lint_retry_conversation_id=lint_retry_conversation_id,
    )

    try:
        result = await execute_turn(
            agent_pool=agent_pool,
            agent_id=agent_id,
            prompt=prompt,
            workspace_root=workspace_root,
            conversation_id=reuse_conversation_id,
            options=turn_options,
            emit_event=emit_event,
            max_network_retries=max_network_retries,
            current_network_retries=current_network_retries,
        )
    except Exception as exc:
        if not reuse_conversation_id or not is_stale_ticket_thread_error(exc):
            raise
        clear_ticket_thread_binding(
            state,
            ticket_id=ticket_id,
            reason="stale_or_missing_thread_target",
        )
        binding_decision = {
            "ticket_id": ticket_id,
            "ticket_path": ticket_path,
            "agent_id": agent_id,
            "profile": profile,
            "action": "reset",
            "reason": "stale_or_missing_thread_target",
            "previous_thread_target_id": reuse_conversation_id,
        }
        result = await execute_turn(
            agent_pool=agent_pool,
            agent_id=agent_id,
            prompt=prompt,
            workspace_root=workspace_root,
            conversation_id=None,
            options=turn_options,
            emit_event=emit_event,
            max_network_retries=max_network_retries,
            current_network_retries=current_network_retries,
        )
    return result, binding_decision
