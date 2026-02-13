from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from ...core.redaction import redact_text
from ...core.state_roots import resolve_global_state_root
from ...core.utils import (
    RepoNotFoundError,
    canonicalize_path,
    find_repo_root,
    is_within,
)
from ...integrations.github.service import find_github_links, parse_github_url
from .constants import (
    DEFAULT_MODEL_LIST_LIMIT,
    DEFAULT_PAGE_SIZE,
    DEFAULT_SKILLS_LIST_LIMIT,
    RESUME_PREVIEW_ASSISTANT_LIMIT,
    RESUME_PREVIEW_SCAN_LINES,
    RESUME_PREVIEW_USER_LIMIT,
    REVIEW_COMMIT_BUTTON_LABEL_LIMIT,
    SHELL_OUTPUT_TRUNCATION_SUFFIX,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    THREAD_LIST_PAGE_LIMIT,
    TRACE_MESSAGE_TOKENS,
    VALID_REASONING_EFFORTS,
)
from .handlers.commands_spec import CommandSpec
from .state import TelegramState, TelegramTopicRecord, ThreadSummary, topic_key


@dataclass(frozen=True)
class ModelOption:
    model_id: str
    label: str
    efforts: tuple[str, ...]
    default_effort: Optional[str] = None


@dataclass(frozen=True)
class CodexFeatureRow:
    key: str
    stage: str
    enabled: bool


def derive_codex_features_command(app_server_command: Sequence[str]) -> list[str]:
    """
    Build a Codex CLI invocation for `features list` that mirrors the configured app-server command.

    We strip a trailing \"app-server\" subcommand (plus keep any preceding flags/binary),
    so custom binaries or wrapper scripts stay aligned with the running app server.
    """
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


