from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Sequence

from ...core.redaction import redact_text
from ...integrations.chat.help_catalog import build_telegram_help_text
from ...integrations.chat.review_commits import (  # noqa: F401
    _format_review_commit_label,
    _parse_review_commit_log,
)
from ...integrations.chat.status_diagnostics import (
    extract_rate_limits,
    format_sandbox_policy,
)
from ...integrations.chat.thread_summaries import (  # noqa: F401
    _coerce_thread_list,
    _extract_text_payload,
    _extract_thread_list_cursor,
    _extract_thread_preview_parts,
)
from ...integrations.chat.turn_metrics import (  # noqa: F401
    _extract_context_usage_percent,
    _format_tui_token_usage,
    _format_turn_metrics,
)
from .constants import (
    TRACE_MESSAGE_TOKENS,
)
from .handlers.commands_spec import CommandSpec
from .lock_utils import (  # noqa: F401
    _lock_payload_summary,
    _read_lock_payload,
    _telegram_lock_path,
)
from .model_formatting import (  # noqa: F401
    ModelOption,
    _coerce_model_entries,
    _coerce_model_options,
    _display_name_is_model_alias,
    _format_feature_flags,
    _format_mcp_list,
    _format_model_list,
    _format_skills_list,
    _normalize_model_name,
)
from .preview_utils import (  # noqa: F401
    DISPATCH_BEGIN_STRIP_RE,
    FIRST_USER_PREVIEW_IGNORE_PATTERNS,
    GITHUB_URL_TRAILING_PUNCTUATION,
    SPECIAL_PREVIEW_MATCHERS,
    _coerce_preview_field,
    _coerce_preview_field_raw,
    _coerce_thread_payload,
    _compact_preview,
    _compact_seed_preview_matcher,
    _compact_seed_summary,
    _extract_compact_goal,
    _extract_first_user_preview,
    _extract_rollout_first_user_preview,
    _extract_rollout_path,
    _extract_rollout_preview,
    _extract_thread_resume_parts,
    _extract_turns_first_user_preview,
    _extract_turns_preview,
    _format_missing_thread_label,
    _format_preview_parts,
    _format_resume_summary,
    _format_summary_preview,
    _format_thread_preview,
    _github_preview_matcher,
    _head_text_lines,
    _is_ignored_first_user_preview,
    _is_no_agent_response,
    _normalize_preview_text,
    _preview_from_text,
    _sanitize_user_preview,
    _special_preview_from_text,
    _strip_dispatch_begin,
    _strip_list_marker,
    _strip_url_trailing_punctuation,
    _thread_summary_preview,
    _truncate_text,
)
from .rate_limit_utils import (  # noqa: F401
    _coerce_number,
    _compute_used_percent,
    _format_percent,
    _format_rate_limit_window,
    _rate_limit_window_minutes,
)
from .shell_utils import (  # noqa: F401
    _extract_command_result,
    _extract_command_text,
    _format_shell_body,
    _format_shell_message,
    _looks_binary,
    _prepare_shell_response,
    _render_command_output,
)
from .state import TelegramTopicRecord
from .thread_utils import (  # noqa: F401
    _clear_pending_compact_seed,
    _coerce_id,
    _consume_raw_token,
    _extract_first_bold_span,
    _extract_thread_id,
    _extract_thread_info,
    _extract_thread_path,
    _extract_turn_thread_id,
    _find_thread_entry,
    _format_conversation_id,
    _format_persist_note,
    _local_workspace_threads,
    _partition_threads,
    _path_within,
    _paths_compatible,
    _repo_root,
    _resume_thread_list_limit,
    _set_pending_compact_seed,
    _set_thread_summary,
    _split_topic_key,
    _with_conversation_id,
)
from .time_utils import (  # noqa: F401
    _approval_age_seconds,
    _coerce_datetime,
    _format_friendly_time,
    _format_future_time,
    _parse_iso_timestamp,
)


@dataclass(frozen=True)
class CodexFeatureRow:
    key: str
    stage: str
    enabled: bool


def derive_codex_features_command(app_server_command: Sequence[str]) -> list[str]:
    base = list(app_server_command or [])
    if base and base[-1] == "app-server":
        base = base[:-1]
    if not base:
        base = ["codex"]
    return [*base, "features", "list"]


def parse_codex_features_list(stdout: str) -> list[CodexFeatureRow]:
    rows: list[CodexFeatureRow] = []
    if not isinstance(stdout, str):
        return rows
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        key, stage, enabled_raw = parts
        key = key.strip()
        stage = stage.strip()
        enabled_raw = enabled_raw.strip().lower()
        if not key or not stage:
            continue
        if enabled_raw in ("true", "1", "yes", "y", "on"):
            enabled = True
        elif enabled_raw in ("false", "0", "no", "n", "off"):
            enabled = False
        else:
            continue
        rows.append(CodexFeatureRow(key=key, stage=stage, enabled=enabled))
    return rows


