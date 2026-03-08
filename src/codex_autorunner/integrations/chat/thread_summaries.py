from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ...core.injected_context import strip_injected_context_blocks

RESUME_PREVIEW_ASSISTANT_LIMIT = 80
RESUME_PREVIEW_SCAN_LINES = 50
RESUME_PREVIEW_USER_LIMIT = 80
REVIEW_COMMIT_BUTTON_LABEL_LIMIT = 80


_THREAD_LIST_CURSOR_KEYS = ("nextCursor", "next_cursor", "next")

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


_FIRST_USER_PREVIEW_IGNORE_PATTERNS = (
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

_DISPATCH_BEGIN_STRIP_RE = re.compile(
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
        pattern.search(trimmed) for pattern in _FIRST_USER_PREVIEW_IGNORE_PATTERNS
    )


def _strip_dispatch_begin(text: Optional[str]) -> Optional[str]:
    if not isinstance(text, str):
        return text
    stripped = _DISPATCH_BEGIN_STRIP_RE.sub("", text)
    return stripped if stripped != text else text


def _sanitize_user_preview(text: Optional[str]) -> Optional[str]:
    if not isinstance(text, str):
        return text
    stripped = _strip_dispatch_begin(text)
    stripped = strip_injected_context_blocks(stripped)
    if _is_ignored_first_user_preview(stripped):
        return None
    return stripped


def _normalize_preview_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3]}..."


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


def _is_no_agent_response(text: str) -> bool:
    stripped = text.strip() if isinstance(text, str) else ""
    if not stripped:
        return True
    if stripped == "(No agent response.)":
        return True
    if stripped.startswith("No agent message produced"):
        return True
    return False


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