def _normalize_approval_preset(raw: str) -> Optional[str]:
    cleaned = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    if cleaned in ("readonly", "read-only", "read_only"):
        return "read-only"
    if cleaned in ("fullaccess", "full-access", "full_access", "full"):
        return "full-access"
    if cleaned in ("auto", "agent"):
        return "auto"
    return None


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
    if sandbox_policy is None:
        return "default"
    if isinstance(sandbox_policy, str):
        return sandbox_policy
    if isinstance(sandbox_policy, dict):
        sandbox_type = sandbox_policy.get("type")
        if isinstance(sandbox_type, str):
            suffix = ""
            if "networkAccess" in sandbox_policy:
                suffix = f", network={sandbox_policy.get('networkAccess')}"
            return f"{sandbox_type}{suffix}"
    return str(sandbox_policy)


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
    if not isinstance(payload, dict):
        return None
    for key in ("rateLimits", "rate_limits", "limits"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    if "primary" in payload or "secondary" in payload:
        return payload
    return None


def _format_rate_limits(rate_limits: Optional[dict[str, Any]]) -> list[str]:
    if not isinstance(rate_limits, dict):
        return []
    parts: list[str] = []
    for key in ("primary", "secondary"):
        entry = rate_limits.get(key)
        if not isinstance(entry, dict):
            continue
        used_value = entry.get("used_percent", entry.get("usedPercent"))
        used = _coerce_number(used_value)
        if used is None:
            used = _compute_used_percent(entry)
        used_text = _format_percent(used)
        window_minutes = _rate_limit_window_minutes(entry, key)
        label = _format_rate_limit_window(window_minutes) or key
        if used_text:
            parts.append(f"[{label}: {used_text}]")
    if not parts:
        return []
    refresh_label = _format_rate_limit_refresh(rate_limits)
    if refresh_label:
        parts.append(f"[refresh: {refresh_label}]")
    return [f"Limits: {' '.join(parts)}"]


def _rate_limit_window_minutes(
    entry: dict[str, Any],
    section: Optional[str] = None,
) -> Optional[int]:
    window_minutes = _coerce_int(
        entry.get("window_minutes", entry.get("windowMinutes"))
    )
    if window_minutes is None:
        for candidate in (
            "window",
            "window_mins",
            "windowMins",
            "period_minutes",
            "periodMinutes",
            "duration_minutes",
            "durationMinutes",
        ):
            window_minutes = _coerce_int(entry.get(candidate))
            if window_minutes is not None:
                break
    if window_minutes is None:
        window_seconds = _coerce_int(
            entry.get("window_seconds", entry.get("windowSeconds"))
        )
        if window_seconds is not None:
            window_minutes = max(int(round(window_seconds / 60)), 1)
    if window_minutes is None and section in ("primary", "secondary"):
        window_minutes = 300 if section == "primary" else 10080
    return window_minutes


def _compute_used_percent(entry: dict[str, Any]) -> Optional[float]:
    remaining = _coerce_number(entry.get("remaining"))
    limit = _coerce_number(entry.get("limit"))
    if remaining is None or limit is None or limit <= 0:
        return None
    used = (limit - remaining) / limit * 100
    return max(min(used, 100.0), 0.0)


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


def _coerce_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_int(value: Any) -> Optional[int]:
    number = _coerce_number(value)
    if number is None:
        return None
    return int(number)


def _format_percent(value: Any) -> Optional[str]:
    number = _coerce_number(value)
    if number is None:
        return None
    if number.is_integer():
        return f"{int(number)}%"
    return f"{number:.1f}%"


def _format_rate_limit_window(window_minutes: Optional[int]) -> Optional[str]:
    if not isinstance(window_minutes, int) or window_minutes <= 0:
        return None
    if window_minutes == 300:
        return "5h"
    if window_minutes % 1440 == 0:
        return f"{window_minutes // 1440}d"
    if window_minutes % 60 == 0:
        return f"{window_minutes // 60}h"
    return f"{window_minutes}m"


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


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        dt = _parse_iso_timestamp(value)
        if dt is not None:
            return dt
        try:
            return _coerce_datetime(float(value))
        except Exception:
            return None
    return None


def _format_friendly_time(value: datetime) -> str:
    month = value.strftime("%b")
    day = value.day
    hour = value.strftime("%I").lstrip("0") or "12"
    minute = value.strftime("%M")
    ampm = value.strftime("%p").lower()
    return f"{month} {day}, {hour}:{minute}{ampm}"


def _format_tui_token_usage(token_usage: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(token_usage, dict):
        return None
    usage = _select_context_usage_bucket(token_usage)
    if not isinstance(usage, dict):
        return None
    total_tokens = usage.get("totalTokens")
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    if not isinstance(total_tokens, int):
        return None
    parts = [f"Token usage: total {total_tokens}"]
    if isinstance(input_tokens, int):
        parts.append(f"input {input_tokens}")
    if isinstance(output_tokens, int):
        parts.append(f"output {output_tokens}")
    percent = _extract_context_usage_percent(token_usage)
    if percent is not None:
        parts.append(f"ctx {percent}%")
    return " ".join(parts)


def _select_context_usage_bucket(
    token_usage: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if not isinstance(token_usage, dict):
        return None
    # Prefer total cumulative usage when available; `last` is often per-call.
    total = token_usage.get("total")
    if isinstance(total, dict):
        return total
    last = token_usage.get("last")
    if isinstance(last, dict):
        return last
    return None


def _extract_context_usage_percent(
    token_usage: Optional[dict[str, Any]],
) -> Optional[int]:
    usage = _select_context_usage_bucket(token_usage)
    if not isinstance(usage, dict):
        return None
    total_tokens = usage.get("totalTokens")
    if not isinstance(token_usage, dict):
        return None
    context_window = token_usage.get("modelContextWindow")
    if not isinstance(total_tokens, int) or not isinstance(context_window, int):
        return None
    if context_window <= 0:
        return None
    percent_used = round(total_tokens / context_window * 100)
    percent_remaining = max(0, 100 - percent_used)
    return min(percent_remaining, 100)


def _format_turn_metrics(
    token_usage: Optional[dict[str, Any]],
    elapsed_seconds: Optional[float],
) -> Optional[str]:
    lines: list[str] = []
    if elapsed_seconds is not None:
        lines.append(f"Turn time: {elapsed_seconds:.1f}s")
    token_line = _format_tui_token_usage(token_usage)
    if token_line:
        lines.append(token_line)
    if not lines:
        return None
    return "\n".join(lines)


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_future_time(delay_seconds: float) -> Optional[str]:
    if delay_seconds <= 0:
        return None
    dt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _approval_age_seconds(created_at: Optional[str]) -> Optional[int]:
    dt = _parse_iso_timestamp(created_at)
    if dt is None:
        return None
    return max(int((datetime.now(timezone.utc) - dt).total_seconds()), 0)


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


def _coerce_model_entries(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [entry for entry in result if isinstance(entry, dict)]
    if isinstance(result, dict):
        for key in ("data", "models", "items", "results"):
            value = result.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
    return []


def _normalize_model_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _display_name_is_model_alias(model: str, display_name: Any) -> bool:
    if not isinstance(display_name, str) or not display_name:
        return False
    return _normalize_model_name(display_name) == _normalize_model_name(model)


def _coerce_model_options(
    result: Any, *, include_efforts: bool = True
) -> list[ModelOption]:
    entries = _coerce_model_entries(result)
    options: list[ModelOption] = []
    for entry in entries:
        model = entry.get("model") or entry.get("id")
        if not isinstance(model, str) or not model:
            continue
        display_name = entry.get("displayName")
        label = model
        if (
            isinstance(display_name, str)
            and display_name
            and not _display_name_is_model_alias(model, display_name)
        ):
            label = f"{model} ({display_name})"
        default_effort = None
        efforts: list[str] = []
        if include_efforts:
            default_effort = entry.get("defaultReasoningEffort")
            if not isinstance(default_effort, str):
                default_effort = None
            efforts_raw = entry.get("supportedReasoningEfforts")
            if isinstance(efforts_raw, list):
                for effort in efforts_raw:
                    if isinstance(effort, dict):
                        value = effort.get("reasoningEffort")
                        if isinstance(value, str):
                            efforts.append(value)
                    elif isinstance(effort, str):
                        efforts.append(effort)
            if default_effort and default_effort not in efforts:
                efforts.append(default_effort)
            efforts = [effort for effort in efforts if effort]
            if not efforts:
                efforts = sorted(VALID_REASONING_EFFORTS)
            efforts = list(dict.fromkeys(efforts))
            if default_effort:
                label = f"{label} (default {default_effort})"
        options.append(
            ModelOption(
                model_id=model,
                label=label,
                efforts=tuple(efforts),
                default_effort=default_effort,
            )
        )
    return options


def _format_model_list(
    result: Any,
    *,
    include_efforts: bool = True,
    set_hint: Optional[str] = None,
) -> str:
    entries = _coerce_model_entries(result)
    if not entries:
        return "No models found."
    lines = ["Available models:"]
    for entry in entries[:DEFAULT_MODEL_LIST_LIMIT]:
        model = entry.get("model") or entry.get("id") or "(unknown)"
        display_name = entry.get("displayName")
        label = str(model)
        if (
            isinstance(display_name, str)
            and display_name
            and not _display_name_is_model_alias(label, display_name)
        ):
            label = f"{model} ({display_name})"
        if include_efforts:
            efforts = entry.get("supportedReasoningEfforts")
            effort_values: list[str] = []
            if isinstance(efforts, list):
                for effort in efforts:
                    if isinstance(effort, dict):
                        value = effort.get("reasoningEffort")
                        if isinstance(value, str):
                            effort_values.append(value)
                    elif isinstance(effort, str):
                        effort_values.append(effort)
            if effort_values:
                label = f"{label} [effort: {', '.join(effort_values)}]"
            default_effort = entry.get("defaultReasoningEffort")
            if isinstance(default_effort, str):
                label = f"{label} (default {default_effort})"
        lines.append(label)
    if len(entries) > DEFAULT_MODEL_LIST_LIMIT:
        lines.append(f"...and {len(entries) - DEFAULT_MODEL_LIST_LIMIT} more.")
    if set_hint is None:
        set_hint = "Use /model <id> [effort] to set." if include_efforts else None
    if set_hint:
        lines.append(set_hint)
    return "\n".join(lines)


def _format_feature_flags(result: Any) -> str:
    config = result.get("config") if isinstance(result, dict) else None
    if config is None and isinstance(result, dict):
        config = result
    if not isinstance(config, dict):
        return "No feature flags found."
    features = config.get("features")
    if not isinstance(features, dict) or not features:
        return "No feature flags found."
    lines = ["Feature flags:"]
    for key in sorted(features.keys()):
        value = features.get(key)
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _format_skills_list(result: Any, workspace_path: Optional[str]) -> str:
    entries: list[dict[str, Any]] = []
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            entries = [entry for entry in data if isinstance(entry, dict)]
    elif isinstance(result, list):
        entries = [entry for entry in result if isinstance(entry, dict)]
    skills: list[tuple[str, str]] = []
    for entry in entries:
        cwd = entry.get("cwd")
        if isinstance(workspace_path, str) and isinstance(cwd, str):
            if (
                Path(cwd).expanduser().resolve()
                != Path(workspace_path).expanduser().resolve()
            ):
                continue
        items = entry.get("skills")
        if isinstance(items, list):
            for skill in items:
                if not isinstance(skill, dict):
                    continue
                name = skill.get("name")
                if not isinstance(name, str) or not name:
                    continue
                description = skill.get("shortDescription") or skill.get("description")
                desc_text = (
                    description.strip()
                    if isinstance(description, str) and description
                    else ""
                )
                skills.append((name, desc_text))
    if not skills:
        return "No skills found."
    lines = ["Skills:"]
    for name, desc in skills[:DEFAULT_SKILLS_LIST_LIMIT]:
        if desc:
            lines.append(f"{name} - {desc}")
        else:
            lines.append(name)
    if len(skills) > DEFAULT_SKILLS_LIST_LIMIT:
        lines.append(f"...and {len(skills) - DEFAULT_SKILLS_LIST_LIMIT} more.")
    lines.append("Use $<SkillName> in your next message to invoke a skill.")
    return "\n".join(lines)


def _format_mcp_list(result: Any) -> str:
    entries: list[dict[str, Any]] = []
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            entries = [entry for entry in data if isinstance(entry, dict)]
    elif isinstance(result, list):
        entries = [entry for entry in result if isinstance(entry, dict)]
    if not entries:
        return "No MCP servers found."
    lines = ["MCP servers:"]
    for entry in entries:
        name = entry.get("name") or "(unknown)"
        auth = entry.get("authStatus") or "unknown"
        tools = entry.get("tools")
        tool_names: list[str] = []
        if isinstance(tools, dict):
            tool_names = sorted(tools.keys())
        elif isinstance(tools, list):
            tool_names = [str(item) for item in tools]
        line = f"{name} ({auth})"
        if tool_names:
            line = f"{line} - tools: {', '.join(tool_names)}"
        lines.append(line)
    return "\n".join(lines)


def _format_help_text(command_specs: dict[str, CommandSpec]) -> str:
    order = [
        "bind",
        "new",
        "resume",
        "review",
        "flow",
        "reply",
        "pr",
        "agent",
        "model",
        "approvals",
        "pma",
        "status",
        "diff",
        "mention",
        "skills",
        "mcp",
        "experimental",
        "init",
        "compact",
        "rollout",
        "feedback",
        "logout",
        "interrupt",
        "help",
    ]
    lines = ["Commands:"]
    for name in order:
        spec = command_specs.get(name)
        if spec:
            lines.append(f"/{name} - {spec.description}")
    if "review" in command_specs:
        lines.append("")
        lines.append("Review:")
        lines.append("/review")
        lines.append("/review pr [branch]")
        lines.append("/review commit <sha> (or /review commit to pick)")
        lines.append("/review custom <instructions> (or /review custom to prompt)")
        lines.append("/review detached ...")

    if "flow" in command_specs:
        lines.append("")
        lines.append("Flow:")
        lines.append("/flow")
        lines.append("/flow status [run_id]")
        lines.append("/flow bootstrap [--force-new]")
        lines.append("/flow issue <issue#|url>")
        lines.append("/flow plan <text>")
        lines.append("/flow resume [run_id]")
        lines.append("/flow stop [run_id]")
        lines.append("/flow recover [run_id]")
        lines.append("/flow restart")
        lines.append("/flow archive [run_id] [--force]")
        lines.append("/flow reply <message>")
        if "reply" in command_specs:
            lines.append("/reply <message> (legacy)")

    lines.append("")
    lines.append("Other:")
    lines.append("Note: /resume is supported for the codex and opencode agents.")
    lines.append(
        "!<cmd> - run a bash command in the bound workspace (non-interactive; long-running commands time out)"
    )
    return "\n".join(lines)


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
_THREAD_LIST_CURSOR_KEYS = ("nextCursor", "next_cursor", "next")


def _extract_thread_list_cursor(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in _THREAD_LIST_CURSOR_KEYS:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int)):
            text = str(value).strip()
            if text:
                return text
    return None


def _coerce_thread_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return _normalize_thread_entries(payload)
    if isinstance(payload, dict):
        for key in ("threads", "data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return _normalize_thread_entries(value)
            if isinstance(value, dict):
                return _normalize_thread_mapping(value)
        if any(key in payload for key in ("id", "threadId", "thread_id")):
            return _normalize_thread_entries([payload])
    return []


def _normalize_thread_entries(entries: Iterable[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, dict):
            item = dict(entry)
            if "id" not in item:
                for key in ("threadId", "thread_id"):
                    value = item.get(key)
                    if isinstance(value, str):
                        item["id"] = value
                        break
            normalized.append(item)
        elif isinstance(entry, str):
            normalized.append({"id": entry})
    return normalized


def _normalize_thread_mapping(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for key, value in mapping.items():
        if not isinstance(key, str):
            continue
        item = dict(value) if isinstance(value, dict) else {}
        item.setdefault("id", key)
        normalized.append(item)
    return normalized


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


def _path_within(root: Path, target: Path) -> bool:
    try:
        root = canonicalize_path(root)
        target = canonicalize_path(target)
    except Exception:
        return False
    return is_within(root, target)


def _repo_root(path: Path) -> Optional[Path]:
    try:
        return find_repo_root(path)
    except RepoNotFoundError:
        return None


def _paths_compatible(workspace_root: Path, resumed_root: Path) -> bool:
    if _path_within(workspace_root, resumed_root):
        return True
    if _path_within(resumed_root, workspace_root):
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
    return _path_within(workspace_repo, resumed_root)


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


COMPACT_SEED_PREFIX = "Context from previous thread:"
COMPACT_SEED_SUFFIX = "Continue from this context. Ask for missing info if needed."


def _strip_list_marker(text: str) -> str:
    if text.startswith("- "):
        return text[2:].strip()
    if text.startswith("* "):
        return text[2:].strip()
    return text


def _compact_seed_summary(text: Optional[str]) -> Optional[str]:
    if not isinstance(text, str):
        return None
    prefix_idx = text.find(COMPACT_SEED_PREFIX)
    if prefix_idx < 0:
        return None
    content = text[prefix_idx + len(COMPACT_SEED_PREFIX) :].lstrip()
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


def _tail_text_lines(path: Path, max_lines: int) -> list[str]:
    if max_lines <= 0:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            buffer = b""
            lines: list[bytes] = []
            while position > 0 and len(lines) <= max_lines:
                read_size = min(4096, position)
                position -= read_size
                handle.seek(position)
                buffer = handle.read(read_size) + buffer
                lines = buffer.splitlines()
            return [
                line.decode("utf-8", errors="replace") for line in lines[-max_lines:]
            ]
    except OSError:
        return []


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


def _extract_text_payload(payload: Any) -> Optional[str]:
    if isinstance(payload, str):
        text = payload.strip()
        return text if text else None
    if isinstance(payload, list):
        parts = []
        for item in payload:
            part_text = _extract_text_payload(item)
            if part_text:
                parts.append(part_text)
        if parts:
            return " ".join(parts)
        return None
    if isinstance(payload, dict):
        for key in ("text", "input_text", "output_text", "message", "value", "delta"):
            value = payload.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
        content = payload.get("content")
        if content is not None:
            return _extract_text_payload(content)
    return None


def _iter_role_texts(
    payload: Any,
    *,
    default_role: Optional[str] = None,
    depth: int = 0,
) -> Iterable[tuple[str, str]]:
    if depth > 5:
        return
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_role_texts(
                item, default_role=default_role, depth=depth + 1
            )
        return
    if not isinstance(payload, dict):
        return
    role = payload.get("role") if isinstance(payload.get("role"), str) else None
    type_value = payload.get("type") if isinstance(payload.get("type"), str) else None
    role_hint = role or default_role
    if not role_hint and type_value:
        lowered = type_value.lower()
        if lowered in (
            "user",
            "user_message",
            "input",
            "input_text",
            "prompt",
            "request",
        ):
            role_hint = "user"
        elif lowered in (
            "assistant",
            "assistant_message",
            "output",
            "output_text",
            "response",
        ):
            role_hint = "assistant"
        else:
            tokens = [token for token in re.split(r"[._]+", lowered) if token]
            if any(token in ("user", "input", "prompt", "request") for token in tokens):
                role_hint = "user"
            elif any(
                token in ("assistant", "output", "response", "completion")
                for token in tokens
            ):
                role_hint = "assistant"
    text = _extract_text_payload(payload)
    if role_hint in ("user", "assistant") and text:
        yield role_hint, text
    nested_payload = payload.get("payload")
    if nested_payload is not None:
        yield from _iter_role_texts(
            nested_payload, default_role=role_hint, depth=depth + 1
        )
    for key in ("input", "output", "messages", "items", "events"):
        if key in payload:
            yield from _iter_role_texts(
                payload[key],
                default_role="user" if key == "input" else "assistant",
                depth=depth + 1,
            )
    for key in ("request", "response", "message", "item", "turn", "event", "data"):
        if key in payload:
            next_role = role_hint
            if next_role is None:
                if key == "request":
                    next_role = "user"
                elif key == "response":
                    next_role = "assistant"
            yield from _iter_role_texts(
                payload[key], default_role=next_role, depth=depth + 1
            )


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
                stripped = _strip_dispatch_begin(text)
                if stripped and not _is_ignored_first_user_preview(stripped):
                    return stripped
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
                        stripped = _strip_dispatch_begin(text)
                        if stripped and not _is_ignored_first_user_preview(stripped):
                            return stripped
    return None


def _extract_thread_preview_parts(entry: Any) -> tuple[Optional[str], Optional[str]]:
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
    user_preview = _coerce_preview_field(entry, user_preview_keys)
    user_preview = _sanitize_user_preview(user_preview)
    assistant_preview = _coerce_preview_field(entry, assistant_preview_keys)
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
            user_preview = _sanitize_user_preview(preview.strip())
    if user_preview:
        user_preview = _truncate_text(
            _normalize_preview_text(user_preview), RESUME_PREVIEW_USER_LIMIT
        )
    if assistant_preview:
        assistant_preview = _truncate_text(
            _normalize_preview_text(assistant_preview),
            RESUME_PREVIEW_ASSISTANT_LIMIT,
        )
    return user_preview, assistant_preview


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
    user_preview = _coerce_preview_field(entry, user_preview_keys)
    user_preview = _strip_dispatch_begin(user_preview)
    if _is_ignored_first_user_preview(user_preview):
        user_preview = None
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
    user_preview = _preview_from_text(summary.user_preview, RESUME_PREVIEW_USER_LIMIT)
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


def _parse_review_commit_log(output: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for record in output.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        sha, _sep, subject = record.partition("\x1f")
        if not sha:
            continue
        entries.append((sha, subject.strip()))
    return entries


def _format_review_commit_label(sha: str, subject: str) -> str:
    short_sha = sha[:7]
    if subject:
        label = f"{short_sha} - {subject}"
    else:
        label = short_sha
    return _truncate_text(label, REVIEW_COMMIT_BUTTON_LABEL_LIMIT)


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


def _telegram_lock_path(token: str) -> Path:
    if not isinstance(token, str) or not token:
        raise ValueError("token is required")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return resolve_global_state_root() / "locks" / f"telegram_bot_{digest}.lock"


def _read_lock_payload(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _lock_payload_summary(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    summary: dict[str, Any] = {}
    for key in ("pid", "started_at", "host", "cwd", "config_root"):
        if key in payload:
            summary[key] = payload.get(key)
    return summary


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
