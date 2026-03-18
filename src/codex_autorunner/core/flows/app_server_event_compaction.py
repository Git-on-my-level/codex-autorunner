from __future__ import annotations

import json
from typing import Any, Optional

from .models import FlowEventType

_COMPACT_APP_SERVER_METHODS = frozenset(
    {"message.part.updated", "message.updated", "session.diff"}
)
_APP_SERVER_PREVIEW_CHARS = 2048
_APP_SERVER_PREVIEW_MARKER = " ... "
_COMPACT_FILE_LIMIT = 10


def normalize_persisted_event_data(
    event_type: FlowEventType,
    data: Optional[dict[str, Any]],
) -> dict[str, Any]:
    normalized = dict(data or {})
    if event_type == FlowEventType.APP_SERVER_EVENT:
        return _compact_app_server_event_data(normalized)
    return normalized


def _coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_non_empty_str(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _truncate_text(value: Any, limit: int = _APP_SERVER_PREVIEW_CHARS) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if len(value) <= limit:
        return value
    if limit <= len(_APP_SERVER_PREVIEW_MARKER):
        return value[:limit]
    keep = limit - len(_APP_SERVER_PREVIEW_MARKER)
    head = (keep + 1) // 2
    tail = keep // 2
    suffix = value[-tail:] if tail else ""
    return f"{value[:head]}{_APP_SERVER_PREVIEW_MARKER}{suffix}"


def _first_non_empty_str(*values: Any) -> Optional[str]:
    for value in values:
        text = _coerce_non_empty_str(value)
        if text is not None:
            return text
    return None


def _scalar_summary(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _pick_summary_fields(
    source: dict[str, Any], keys: tuple[str, ...]
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in keys:
        if key not in source:
            continue
        value = _scalar_summary(source.get(key))
        if value is not None:
            summary[key] = value
    return summary


def _compact_error_summary(value: Any) -> Optional[dict[str, Any]]:
    err = _coerce_dict(value)
    if not err:
        return None
    summary = _pick_summary_fields(
        err, ("message", "error", "additionalDetails", "details")
    )
    return summary or None


def _compact_part_state_summary(value: Any) -> Optional[dict[str, Any]]:
    state = _coerce_dict(value)
    if not state:
        return None
    summary = _pick_summary_fields(state, ("status", "exitCode", "exit_code", "reason"))
    error_summary = _compact_error_summary(state.get("error"))
    if error_summary:
        summary["error"] = error_summary
    return summary or None


def _compact_delta_summary(value: Any) -> Optional[Any]:
    if isinstance(value, str):
        return _truncate_text(value)
    delta = _coerce_dict(value)
    if not delta:
        return None
    text = _truncate_text(delta.get("text"))
    if text is None:
        return None
    return {"text": text}


def _extract_delta_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return _truncate_text(value)
    delta = _coerce_dict(value)
    if not delta:
        return None
    return _truncate_text(delta.get("text"))


def _compact_tool_input_container(value: Any) -> Optional[dict[str, str]]:
    container = _coerce_dict(value)
    if not container:
        return None
    for key in ("command", "cmd", "script", "input"):
        text = _truncate_text(container.get(key))
        if text:
            return {key: text}
    return None


def _compact_file_entries(value: Any) -> Optional[list[dict[str, Any] | str]]:
    entries = value if isinstance(value, list) else []
    if not entries:
        return None
    compact_entries: list[dict[str, Any] | str] = []
    for entry in entries[:_COMPACT_FILE_LIMIT]:
        if isinstance(entry, str):
            compact_entries.append(_truncate_text(entry) or entry)
            continue
        if isinstance(entry, dict):
            compact_entry = _pick_summary_fields(
                entry, ("path", "file", "name", "status")
            )
            if compact_entry:
                compact_entries.append(compact_entry)
    return compact_entries or None


def _collect_file_labels(value: Any, limit: int = _COMPACT_FILE_LIMIT) -> list[str]:
    entries = value if isinstance(value, list) else []
    labels: list[str] = []
    for entry in entries:
        if len(labels) >= limit:
            break
        if isinstance(entry, str):
            text = _truncate_text(entry)
            if text:
                labels.append(text)
            continue
        if isinstance(entry, dict):
            text = _first_non_empty_str(
                entry.get("path"), entry.get("file"), entry.get("name")
            )
            if text:
                labels.append(_truncate_text(text) or text)
    return labels


def _file_change_preview(value: Any) -> Optional[str]:
    entries = value if isinstance(value, list) else []
    if not entries:
        return None
    labels = _collect_file_labels(entries)
    if labels:
        remaining = max(0, len(entries) - len(labels))
        summary = ", ".join(labels)
        if remaining:
            suffix = " file" if remaining == 1 else " files"
            summary = f"{summary} +{remaining} more{suffix}"
        return _truncate_text(summary) or summary
    count = len(entries)
    noun = "file change" if count == 1 else "file changes"
    return f"{count} {noun}"


def _compact_part_summary(
    part: dict[str, Any], preview: Optional[str]
) -> dict[str, Any]:
    summary = _pick_summary_fields(
        part,
        (
            "id",
            "sessionID",
            "sessionId",
            "messageID",
            "messageId",
            "message_id",
            "type",
            "tool",
            "name",
            "callID",
            "path",
            "file",
            "hash",
            "reason",
            "snapshot",
        ),
    )
    for key in ("input", "command", "cmd", "script"):
        text = _truncate_text(part.get(key))
        if text:
            summary[key] = text
            break
    for key in ("args", "arguments", "params"):
        compact_container = _compact_tool_input_container(part.get(key))
        if compact_container:
            summary[key] = compact_container
            break
    if preview and str(summary.get("type") or "").strip().lower() in {
        "",
        "text",
        "reasoning",
    }:
        summary["text"] = preview
    state_summary = _compact_part_state_summary(part.get("state"))
    if state_summary:
        summary["state"] = state_summary
    compact_files = _compact_file_entries(part.get("files"))
    if compact_files:
        summary["files"] = compact_files
    return summary


def _compact_info_summary(info: dict[str, Any]) -> dict[str, Any]:
    return _pick_summary_fields(
        info,
        (
            "id",
            "sessionID",
            "sessionId",
            "role",
            "finish",
            "agent",
            "providerID",
            "modelID",
            "mode",
        ),
    )


def _extract_preview_from_message(method: str, params: dict[str, Any]) -> Optional[str]:
    properties = _coerce_dict(params.get("properties"))
    part = _coerce_dict(properties.get("part")) or _coerce_dict(params.get("part"))
    if method == "message.part.updated":
        part_type = str(part.get("type") or "").strip().lower()
        if part_type in {"", "text", "reasoning"}:
            return (
                _truncate_text(part.get("text"))
                or _extract_delta_text(params.get("delta"))
                or _extract_delta_text(params.get("text"))
                or _extract_delta_text(params.get("output"))
                or _extract_delta_text(properties.get("delta"))
            )
        tool_preview = _first_non_empty_str(
            part.get("command"),
            part.get("input"),
            part.get("cmd"),
            part.get("script"),
        )
        if tool_preview:
            return _truncate_text(tool_preview)
        args = (
            _coerce_dict(part.get("args"))
            or _coerce_dict(part.get("arguments"))
            or _coerce_dict(part.get("params"))
        )
        return _truncate_text(
            args.get("command")
            or args.get("input")
            or args.get("cmd")
            or args.get("script")
        )
    if method == "message.updated":
        info = _coerce_dict(properties.get("info"))
        summary = _coerce_dict(info.get("summary"))
        return _truncate_text(
            summary.get("title") or params.get("message") or params.get("status")
        )
    if method == "session.diff":
        diff = properties.get("diff")
        if isinstance(diff, list):
            preview = _file_change_preview(diff)
            if preview:
                return preview
            count = len(diff)
            noun = "file change" if count == 1 else "file changes"
            return f"{count} {noun}"
        return _truncate_text(params.get("message") or params.get("status"))
    return None


def _build_compact_app_server_params(
    method: str,
    params: dict[str, Any],
    preview: Optional[str],
) -> dict[str, Any]:
    summary = _pick_summary_fields(
        params,
        (
            "turn_id",
            "turnId",
            "itemId",
            "status",
            "message",
            "role",
        ),
    )
    properties = _coerce_dict(params.get("properties"))
    info = _coerce_dict(properties.get("info"))
    part = _coerce_dict(properties.get("part")) or _coerce_dict(params.get("part"))
    properties_summary: dict[str, Any] = {}
    info_summary = _compact_info_summary(info)
    if info_summary:
        properties_summary["info"] = info_summary
    if part:
        part_summary = _compact_part_summary(part, preview)
        if part_summary:
            properties_summary["part"] = part_summary
    session_id = _first_non_empty_str(
        properties.get("sessionID"),
        properties.get("sessionId"),
    )
    if session_id:
        properties_summary["sessionID"] = session_id
    if method == "session.diff":
        diff = properties.get("diff")
        if isinstance(diff, list):
            properties_summary["diff_count"] = len(diff)
            compact_files = _compact_file_entries(diff)
            if compact_files:
                properties_summary["files"] = compact_files
        if preview:
            summary["message"] = preview
    if method == "message.part.updated":
        delta_summary = _compact_delta_summary(
            params.get("delta")
            or params.get("text")
            or params.get("output")
            or properties.get("delta")
        )
        if delta_summary is not None:
            properties_summary["delta"] = delta_summary
    if properties_summary:
        summary["properties"] = properties_summary
    error_summary = _compact_error_summary(params.get("error"))
    if error_summary:
        summary["error"] = error_summary
    return summary


def _compact_app_server_event_data(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("truncated") is True:
        return dict(data)

    message = _coerce_dict(data.get("message"))
    method = _coerce_non_empty_str(message.get("method"))
    if method is None or method not in _COMPACT_APP_SERVER_METHODS:
        return dict(data)

    params = _coerce_dict(message.get("params"))
    properties = _coerce_dict(params.get("properties"))
    info = _coerce_dict(properties.get("info"))
    part = _coerce_dict(properties.get("part")) or _coerce_dict(params.get("part"))
    preview = _extract_preview_from_message(method, params)
    raw_payload = json.dumps(data, ensure_ascii=False)
    compact_data: dict[str, Any] = {
        "method": method,
        "message": {
            "method": method,
            "params": _build_compact_app_server_params(method, params, preview),
        },
        "payload_bytes": len(raw_payload.encode("utf-8")),
        "truncated": True,
    }
    for key in ("id", "received_at", "receivedAt"):
        value = _scalar_summary(data.get(key))
        if value is not None:
            compact_data[key] = value
    turn_id = _first_non_empty_str(
        data.get("turn_id"),
        params.get("turn_id"),
        params.get("turnId"),
    )
    if turn_id:
        compact_data["turn_id"] = turn_id
    thread_id = _first_non_empty_str(
        data.get("thread_id"),
        data.get("threadId"),
        params.get("thread_id"),
        params.get("threadId"),
        info.get("sessionID"),
        info.get("sessionId"),
        part.get("sessionID"),
        part.get("sessionId"),
        properties.get("sessionID"),
        properties.get("sessionId"),
    )
    if thread_id:
        compact_data["thread_id"] = thread_id
    message_id = _first_non_empty_str(
        data.get("message_id"),
        info.get("id"),
        part.get("messageID"),
        part.get("messageId"),
        part.get("message_id"),
    )
    if message_id:
        compact_data["message_id"] = message_id
    part_id = _first_non_empty_str(data.get("part_id"), part.get("id"))
    if part_id:
        compact_data["part_id"] = part_id
    role = _first_non_empty_str(data.get("role"), info.get("role"), params.get("role"))
    if role:
        compact_data["role"] = role
    tool = _first_non_empty_str(data.get("tool"), part.get("tool"), part.get("name"))
    if tool:
        compact_data["tool"] = tool
    status = _first_non_empty_str(
        data.get("status"),
        params.get("status"),
        _coerce_dict(part.get("state")).get("status"),
        info.get("finish"),
    )
    if status:
        compact_data["status"] = status
    if preview:
        compact_data["preview"] = preview
    return compact_data
