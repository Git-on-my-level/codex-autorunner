from __future__ import annotations

from typing import Any, Optional, Sequence

from .update_notifier import (
    format_update_status_message as _format_update_status_message,
)


def _clean_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _format_backticked(value: Any, default: str) -> str:
    text = _clean_text(value) or default
    return f"`{text}`"


def _state_suffix(state_label: Optional[str]) -> str:
    text = _clean_text(state_label)
    return f" ({text})" if text else ""


def _join_nonempty(parts: Sequence[str]) -> str:
    return " ".join(part for part in parts if part)


def build_thread_detail_lines(
    *,
    thread_id: Optional[Any] = None,
    workspace_path: Optional[Any] = None,
    actor_label: Optional[Any] = None,
    model: Optional[Any] = None,
    effort: Optional[Any] = None,
    headline: Optional[Any] = None,
    extra_lines: Sequence[Any] = (),
) -> list[str]:
    lines: list[str] = []
    headline_text = _clean_text(headline)
    if headline_text:
        lines.append(headline_text)
    elif thread_id is not None:
        lines.append(f"Thread: {_format_backticked(thread_id, 'thread')}")
    if workspace_path is not None:
        lines.append(f"Directory: {_clean_text(workspace_path) or 'unbound'}")
    if actor_label is not None:
        lines.append(f"Agent: {_clean_text(actor_label) or 'default'}")
    if model is not None:
        lines.append(f"Model: {_clean_text(model) or 'default'}")
    if effort is not None:
        lines.append(f"Effort: {_clean_text(effort) or 'default'}")
    for value in extra_lines:
        text = _clean_text(value)
        if text:
            lines.append(text)
    return lines


def build_fresh_session_started_lines(
    *,
    mode_label: Any,
    actor_label: Any,
    state_label: Optional[str] = None,
) -> list[str]:
    mode_text = _clean_text(mode_label)
    headline = "Started a fresh"
    if mode_text:
        headline += f" {mode_text} session"
    else:
        headline += " session"
    return [
        _join_nonempty([headline, "for", _format_backticked(actor_label, "this agent")])
        + _state_suffix(state_label)
        + "."
    ]


def build_branch_reset_started_lines(
    *,
    branch_name: Any,
    default_branch: Any,
    mode_label: Any,
    actor_label: Any,
    state_label: Optional[str] = None,
    setup_command_count: Optional[int] = None,
) -> list[str]:
    mode_text = _clean_text(mode_label)
    mode_phrase = f"{mode_text} session" if mode_text else "session"
    line = _join_nonempty(
        [
            "Reset branch",
            _format_backticked(branch_name, "branch"),
            "to",
            _format_backticked(
                f"origin/{_clean_text(default_branch) or 'main'}", "origin/main"
            ),
            "in current workspace and started fresh",
            mode_phrase,
            "for",
            _format_backticked(actor_label, "this agent"),
        ]
    )
    line += _state_suffix(state_label) + "."
    if isinstance(setup_command_count, int) and setup_command_count > 0:
        line += f" Ran {setup_command_count} setup command(s)."
    return [line]


def build_reset_state_lines(
    *,
    mode_label: Any,
    actor_label: Any,
    state_label: Optional[str] = None,
    action_label: str = "Reset",
) -> list[str]:
    line = _join_nonempty(
        [
            _clean_text(action_label) or "Reset",
            _clean_text(mode_label) or "session",
            "thread state",
        ]
    )
    line += _state_suffix(state_label)
    line += f" for {_format_backticked(actor_label, 'this agent')}."
    return [line]


def build_resumed_thread_lines(
    *,
    thread_id: Any,
    workspace_path: Optional[Any] = None,
    actor_label: Optional[Any] = None,
    model: Optional[Any] = None,
    effort: Optional[Any] = None,
    user_preview: Optional[Any] = None,
    assistant_preview: Optional[Any] = None,
) -> list[str]:
    lines = build_thread_detail_lines(
        thread_id=thread_id,
        workspace_path=workspace_path,
        actor_label=actor_label,
        model=model,
        effort=effort,
        headline=f"Resumed thread {_format_backticked(thread_id, 'thread')}",
    )
    if _clean_text(user_preview):
        lines.extend(["", "User:", _clean_text(user_preview) or ""])
    if _clean_text(assistant_preview):
        lines.extend(["", "Assistant:", _clean_text(assistant_preview) or ""])
    return lines


def format_resumed_session_message(
    *,
    mode_label: Any,
    actor_label: Any,
    thread_id: Any,
) -> str:
    mode_text = _clean_text(mode_label)
    prefix = "Resumed"
    if mode_text:
        prefix += f" {mode_text} session"
    else:
        prefix += " session"
    return f"{prefix} for {_format_backticked(actor_label, 'this agent')} with thread {_format_backticked(thread_id, 'thread')}."


def format_update_preparing_message(
    target_label: Any,
    *,
    restart_required: bool = True,
    include_status_hint: bool = True,
    status_command: str = "/update status",
    completion_scope_label: str = "this channel",
) -> str:
    target_text = _clean_text(target_label) or "update"
    parts = [
        f"Preparing update ({target_text}). Checking whether the update can start now."
    ]
    if restart_required:
        parts.append(
            f"If it does, the selected service(s) will restart shortly and I will post completion status in {completion_scope_label}."
        )
    else:
        parts.append(
            f"If it does, I will post completion status in {completion_scope_label}."
        )
    if include_status_hint:
        parts.append(f"Use {status_command} for progress.")
    return " ".join(parts)


def format_update_started_message(
    target_label: Any,
    *,
    restart_required: bool = True,
    include_status_hint: bool = True,
    status_command: str = "/update status",
    include_completion_notice: bool = True,
    completion_scope_label: str = "this channel",
) -> str:
    target_text = _clean_text(target_label) or "update"
    parts = [f"Update started ({target_text})."]
    if restart_required:
        parts.append("The selected service(s) will restart.")
    if include_completion_notice:
        parts.append(f"I will post completion status in {completion_scope_label}.")
    if include_status_hint:
        parts.append(f"Use {status_command} for progress.")
    return " ".join(parts)


def format_update_status_message(status: Optional[dict[str, Any]]) -> str:
    rendered = _format_update_status_message(status)
    if not status:
        return rendered
    lines = [rendered]
    repo_ref = _clean_text(status.get("repo_ref"))
    if repo_ref:
        lines.append(f"Ref: {repo_ref}")
    log_path = _clean_text(status.get("log_path"))
    if log_path:
        lines.append(f"Log: {log_path}")
    return "\n".join(lines)


__all__ = [
    "build_branch_reset_started_lines",
    "build_fresh_session_started_lines",
    "build_reset_state_lines",
    "build_thread_detail_lines",
    "build_resumed_thread_lines",
    "format_resumed_session_message",
    "format_update_preparing_message",
    "format_update_started_message",
    "format_update_status_message",
]
