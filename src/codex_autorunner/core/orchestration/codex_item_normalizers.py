from __future__ import annotations

import json
from typing import Any, Optional

from ..ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
)

_LOG_LINE_METHODS = frozenset(
    {
        "item/commandexecution/outputdelta",
        "item/filechange/outputdelta",
    }
)


def normalize_tool_name(
    params: dict[str, Any],
    *,
    item: Optional[dict[str, Any]] = None,
) -> tuple[str, dict[str, Any]]:
    item_dict = item if isinstance(item, dict) else _coerce_dict(params.get("item"))
    item_type = item_dict.get("type")

    if item_type == "commandExecution":
        command = item_dict.get("command")
        if not command:
            command = params.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command).strip()
        if isinstance(command, str) and command:
            return command, {"command": command}
        return "commandExecution", {}

    if item_type == "fileChange":
        files = item_dict.get("files")
        if isinstance(files, list):
            paths = [str(entry) for entry in files if isinstance(entry, str)]
            if paths:
                return "fileChange", {"files": paths}
        return "fileChange", {}

    if item_type == "tool":
        name = item_dict.get("name") or item_dict.get("tool") or item_dict.get("id")
        if isinstance(name, str) and name:
            return name, {}
        return "tool", {}

    tool_call = _coerce_dict(item_dict.get("toolCall") or item_dict.get("tool_call"))
    name = tool_call.get("name") or params.get("toolName") or params.get("tool_name")
    if isinstance(name, str) and name:
        input_payload = tool_call.get("input")
        if isinstance(input_payload, dict):
            return name, input_payload
        input_payload = params.get("toolInput") or params.get("input")
        if isinstance(input_payload, dict):
            return name, input_payload
        return name, {}
    return "", {}


def extract_agent_message_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text
    content = item.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for entry in content:
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            if entry_type not in (None, "output_text", "text", "message"):
                continue
            candidate = entry.get("text")
            if isinstance(candidate, str) and candidate.strip():
                parts.append(candidate)
        if parts:
            return "".join(parts)
    return ""


def is_commentary_agent_message(item: dict[str, Any]) -> bool:
    return str(item.get("phase") or "").strip().lower() == "commentary"


def output_delta_type_for_method(method: str) -> str:
    normalized = method.strip().lower()
    if normalized in _LOG_LINE_METHODS:
        return RUN_EVENT_DELTA_TYPE_LOG_LINE
    return RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM


def extract_codex_output_delta(params: dict[str, Any]) -> str:
    for key in ("delta", "text", "output"):
        value = params.get(key)
        if isinstance(value, str):
            return value
    return ""


def reasoning_buffer_key(
    params: dict[str, Any],
    *,
    item: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    for key in ("itemId", "item_id", "turnId", "turn_id"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    if isinstance(item, dict):
        for key in ("id", "itemId", "turnId", "turn_id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def runtime_raw_event_key(raw_event: Any) -> str:
    if isinstance(raw_event, (dict, list)):
        return json.dumps(
            raw_event,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return str(raw_event)


def merge_runtime_raw_events(
    streamed_raw_events: list[Any] | tuple[Any, ...],
    result_raw_events: list[Any] | tuple[Any, ...],
) -> list[Any]:
    streamed = list(streamed_raw_events or [])
    result = list(result_raw_events or [])
    if not streamed:
        return result
    if not result:
        return streamed
    streamed_keys = [runtime_raw_event_key(item) for item in streamed]
    result_keys = [runtime_raw_event_key(item) for item in result]
    max_overlap = min(len(streamed_keys), len(result_keys))
    for overlap in range(max_overlap, 0, -1):
        if streamed_keys[-overlap:] == result_keys[:overlap]:
            return streamed + result[overlap:]
    return streamed + result


def extract_codex_usage(params: dict[str, Any]) -> Optional[dict[str, Any]]:
    usage = params.get("usage") or params.get("tokenUsage")
    if isinstance(usage, dict):
        return usage
    return None


def _coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


__all__ = [
    "extract_agent_message_text",
    "extract_codex_output_delta",
    "extract_codex_usage",
    "is_commentary_agent_message",
    "merge_runtime_raw_events",
    "normalize_tool_name",
    "output_delta_type_for_method",
    "reasoning_buffer_key",
    "runtime_raw_event_key",
]
