from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from ...core.injected_context import strip_injected_context_blocks
from ...integrations.chat.compaction import (
    COMPACT_SEED_PREFIX,
    COMPACT_SEED_SUFFIX,
)
from ...integrations.chat.session_messages import build_resumed_thread_lines
from ...integrations.chat.thread_summaries import (
    _extract_raw_first_user_preview,
    _extract_thread_preview_parts,
    _iter_role_texts,
    _tail_text_lines,
)
from ...integrations.github.service import find_github_links, parse_github_url
from .constants import (
    RESUME_PREVIEW_ASSISTANT_LIMIT,
    RESUME_PREVIEW_SCAN_LINES,
    RESUME_PREVIEW_USER_LIMIT,
)
from .state import TelegramTopicRecord
from .state_types import ThreadSummary


def _compact_preview(text: Any, limit: int = 40) -> str:
    preview = " ".join(str(text or "").split())
    if len(preview) > limit:
        return preview[: limit - 3] + "..."
    return preview or "(no preview)"


def _normalize_preview_text(text: str) -> str:
    return " ".join(text.split()).strip()


GITHUB_URL_TRAILING_PUNCTUATION = ".,)]}>\"'"


def _strip_url_trailing_punctuation(url: str) -> str:
    return url.rstrip(GITHUB_URL_TRAILING_PUNCTUATION)


FIRST_USER_PREVIEW_IGNORE_PATTERNS = (
    re.compile(
        r"(?s)^\s*#\s*AGENTS\.md instructions for .+?\n\n<INSTRUCTIONS>\n.*?\n</INSTRUCTIONS>\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?s)^\s*<user_instructions>\s*.*?\s*</user_instructions>\s*$", re.IGNORECASE
    ),
    re.compile(
        r"(?s)^\s*<environment_context>\s*.*?\s*</environment_context>\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"(?s)^\s*<skill>\s*.*?\s*</skill>\s*$", re.IGNORECASE),
    re.compile(
        r"(?s)^\s*<user_shell_command>\s*.*?\s*</user_shell_command>\s*$", re.IGNORECASE
    ),
)

DISPATCH_BEGIN_STRIP_RE = re.compile(
    r"(?s)^\s*(?:<prior context>\s*)?##\s*My request for Codex:\s*",
    re.IGNORECASE,
)
LEADING_HTML_COMMENT_RE = re.compile(r"(?s)^\s*(?:<!--.*?-->\s*)+")


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
    stripped = LEADING_HTML_COMMENT_RE.sub("", stripped)
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


def _is_no_agent_response(text: str) -> bool:
    stripped = text.strip() if isinstance(text, str) else ""
    if not stripped:
        return True
    if stripped == "(No agent response.)":
        return True
    if stripped.startswith("No agent message produced"):
        return True
    return False


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
    user_preview = _extract_raw_first_user_preview(entry)
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
    agent: Optional[str] = None,
) -> str:
    from ..chat.agents import DEFAULT_CHAT_AGENT_MODELS

    user_preview, assistant_preview = _extract_thread_resume_parts(entry)
    return "\n".join(
        build_resumed_thread_lines(
            thread_id=thread_id,
            workspace_path=workspace_path,
            model=(
                model or DEFAULT_CHAT_AGENT_MODELS.get(agent, "default")
                if model is None
                else model
            ),
            effort="default" if effort is None else effort,
            user_preview=user_preview,
            assistant_preview=assistant_preview,
        )
    )


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


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3]}..."