def format_codex_features(
    rows: Sequence[CodexFeatureRow], *, stage_filter: Optional[str]
) -> str:
    filtered = [
        row
        for row in rows
        if stage_filter is None or row.stage.lower() == stage_filter.lower()
    ]
    if not filtered:
        label = (
            "feature flags" if stage_filter is None else f"{stage_filter} feature flags"
        )
        return f"No {label} found."
    header = (
        "Codex feature flags (all):"
        if stage_filter is None
        else f"Codex feature flags ({stage_filter}):"
    )
    lines = [header]
    for row in sorted(filtered, key=lambda r: r.key):
        lines.append(f"- {row.key}: {row.enabled}")
    lines.append("")
    lines.append("Usage:")
    lines.append("/experimental enable <flag>")
    lines.append("/experimental disable <flag>")
    if stage_filter is not None:
        lines.append("/experimental all")
    return "\n".join(lines)


def _clear_policy_overrides(record: "TelegramTopicRecord") -> None:
    record.approval_policy = None
    record.sandbox_policy = None


def _set_policy_overrides(
    record: "TelegramTopicRecord",
    *,
    approval_policy: Optional[str] = None,
    sandbox_policy: Optional[Any] = None,
) -> None:
    if approval_policy is not None:
        record.approval_policy = approval_policy
    if sandbox_policy is not None:
        record.sandbox_policy = sandbox_policy


def _set_model_overrides(
    record: "TelegramTopicRecord",
    model: Optional[str],
    *,
    effort: Optional[str] = None,
    clear_effort: bool = False,
) -> None:
    record.model = model
    if effort is not None:
        record.effort = effort
    elif clear_effort:
        record.effort = None


def _set_rollout_path(record: "TelegramTopicRecord", rollout_path: str) -> None:
    record.rollout_path = rollout_path


def _format_sandbox_policy(sandbox_policy: Any) -> str:
    return format_sandbox_policy(sandbox_policy)


def _format_token_usage(token_usage: Optional[dict[str, Any]]) -> list[str]:
    if not token_usage:
        return []
    lines: list[str] = []
    total = token_usage.get("total") if isinstance(token_usage, dict) else None
    last = token_usage.get("last") if isinstance(token_usage, dict) else None
    if isinstance(total, dict):
        total_line = _format_token_row("Token usage (total)", total)
        if total_line:
            lines.append(total_line)
    if isinstance(last, dict):
        last_line = _format_token_row("Token usage (last)", last)
        if last_line:
            lines.append(last_line)
    context = (
        token_usage.get("modelContextWindow") if isinstance(token_usage, dict) else None
    )
    if isinstance(context, int):
        lines.append(f"Context window: {context}")
    return lines


def _extract_rate_limits(payload: Any) -> Optional[dict[str, Any]]:
    return extract_rate_limits(payload)


def _format_rate_limit_refresh(rate_limits: dict[str, Any]) -> Optional[str]:
    refresh_dt = _extract_rate_limit_timestamp(rate_limits)
    if refresh_dt is None:
        return None
    return _format_friendly_time(refresh_dt.astimezone())


def _extract_rate_limit_timestamp(rate_limits: dict[str, Any]) -> Optional[datetime]:
    candidates: list[tuple[int, datetime]] = []
    for section in ("primary", "secondary"):
        entry = rate_limits.get(section)
        if not isinstance(entry, dict):
            continue
        window_minutes = _rate_limit_window_minutes(entry, section) or 0
        for key in (
            "resets_at",
            "resetsAt",
            "reset_at",
            "resetAt",
            "refresh_at",
            "refreshAt",
            "updated_at",
            "updatedAt",
        ):
            if key in entry:
                dt = _coerce_datetime(entry.get(key))
                if dt is not None:
                    candidates.append((window_minutes, dt))
    if candidates:
        return max(candidates, key=lambda item: (item[0], item[1]))[1]
    for key in (
        "refreshed_at",
        "refreshedAt",
        "refresh_at",
        "refreshAt",
        "updated_at",
        "updatedAt",
        "timestamp",
        "time",
        "as_of",
        "asOf",
    ):
        if key in rate_limits:
            return _coerce_datetime(rate_limits.get(key))
    return None


def _format_token_row(label: str, usage: dict[str, Any]) -> Optional[str]:
    total_tokens = usage.get("totalTokens")
    input_tokens = usage.get("inputTokens")
    cached_input_tokens = usage.get("cachedInputTokens")
    output_tokens = usage.get("outputTokens")
    reasoning_tokens = usage.get("reasoningTokens")
    if reasoning_tokens is None:
        reasoning_tokens = usage.get("reasoningOutputTokens")
    parts: list[str] = []
    if isinstance(total_tokens, int):
        parts.append(f"total={total_tokens}")
    if isinstance(input_tokens, int):
        parts.append(f"in={input_tokens}")
    if isinstance(cached_input_tokens, int):
        parts.append(f"cached={cached_input_tokens}")
    if isinstance(output_tokens, int):
        parts.append(f"out={output_tokens}")
    if isinstance(reasoning_tokens, int):
        parts.append(f"reasoning={reasoning_tokens}")
    if not parts:
        return None
    return f"{label}: " + " ".join(parts)


