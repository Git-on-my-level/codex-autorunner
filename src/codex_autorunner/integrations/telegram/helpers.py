from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from ...core.injected_context import strip_injected_context_blocks
from ...core.redaction import redact_text
from ...core.utils import (
    RepoNotFoundError,
    canonicalize_path,
    find_repo_root,
    is_within,
)
from ...integrations.chat.compaction import (
    COMPACT_SEED_PREFIX,
    COMPACT_SEED_SUFFIX,
)
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
    _iter_role_texts,
    _tail_text_lines,
)
from ...integrations.chat.turn_metrics import (  # noqa: F401
    _extract_context_usage_percent,
    _format_tui_token_usage,
    _format_turn_metrics,
)
from ...integrations.github.service import find_github_links, parse_github_url
from .constants import (
    DEFAULT_PAGE_SIZE,
    RESUME_PREVIEW_ASSISTANT_LIMIT,
    RESUME_PREVIEW_SCAN_LINES,
    RESUME_PREVIEW_USER_LIMIT,
    SHELL_OUTPUT_TRUNCATION_SUFFIX,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    THREAD_LIST_PAGE_LIMIT,
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
from .rate_limit_utils import (  # noqa: F401
    _coerce_number,
    _compute_used_percent,
    _format_percent,
    _format_rate_limit_window,
    _rate_limit_window_minutes,
)
from .state import TelegramState, TelegramTopicRecord, ThreadSummary, topic_key
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


def _extract_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("threadId", "thread_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    thread = payload.get("thread")
    if isinstance(thread, dict):
        for key in ("id", "threadId", "thread_id"):
            value = thread.get(key)
            if isinstance(value, str):
                return value
    return None


def _extract_thread_info(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else None
    workspace_path = _extract_thread_path(payload)
    if not workspace_path and isinstance(thread, dict):
        workspace_path = _extract_thread_path(thread)
    rollout_path = None
    if isinstance(thread, dict):
        rollout_path = (
            thread.get("path") if isinstance(thread.get("path"), str) else None
        )
    if rollout_path is None and isinstance(payload.get("path"), str):
        rollout_path = payload.get("path")
    agent = None
    if isinstance(payload.get("agent"), str):
        agent = payload.get("agent")
    if (
        agent is None
        and isinstance(thread, dict)
        and isinstance(thread.get("agent"), str)
    ):
        agent = thread.get("agent")
    model = None
    for key in ("model", "modelId"):
        value = payload.get(key)
        if isinstance(value, str):
            model = value
            break
    if model is None and isinstance(thread, dict):
        for key in ("model", "modelId"):
            value = thread.get(key)
            if isinstance(value, str):
                model = value
                break
    effort = payload.get("reasoningEffort") or payload.get("effort")
    if not isinstance(effort, str) and isinstance(thread, dict):
        effort = thread.get("reasoningEffort") or thread.get("effort")
    if not isinstance(effort, str):
        effort = None
    summary = payload.get("summary") or payload.get("summaryMode")
    if not isinstance(summary, str) and isinstance(thread, dict):
        summary = thread.get("summary") or thread.get("summaryMode")
    if not isinstance(summary, str):
        summary = None
    approval_policy = payload.get("approvalPolicy") or payload.get("approval_policy")
    if not isinstance(approval_policy, str) and isinstance(thread, dict):
        approval_policy = thread.get("approvalPolicy") or thread.get("approval_policy")
    if not isinstance(approval_policy, str):
        approval_policy = None
    sandbox_policy = payload.get("sandboxPolicy") or payload.get("sandbox")
    if not isinstance(sandbox_policy, (dict, str)) and isinstance(thread, dict):
        sandbox_policy = thread.get("sandboxPolicy") or thread.get("sandbox")
    if not isinstance(sandbox_policy, (dict, str)):
        sandbox_policy = None
    return {
        "thread_id": _extract_thread_id(payload),
        "workspace_path": workspace_path,
        "rollout_path": rollout_path,
        "agent": agent,
        "model": model,
        "effort": effort,
        "summary": summary,
        "approval_policy": approval_policy,
        "sandbox_policy": sandbox_policy,
    }


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


def _set_thread_summary(
    record: "TelegramTopicRecord",
    thread_id: str,
    *,
    user_preview: Optional[str] = None,
    assistant_preview: Optional[str] = None,
    last_used_at: Optional[str] = None,
    workspace_path: Optional[str] = None,
    rollout_path: Optional[str] = None,
) -> None:
    if not isinstance(thread_id, str) or not thread_id:
        return
    summary = record.thread_summaries.get(thread_id)
    if summary is None:
        summary = ThreadSummary()
    if user_preview is not None:
        summary.user_preview = user_preview
    if assistant_preview is not None:
        summary.assistant_preview = assistant_preview
    if last_used_at is not None:
        summary.last_used_at = last_used_at
    if workspace_path is not None:
        summary.workspace_path = workspace_path
    if rollout_path is not None:
        summary.rollout_path = rollout_path
    record.thread_summaries[thread_id] = summary
    if record.thread_ids:
        keep = set(record.thread_ids)
        for key in list(record.thread_summaries.keys()):
            if key not in keep:
                record.thread_summaries.pop(key, None)


def _set_pending_compact_seed(
    record: "TelegramTopicRecord", seed_text: str, thread_id: Optional[str]
) -> None:
    record.pending_compact_seed = seed_text
    record.pending_compact_seed_thread_id = thread_id


def _clear_pending_compact_seed(record: "TelegramTopicRecord") -> None:
    record.pending_compact_seed = None
    record.pending_compact_seed_thread_id = None


def _format_conversation_id(chat_id: int, thread_id: Optional[int]) -> str:
    return topic_key(chat_id, thread_id)


def _with_conversation_id(
    message: str, *, chat_id: int, thread_id: Optional[int]
) -> str:
    conversation_id = _format_conversation_id(chat_id, thread_id)
    return f"{message} (conversation {conversation_id})"


def _format_persist_note(message: str, *, persist: bool) -> str:
    if not persist:
        return message
    return f"{message} (Persistence is not supported in Telegram; applied to this topic only.)"


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


def _coerce_id(value: Any) -> Optional[str]:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _extract_turn_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for candidate in (payload, payload.get("turn"), payload.get("item")):
        if not isinstance(candidate, dict):
            continue
        for key in ("threadId", "thread_id"):
            thread_id = _coerce_id(candidate.get(key))
            if thread_id:
                return thread_id
        thread = candidate.get("thread")
        if isinstance(thread, dict):
            thread_id = _coerce_id(
                thread.get("id") or thread.get("threadId") or thread.get("thread_id")
            )
            if thread_id:
                return thread_id
    return None


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


def _render_command_output(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        stdout = result.get("stdout") or result.get("stdOut") or result.get("output")
        stderr = result.get("stderr") or result.get("stdErr")
        if isinstance(stdout, str) and isinstance(stderr, str):
            if stdout and stderr:
                return stdout.rstrip("\n") + "\n" + stderr
            if stdout:
                return stdout
            return stderr
        if isinstance(stdout, str):
            return stdout
        if isinstance(stderr, str):
            return stderr
    return ""


def _extract_command_result(result: Any) -> tuple[str, str, Optional[int]]:
    stdout = ""
    stderr = ""
    exit_code = None
    if isinstance(result, str):
        stdout = result
        return stdout, stderr, exit_code
    if isinstance(result, dict):
        stdout_value = (
            result.get("stdout") or result.get("stdOut") or result.get("output")
        )
        stderr_value = result.get("stderr") or result.get("stdErr")
        exit_value = result.get("exitCode") or result.get("exit_code")
        if isinstance(stdout_value, str):
            stdout = stdout_value
        if isinstance(stderr_value, str):
            stderr = stderr_value
        if isinstance(exit_value, int):
            exit_code = exit_value
    return stdout, stderr, exit_code


def _format_shell_body(
    command: str, stdout: str, stderr: str, exit_code: Optional[int]
) -> str:
    lines = [f"$ {command}"]
    if stdout:
        lines.append(stdout.rstrip("\n"))
    if stderr:
        if stdout:
            lines.append("")
        lines.append("[stderr]")
        lines.append(stderr.rstrip("\n"))
    if not stdout and not stderr:
        lines.append("(no output)")
    if exit_code is not None and exit_code != 0:
        lines.append(f"(exit {exit_code})")
    return "\n".join(lines)


def _format_shell_message(body: str, *, note: Optional[str]) -> str:
    if note:
        return f"{note}\n```text\n{body}\n```"
    return f"```text\n{body}\n```"


def _prepare_shell_response(
    full_body: str,
    *,
    max_output_chars: int,
    filename: str,
) -> tuple[str, Optional[bytes]]:
    message = _format_shell_message(full_body, note=None)
    if (
        len(full_body) <= max_output_chars
        and len(message) <= TELEGRAM_MAX_MESSAGE_LENGTH
    ):
        return message, None
    note = f"Output too long; attached full output as {filename}. Showing head."
    limit = max_output_chars
    head = full_body[:limit].rstrip()
    head = f"{head}{SHELL_OUTPUT_TRUNCATION_SUFFIX}"
    message = _format_shell_message(head, note=note)
    if len(message) > TELEGRAM_MAX_MESSAGE_LENGTH:
        excess = len(message) - TELEGRAM_MAX_MESSAGE_LENGTH
        allowed = max(0, limit - excess)
        head = full_body[:allowed].rstrip()
        head = f"{head}{SHELL_OUTPUT_TRUNCATION_SUFFIX}"
        message = _format_shell_message(head, note=note)
    attachment = full_body.encode("utf-8", errors="replace")
    return message, attachment


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data


def _find_thread_entry(payload: Any, thread_id: str) -> Optional[dict[str, Any]]:
    for entry in _coerce_thread_list(payload):
        if entry.get("id") == thread_id:
            return entry
    return None


def _extract_rollout_path(entry: Any) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    for key in ("rollout_path", "rolloutPath", "path"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    thread = entry.get("thread")
    if isinstance(thread, dict):
        value = thread.get("path")
        if isinstance(value, str):
            return value
    return None


_THREAD_PATH_KEYS_PRIMARY = (
    "cwd",
    "workspace_path",
    "workspacePath",
    "repoPath",
    "repo_path",
    "projectRoot",
    "project_root",
)
_THREAD_PATH_CONTAINERS = (
    "workspace",
    "project",
    "repo",
    "metadata",
    "context",
    "config",
)


def _extract_thread_path(entry: dict[str, Any]) -> Optional[str]:
    for key in _THREAD_PATH_KEYS_PRIMARY:
        value = entry.get(key)
        if isinstance(value, str):
            return value
    for container_key in _THREAD_PATH_CONTAINERS:
        nested = entry.get(container_key)
        if isinstance(nested, dict):
            for key in _THREAD_PATH_KEYS_PRIMARY:
                value = nested.get(key)
                if isinstance(value, str):
                    return value
    return None


def _partition_threads(
    threads: Any, workspace_path: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    if not isinstance(threads, list):
        return [], [], False
    workspace = Path(workspace_path).expanduser().resolve()
    filtered: list[dict[str, Any]] = []
    unscoped: list[dict[str, Any]] = []
    saw_path = False
    for entry in threads:
        if not isinstance(entry, dict):
            continue
        cwd = _extract_thread_path(entry)
        if not isinstance(cwd, str):
            unscoped.append(entry)
            continue
        saw_path = True
        try:
            candidate = Path(cwd).expanduser().resolve()
        except Exception:
            continue
        if _paths_compatible(workspace, candidate):
            filtered.append(entry)
    return filtered, unscoped, saw_path


def _local_workspace_threads(
    state: "TelegramState",
    workspace_path: Optional[str],
    *,
    current_key: str,
) -> tuple[list[str], dict[str, str], dict[str, set[str]]]:
    thread_ids: list[str] = []
    previews: dict[str, str] = {}
    topic_keys_by_thread: dict[str, set[str]] = {}
    if not isinstance(workspace_path, str) or not workspace_path.strip():
        return thread_ids, previews, topic_keys_by_thread
    workspace_key = workspace_path.strip()
    workspace_root: Optional[Path] = None
    try:
        workspace_root = Path(workspace_key).expanduser().resolve()
    except Exception:
        workspace_root = None

    def matches(candidate_path: Optional[str]) -> bool:
        if not isinstance(candidate_path, str) or not candidate_path.strip():
            return False
        candidate_path = candidate_path.strip()
        if workspace_root is not None:
            try:
                candidate_root = Path(candidate_path).expanduser().resolve()
            except Exception:
                return False
            return _paths_compatible(workspace_root, candidate_root)
        return candidate_path == workspace_key

    def add_record(key: str, record: "TelegramTopicRecord") -> None:
        if not matches(record.workspace_path):
            return
        for thread_id in record.thread_ids:
            topic_keys_by_thread.setdefault(thread_id, set()).add(key)
            if thread_id not in previews:
                preview = _thread_summary_preview(record, thread_id)
                if preview:
                    previews[thread_id] = preview
            if thread_id in seen:
                continue
            seen.add(thread_id)
            thread_ids.append(thread_id)

    seen: set[str] = set()
    current = state.topics.get(current_key)
    if current is not None:
        add_record(current_key, current)
    for key, record in state.topics.items():
        if key == current_key:
            continue
        add_record(key, record)
    return thread_ids, previews, topic_keys_by_thread


def _path_within(*, root: Path, target: Path) -> bool:
    try:
        root = canonicalize_path(root)
        target = canonicalize_path(target)
    except Exception:
        return False
    return is_within(root=root, target=target)


def _repo_root(path: Path) -> Optional[Path]:
    try:
        return find_repo_root(path)
    except RepoNotFoundError:
        return None


def _paths_compatible(workspace_root: Path, resumed_root: Path) -> bool:
    if _path_within(root=workspace_root, target=resumed_root):
        return True
    if _path_within(root=resumed_root, target=workspace_root):
        workspace_repo = _repo_root(workspace_root)
        resumed_repo = _repo_root(resumed_root)
        if workspace_repo is None or resumed_repo is None:
            return False
        if workspace_repo != resumed_repo:
            return False
        return resumed_root == workspace_repo
    workspace_repo = _repo_root(workspace_root)
    resumed_repo = _repo_root(resumed_root)
    if workspace_repo is None or resumed_repo is None:
        return False
    if workspace_repo != resumed_repo:
        return False
    return _path_within(root=workspace_repo, target=resumed_root)


def _should_trace_message(text: str) -> bool:
    if not text:
        return False
    if "(conversation " in text:
        return False
    lowered = text.lower()
    return any(token in lowered for token in TRACE_MESSAGE_TOKENS)


def _compact_preview(text: Any, limit: int = 40) -> str:
    preview = " ".join(str(text or "").split())
    if len(preview) > limit:
        return preview[: limit - 3] + "..."
    return preview or "(no preview)"


def _coerce_thread_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    thread = payload.get("thread")
    if isinstance(thread, dict):
        merged = dict(thread)
        for key, value in payload.items():
            if key != "thread" and key not in merged:
                merged[key] = value
        return merged
    return dict(payload)


def _normalize_preview_text(text: str) -> str:
    return " ".join(text.split()).strip()


GITHUB_URL_TRAILING_PUNCTUATION = ".,)]}>\"'"


def _strip_url_trailing_punctuation(url: str) -> str:
    return url.rstrip(GITHUB_URL_TRAILING_PUNCTUATION)


FIRST_USER_PREVIEW_IGNORE_PATTERNS = (
    # New-format user instructions injection (AGENTS.md), preferred format.
    re.compile(
        r"(?s)^\s*#\s*AGENTS\.md instructions for .+?\n\n<INSTRUCTIONS>\n.*?\n</INSTRUCTIONS>\s*$",
        re.IGNORECASE,
    ),
    # Legacy user instructions injection.
    re.compile(
        r"(?s)^\s*<user_instructions>\s*.*?\s*</user_instructions>\s*$", re.IGNORECASE
    ),
    # Environment context injection (cwd, approval policy, sandbox policy, etc.).
    re.compile(
        r"(?s)^\s*<environment_context>\s*.*?\s*</environment_context>\s*$",
        re.IGNORECASE,
    ),
    # Skill instructions injection (includes name/path and skill contents).
    re.compile(r"(?s)^\s*<skill>\s*.*?\s*</skill>\s*$", re.IGNORECASE),
    # User shell command records (transcript of !/shell).
    re.compile(
        r"(?s)^\s*<user_shell_command>\s*.*?\s*</user_shell_command>\s*$", re.IGNORECASE
    ),
)

DISPATCH_BEGIN_STRIP_RE = re.compile(
    r"(?s)^\s*(?:<prior context>\s*)?##\s*My request for Codex:\s*",
    re.IGNORECASE,
)


def _is_ignored_first_user_preview(text: Optional[str]) -> bool:
    if not isinstance(text, str):
        return False
    trimmed = text.strip()
    if not trimmed:
        return True
    return any(
        pattern.search(trimmed) for pattern in FIRST_USER_PREVIEW_IGNORE_PATTERNS
    )


def _strip_dispatch_begin(text: Optional[str]) -> Optional[str]:
    if not isinstance(text, str):
        return text
    stripped = DISPATCH_BEGIN_STRIP_RE.sub("", text)
    return stripped if stripped != text else text


def _sanitize_user_preview(text: Optional[str]) -> Optional[str]:
    if not isinstance(text, str):
        return text
    stripped = _strip_dispatch_begin(text)
    stripped = strip_injected_context_blocks(stripped)
    if _is_ignored_first_user_preview(stripped):
        return None
    return stripped


def _github_preview_matcher(text: Optional[str]) -> Optional[str]:
    if not isinstance(text, str) or not text.strip():
        return None
    for link in find_github_links(text):
        cleaned = _strip_url_trailing_punctuation(link)
        parsed = parse_github_url(cleaned)
        if not parsed:
            continue
        slug, kind, number = parsed
        label = f"{slug}#{number}"
        if kind == "pr":
            return f"{label} (PR)"
        return f"{label} (Issue)"
    return None


def _strip_list_marker(text: str) -> str:
    if text.startswith("- "):
        return text[2:].strip()
    if text.startswith("* "):
        return text[2:].strip()
    return text


def _compact_seed_summary(text: Optional[str]) -> Optional[str]:
    if not isinstance(text, str):
        return None
    prefix = None
    for candidate in (COMPACT_SEED_PREFIX, "Context from previous thread:"):
        if candidate in text:
            prefix = candidate
            break
    if prefix is None:
        return None
    prefix_idx = text.find(prefix)
    content = text[prefix_idx + len(prefix) :].lstrip()
    suffix_idx = content.find(COMPACT_SEED_SUFFIX)
    if suffix_idx >= 0:
        content = content[:suffix_idx]
    return content.strip() or None


def _extract_compact_goal(summary: str) -> Optional[str]:
    lines = summary.splitlines()
    expecting_goal_line = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if expecting_goal_line:
            return _strip_list_marker(stripped)
        if lowered.startswith("goals:") or lowered.startswith("goal:"):
            after = stripped.split(":", 1)[1].strip()
            if after:
                return after
            expecting_goal_line = True
    return None


def _compact_seed_preview_matcher(text: Optional[str]) -> Optional[str]:
    summary = _compact_seed_summary(text)
    if not summary:
        return None
    goal = _extract_compact_goal(summary)
    if goal:
        return f"Compacted: {goal}"
    for line in summary.splitlines():
        stripped = line.strip()
        if stripped:
            return f"Compacted: {_strip_list_marker(stripped)}"
    return "Compacted"


SPECIAL_PREVIEW_MATCHERS: tuple[Callable[[Optional[str]], Optional[str]], ...] = (
    _compact_seed_preview_matcher,
    _github_preview_matcher,
)


def _special_preview_from_text(text: Optional[str]) -> Optional[str]:
    for matcher in SPECIAL_PREVIEW_MATCHERS:
        preview = matcher(text)
        if preview:
            return preview
    return None


def _preview_from_text(text: Optional[str], limit: int) -> Optional[str]:
    if not isinstance(text, str):
        return None
    trimmed = text.strip()
    if not trimmed or _is_no_agent_response(trimmed):
        return None
    return _truncate_text(_normalize_preview_text(trimmed), limit)


def _coerce_preview_field(entry: dict[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return None


def _coerce_preview_field_raw(
    entry: dict[str, Any], keys: Sequence[str]
) -> Optional[str]:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _head_text_lines(path: Path, max_lines: int) -> list[str]:
    if max_lines <= 0:
        return []
    try:
        lines: list[str] = []
        with path.open("rb") as handle:
            for _ in range(max_lines):
                line = handle.readline()
                if not line:
                    break
                lines.append(line.decode("utf-8", errors="replace"))
        return lines
    except OSError:
        return []


def _extract_rollout_preview(path: Path) -> tuple[Optional[str], Optional[str]]:
    lines = _tail_text_lines(path, RESUME_PREVIEW_SCAN_LINES)
    if not lines:
        return None, None
    last_user = None
    last_assistant = None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        for role, text in _iter_role_texts(payload):
            if role == "assistant" and last_assistant is None:
                last_assistant = text
            elif role == "user" and last_user is None:
                sanitized = _sanitize_user_preview(text)
                if sanitized:
                    last_user = sanitized
            if last_user and last_assistant:
                return last_user, last_assistant
    return last_user, last_assistant


def _extract_rollout_first_user_preview(path: Path) -> Optional[str]:
    lines = _head_text_lines(path, RESUME_PREVIEW_SCAN_LINES)
    if not lines:
        return None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        for role, text in _iter_role_texts(payload):
            if role == "user" and text:
                sanitized = _sanitize_user_preview(text)
                if sanitized:
                    return sanitized
    return None


def _extract_turns_preview(turns: Any) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(turns, list):
        return None, None
    last_user = None
    last_assistant = None
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        candidates: list[Any] = []
        for key in ("items", "messages", "input", "output"):
            value = turn.get(key)
            if value is not None:
                candidates.append(value)
        if not candidates:
            candidates.append(turn)
        for candidate in candidates:
            if isinstance(candidate, list):
                iterable: Iterable[Any] = reversed(candidate)
            else:
                iterable = (candidate,)
            for item in iterable:
                for role, text in _iter_role_texts(item):
                    if role == "assistant" and last_assistant is None:
                        last_assistant = text
                    elif role == "user" and last_user is None:
                        sanitized = _sanitize_user_preview(text)
                        if sanitized:
                            last_user = sanitized
                    if last_user and last_assistant:
                        return last_user, last_assistant
    return last_user, last_assistant


def _extract_turns_first_user_preview(turns: Any) -> Optional[str]:
    if not isinstance(turns, list):
        return None
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        candidates: list[Any] = []
        for key in ("items", "messages", "input", "output"):
            value = turn.get(key)
            if value is not None:
                candidates.append(value)
        if not candidates:
            candidates.append(turn)
        for candidate in candidates:
            if isinstance(candidate, list):
                iterable: Iterable[Any] = candidate
            else:
                iterable = (candidate,)
            for item in iterable:
                for role, text in _iter_role_texts(item):
                    if role == "user" and text:
                        sanitized = _sanitize_user_preview(text)
                        if sanitized:
                            return sanitized
    return None


def _extract_thread_resume_parts(entry: Any) -> tuple[Optional[str], Optional[str]]:
    entry = _coerce_thread_payload(entry)
    user_preview_keys = (
        "last_user_message",
        "lastUserMessage",
        "last_user",
        "lastUser",
        "last_user_text",
        "lastUserText",
        "user_preview",
        "userPreview",
    )
    assistant_preview_keys = (
        "last_assistant_message",
        "lastAssistantMessage",
        "last_assistant",
        "lastAssistant",
        "last_assistant_text",
        "lastAssistantText",
        "assistant_preview",
        "assistantPreview",
        "last_response",
        "lastResponse",
        "response_preview",
        "responsePreview",
    )
    user_preview = _coerce_preview_field_raw(entry, user_preview_keys)
    user_preview = _sanitize_user_preview(user_preview)
    assistant_preview = _coerce_preview_field_raw(entry, assistant_preview_keys)
    turns = entry.get("turns")
    if turns and (not user_preview or not assistant_preview):
        turn_user, turn_assistant = _extract_turns_preview(turns)
        if not user_preview and turn_user:
            user_preview = turn_user
        if not assistant_preview and turn_assistant:
            assistant_preview = turn_assistant
    rollout_path = _extract_rollout_path(entry)
    if rollout_path and (not user_preview or not assistant_preview):
        path = Path(rollout_path)
        if path.exists():
            rollout_user, rollout_assistant = _extract_rollout_preview(path)
            if not user_preview and rollout_user:
                user_preview = rollout_user
            if not assistant_preview and rollout_assistant:
                assistant_preview = rollout_assistant
    if user_preview is None:
        preview = entry.get("preview")
        if isinstance(preview, str) and preview.strip():
            user_preview = _sanitize_user_preview(preview)
    if assistant_preview and _is_no_agent_response(assistant_preview):
        assistant_preview = None
    return user_preview, assistant_preview


def _extract_first_user_preview(entry: Any) -> Optional[str]:
    entry = _coerce_thread_payload(entry)
    user_preview_keys = (
        "first_user_message",
        "firstUserMessage",
        "first_user",
        "firstUser",
        "initial_user_message",
        "initialUserMessage",
        "initial_user",
        "initialUser",
        "first_message",
        "firstMessage",
        "initial_message",
        "initialMessage",
    )
    user_preview = _sanitize_user_preview(
        _coerce_preview_field(entry, user_preview_keys)
    )
    turns = entry.get("turns")
    if not user_preview and turns:
        user_preview = _extract_turns_first_user_preview(turns)
    rollout_path = _extract_rollout_path(entry)
    if not user_preview and rollout_path:
        path = Path(rollout_path)
        if path.exists():
            user_preview = _extract_rollout_first_user_preview(path)
    special_preview = _special_preview_from_text(user_preview)
    if special_preview:
        return _preview_from_text(special_preview, RESUME_PREVIEW_USER_LIMIT)
    return _preview_from_text(user_preview, RESUME_PREVIEW_USER_LIMIT)


def _format_preview_parts(
    user_preview: Optional[str], assistant_preview: Optional[str]
) -> str:
    if user_preview and assistant_preview:
        return f"User: {user_preview}\nAssistant: {assistant_preview}"
    if user_preview:
        return f"User: {user_preview}"
    if assistant_preview:
        return f"Assistant: {assistant_preview}"
    return "(no preview)"


def _format_thread_preview(entry: Any) -> str:
    user_preview, assistant_preview = _extract_thread_preview_parts(entry)
    return _format_preview_parts(user_preview, assistant_preview)


def _format_resume_summary(
    thread_id: str,
    entry: Any,
    *,
    workspace_path: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> str:
    user_preview, assistant_preview = _extract_thread_resume_parts(entry)
    # Keep raw whitespace for resume summaries; long messages are chunked by the
    # Telegram adapter (send_message_chunks) so we avoid truncation here.
    parts = [f"Resumed thread `{thread_id}`"]
    if workspace_path or model or effort:
        parts.append(f"Directory: {workspace_path or 'unbound'}")
        parts.append(f"Model: {model or 'default'}")
        parts.append(f"Effort: {effort or 'default'}")
    if user_preview:
        parts.extend(["", "User:", user_preview])
    if assistant_preview:
        parts.extend(["", "Assistant:", assistant_preview])
    return "\n".join(parts)


def _format_summary_preview(summary: ThreadSummary) -> str:
    user_preview = _preview_from_text(
        _sanitize_user_preview(summary.user_preview), RESUME_PREVIEW_USER_LIMIT
    )
    assistant_preview = _preview_from_text(
        summary.assistant_preview, RESUME_PREVIEW_ASSISTANT_LIMIT
    )
    return _format_preview_parts(user_preview, assistant_preview)


def _thread_summary_preview(
    record: "TelegramTopicRecord", thread_id: str
) -> Optional[str]:
    summary = record.thread_summaries.get(thread_id)
    if summary is None:
        return None
    preview = _format_summary_preview(summary)
    if preview == "(no preview)":
        return None
    return preview


def _format_missing_thread_label(thread_id: str, preview: Optional[str]) -> str:
    if preview:
        return preview
    prefix = thread_id[:8]
    suffix = "..." if len(thread_id) > 8 else ""
    return f"Thread {prefix}{suffix} (not indexed yet)"


def _resume_thread_list_limit(thread_ids: Sequence[str]) -> int:
    desired = max(DEFAULT_PAGE_SIZE, len(thread_ids) or DEFAULT_PAGE_SIZE)
    return min(THREAD_LIST_PAGE_LIMIT, desired)


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3]}..."


def _consume_raw_token(raw: str) -> tuple[Optional[str], str]:
    stripped = raw.lstrip()
    if not stripped:
        return None, ""
    for idx, ch in enumerate(stripped):
        if ch.isspace():
            return stripped[:idx], stripped[idx:]
    return stripped, ""


def _extract_first_bold_span(text: str) -> Optional[str]:
    if not text:
        return None
    start = text.find("**")
    if start < 0:
        return None
    end = text.find("**", start + 2)
    if end < 0:
        return None
    content = text[start + 2 : end].strip()
    return content or None


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


def _is_no_agent_response(text: str) -> bool:
    stripped = text.strip() if isinstance(text, str) else ""
    if not stripped:
        return True
    if stripped == "(No agent response.)":
        return True
    if stripped.startswith("No agent message produced"):
        return True
    return False


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


def _extract_command_text(item: dict[str, Any], params: dict[str, Any]) -> str:
    command = item.get("command") if isinstance(item, dict) else None
    if command is None and isinstance(params, dict):
        command = params.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command).strip()
    if isinstance(command, str):
        return command.strip()
    return ""


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


def _split_topic_key(key: str) -> tuple[int, Optional[int]]:
    parts = key.split(":", 2)
    chat_raw = parts[0] if parts else ""
    thread_raw = parts[1] if len(parts) > 1 else ""
    chat_id = int(chat_raw)
    thread_id = None
    if thread_raw and thread_raw != "root":
        thread_id = int(thread_raw)
    return chat_id, thread_id


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
