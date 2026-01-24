from __future__ import annotations

from typing import Any, cast

from .redaction import redact_text
from .text_delta_coalescer import TextDeltaCoalescer


def _coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _extract_command(item: Any, params: Any) -> str:
    command = None
    if isinstance(item, dict):
        command = item.get("command")
    if command is None and isinstance(params, dict):
        command = params.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command).strip()
    if isinstance(command, str):
        return command.strip()
    return ""


def _extract_files(payload: Any) -> list[str]:
    files: list[str] = []

    def add_entry(entry: Any) -> None:
        if isinstance(entry, str) and entry.strip():
            files.append(entry.strip())
            return
        if isinstance(entry, dict):
            path = entry.get("path") or entry.get("file") or entry.get("name")
            if isinstance(path, str) and path.strip():
                files.append(path.strip())

    if not isinstance(payload, dict):
        return files
    for key in ("files", "fileChanges", "paths"):
        value = payload.get(key)
        if isinstance(value, list):
            for entry in value:
                add_entry(entry)
    for key in ("path", "file", "name"):
        add_entry(payload.get(key))
    return files


def _extract_error_message(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    err = params.get("error")
    if isinstance(err, dict):
        message = err.get("message") if isinstance(err.get("message"), str) else ""
        details = ""
        if isinstance(err.get("additionalDetails"), str):
            details = err["additionalDetails"]
        elif isinstance(err.get("details"), str):
            details = err["details"]
        if message and details and message != details:
            return f"{message} ({details})"
        return message or details
    if isinstance(err, str):
        return err
    message = params.get("message")
    if isinstance(message, str):
        return message
    return ""


class AppServerEventFormatter:
    def __init__(self, redact_enabled: bool = True) -> None:
        self._redact_enabled = redact_enabled
        self._thinking_items: set[str] = set()
        self._reasoning_coalescers: dict[str, TextDeltaCoalescer] = {}

    def reset(self) -> None:
        self._thinking_items.clear()
        self._reasoning_coalescers.clear()

    def format_event(self, message: Any) -> list[str]:
        if not isinstance(message, dict):
            return []
        method = message.get("method") or ""
        params = _coerce_dict(message.get("params"))
        item = _coerce_dict(params.get("item"))
        item_id = params.get("itemId") or item.get("id") or item.get("itemId")
        lines: list[str] = []

        if method == "item/reasoning/summaryTextDelta":
            delta = params.get("delta")
            if not isinstance(delta, str) or not delta:
                return []
            has_valid_item_id = isinstance(item_id, str) and item_id
            if has_valid_item_id and item_id not in self._thinking_items:
                lines.append("thinking")
                self._thinking_items.add(cast(str, item_id))
            if has_valid_item_id:
                if item_id not in self._reasoning_coalescers:
                    self._reasoning_coalescers[cast(str, item_id)] = (
                        TextDeltaCoalescer()
                    )
                self._reasoning_coalescers[cast(str, item_id)].add(delta)
            else:
                lines.append("thinking")
                for line in delta.splitlines() or [""]:
                    if line:
                        lines.append(f"**{line}**")
                    else:
                        lines.append("")
            return lines

        if method == "item/reasoning/summaryPartAdded":
            if (
                isinstance(item_id, str)
                and item_id
                and item_id in self._reasoning_coalescers
            ):
                coalescer = self._reasoning_coalescers[item_id]
                buffer = coalescer.flush_all()
                for line in buffer:
                    if line:
                        lines.append(f"**{line}**")
                    else:
                        lines.append("")
                self._reasoning_coalescers[item_id].clear()
            return lines

        if method in ("turn/completed", "error"):
            self.reset()

        if method == "item/commandExecution/requestApproval":
            command = _extract_command(item, params)
            lines.append("exec")
            if command:
                lines.append(command)
            return lines

        if method == "item/fileChange/requestApproval":
            files = _extract_files(params) or _extract_files(item)
            if files:
                lines.append("file update")
                lines.extend([f"M {path}" for path in files])
            return lines

        if method == "item/completed":
            item_type = item.get("type")
            if item_type == "commandExecution":
                command = _extract_command(item, params)
                lines.append("exec")
                if command:
                    lines.append(command)
                if item.get("exitCode") is not None:
                    lines.append(f"exit {item.get('exitCode')}")
            elif item_type == "fileChange":
                files = _extract_files(item)
                if files:
                    lines.append("file update")
                    lines.extend([f"M {path}" for path in files])
            elif item_type == "reasoning":
                if (
                    isinstance(item_id, str)
                    and item_id
                    and item_id in self._reasoning_coalescers
                ):
                    coalescer = self._reasoning_coalescers[item_id]
                    buffer = coalescer.flush_all()
                    self._reasoning_coalescers.pop(item_id, None)
                    for line in buffer:
                        if line:
                            lines.append(f"**{line}**")
                        else:
                            lines.append("")
            elif item_type == "tool":
                tool_name = item.get("name") or item.get("tool") or item.get("id")
                if isinstance(tool_name, str) and tool_name:
                    if tool_name == "apply_patch":
                        lines.append("apply_patch(...)")
                    else:
                        lines.append(f"tool: {tool_name}")
            return lines

        if method == "turn/diff/updated":
            diff = (
                params.get("diff")
                or params.get("patch")
                or params.get("content")
                or params.get("value")
            )
            if isinstance(diff, str) and diff:
                diff_text = redact_text(diff) if self._redact_enabled else diff
                lines.extend(diff_text.splitlines())
            return lines

        if method == "error":
            message_text = _extract_error_message(params)
            if message_text:
                lines.append(f"error: {message_text}")
            return lines

        if "outputdelta" in method.lower():
            delta = params.get("delta") or params.get("text") or params.get("output")
            if isinstance(delta, str) and delta:
                delta_text = redact_text(delta) if self._redact_enabled else delta
                lines.extend(delta_text.splitlines())
            return lines

        return lines