def _format_help_text(command_specs: dict[str, CommandSpec]) -> str:
    return build_telegram_help_text(command_specs.keys())


def _should_trace_message(text: str) -> bool:
    if not text:
        return False
    if "(conversation " in text:
        return False
    lowered = text.lower()
    return any(token in lowered for token in TRACE_MESSAGE_TOKENS)


def _compose_agent_response(
    final_message: Optional[str] = None,
    *,
    messages: Optional[list[str]] = None,
    errors: Optional[list[str]] = None,
    status: Optional[str] = None,
) -> str:
    if isinstance(final_message, str) and final_message.strip():
        return final_message.strip()
    cleaned = [
        msg.strip() for msg in (messages or []) if isinstance(msg, str) and msg.strip()
    ]
    if not cleaned:
        cleaned_errors = [
            err.strip()
            for err in (errors or [])
            if isinstance(err, str) and err.strip()
        ]
        if cleaned_errors:
            if len(cleaned_errors) == 1:
                lines = [f"Error: {cleaned_errors[0]}"]
            else:
                lines = ["Errors:"]
                lines.extend(f"- {err}" for err in cleaned_errors)
            if status and status != "completed":
                lines.append(f"Status: {status}")
            return "\n".join(lines)
        if status and status != "completed":
            return f"No agent message produced (status: {status}). Check logs."
        return "No agent message produced. Check logs."
    return "\n\n".join(cleaned)


def _compose_interrupt_response(agent_text: str) -> str:
    base = "Interrupted."
    if agent_text and not _is_no_agent_response(agent_text):
        return f"{base}\n\n{agent_text}"
    return base


def is_interrupt_status(status: Optional[str]) -> bool:
    if not status:
        return False
    normalized = status.strip().lower()
    return normalized in {"interrupted", "cancelled", "canceled", "aborted"}


def _format_approval_prompt(message: dict[str, Any]) -> str:
    method = message.get("method")
    params_raw = message.get("params")
    params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}
    if isinstance(method, str) and method.startswith("opencode/permission"):
        prompt = params.get("prompt")
        if isinstance(prompt, str) and prompt:
            return prompt
    lines = ["Approval required"]
    reason = params.get("reason")
    if isinstance(reason, str) and reason:
        lines.append(f"Reason: {reason}")
    if method == "item/commandExecution/requestApproval":
        command = params.get("command")
        if command:
            lines.append(f"Command: {command}")
    elif method == "item/fileChange/requestApproval":
        files = _extract_files(params)
        if files:
            if len(files) == 1:
                lines.append(f"File: {files[0]}")
            else:
                lines.append("Files:")
                lines.extend([f"- {path}" for path in files[:10]])
                if len(files) > 10:
                    lines.append("- ...")
    return "\n".join(lines)


def _format_approval_decision(decision: str) -> str:
    return f"Approval {decision}."


def _extract_files(params: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for key in ("files", "fileChanges", "paths"):
        payload = params.get(key)
        if isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, str) and entry:
                    files.append(entry)
                elif isinstance(entry, dict):
                    path = entry.get("path") or entry.get("file") or entry.get("name")
                    if isinstance(path, str) and path:
                        files.append(path)
    return files


def _page_count(total: int, page_size: int) -> int:
    if total <= 0:
        return 0
    return (total + page_size - 1) // page_size


def _page_slice(
    items: Sequence[tuple[str, str]],
    page: int,
    page_size: int,
) -> list[tuple[str, str]]:
    start = page * page_size
    end = start + page_size
    return list(items[start:end])


def _selection_contains(items: Sequence[tuple[str, str]], value: str) -> bool:
    return any(item_id == value for item_id, _ in items)


def _format_selection_prompt(base: str, page: int, total_pages: int) -> str:
    if total_pages <= 1:
        return base
    trimmed = base.rstrip(".")
    return f"{trimmed} (page {page + 1}/{total_pages})."


def format_public_error(detail: str, *, limit: int = 200) -> str:
    """Format error detail for public Telegram messages with redaction and truncation.

    This helper ensures all user-visible error text sent via Telegram is:
    - Short and readable
    - Redacted for known secret patterns
    - Does not include raw file contents or stack traces

    Args:
        detail: Error detail string to format.
        limit: Maximum length of output (default 200).

    Returns:
        Formatted error string with secrets redacted and length limited.
    """
    normalized = " ".join(detail.split())
    redacted = redact_text(normalized)
    if len(redacted) > limit:
        return f"{redacted[: limit - 3]}..."
    return redacted
