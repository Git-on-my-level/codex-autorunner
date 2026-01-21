from __future__ import annotations

import logging
from typing import Any, Optional

from ...core.text_delta_coalescer import TextDeltaCoalescer


class OpenCodeEventFormatter:
    def __init__(self) -> None:
        self._seen_reasoning_parts: set[str] = set()
        self._reasoning_coalescers: dict[str, TextDeltaCoalescer] = {}
        self._tool_last_status: dict[str, str] = {}
        self._seen_patch_hashes: set[str] = set()
        self._logger = logging.getLogger(__name__)

    def flush_all_reasoning(self) -> list[str]:
        lines: list[str] = []
        for coalescer in self._reasoning_coalescers.values():
            remaining = coalescer.flush_all()
            for line in remaining:
                if line.strip():
                    lines.append(f"**{line.strip()}**")
        self._reasoning_coalescers.clear()
        return lines

    def reset(self) -> None:
        self._seen_reasoning_parts.clear()
        self._reasoning_coalescers.clear()
        self._tool_last_status.clear()
        self._seen_patch_hashes.clear()

    def format_part(
        self, part_type: str, part: dict[str, Any], delta_text: Optional[str]
    ) -> list[str]:
        lines: list[str] = []
        part_id = part.get("id") or part.get("partId")

        if part_type == "reasoning":
            lines.extend(self._format_reasoning_part(part_id, delta_text))

        elif part_type == "tool":
            lines.extend(self._format_tool_part(part))

        elif part_type == "patch":
            lines.extend(self._format_patch_part(part))

        return lines

    def _format_reasoning_part(
        self, part_id: Optional[str], delta_text: Optional[str]
    ) -> list[str]:
        lines: list[str] = []
        key = part_id or "reasoning"

        if delta_text:
            if key not in self._seen_reasoning_parts:
                lines.append("thinking")
                self._seen_reasoning_parts.add(key)

            if key not in self._reasoning_coalescers:
                self._reasoning_coalescers[key] = TextDeltaCoalescer()
            coalescer = self._reasoning_coalescers[key]
            coalescer.add(delta_text)
            complete_lines = coalescer.flush_lines()
            for line in complete_lines:
                if line.strip():
                    lines.append(f"**{line.strip()}**")

            remaining = coalescer.get_buffer()
            if remaining and remaining.strip():
                lines.append(f"**{remaining.strip()}**")
                coalescer.clear()
        return lines

    def _format_tool_part(self, part: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        tool_id = part.get("callID") or part.get("id")
        tool_name = part.get("tool") or part.get("name") or ""

        if not isinstance(tool_name, str) or not tool_name:
            return lines

        state = part.get("state", {})
        if not isinstance(state, dict):
            state = {}

        status = state.get("status")
        if isinstance(status, str) and status:
            key = f"{tool_id}:{tool_name}" if tool_id else tool_name
            last_status = self._tool_last_status.get(key)

            if last_status != status:
                self._tool_last_status[key] = status

                if status in ("running", "pending"):
                    lines.append("exec")
                    lines.append(f"tool: {tool_name}")

                elif status == "completed":
                    if last_status not in ("running", "pending"):
                        lines.append("exec")
                        lines.append(f"tool: {tool_name}")
                    exit_code = state.get("exitCode")
                    if exit_code is not None:
                        lines.append(f"exit {exit_code}")

                elif status in ("error", "failed"):
                    if last_status not in ("running", "pending"):
                        lines.append("exec")
                        lines.append(f"tool: {tool_name}")
                    error = state.get("error")
                    if isinstance(error, (str, dict)):
                        if isinstance(error, dict):
                            error = error.get("message") or error.get("error")
                        if isinstance(error, str) and error:
                            lines.append(f"error: {error}")

        elif tool_id is None:
            lines.append("exec")
            lines.append(f"tool: {tool_name}")

        return lines

    def _format_patch_part(self, part: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        patch_hash = part.get("hash")

        if isinstance(patch_hash, str) and patch_hash:
            if patch_hash in self._seen_patch_hashes:
                return lines
            self._seen_patch_hashes.add(patch_hash)

        files = part.get("files")
        if isinstance(files, list):
            if files:
                lines.append("file update")
                for file_entry in files:
                    if isinstance(file_entry, dict):
                        path = file_entry.get("path") or file_entry.get("file")
                        action = file_entry.get("status") or "M"
                        if isinstance(path, str) and path:
                            lines.append(f"{action} {path}")
                    elif isinstance(file_entry, str):
                        lines.append(f"M {file_entry}")

        elif isinstance(files, str):
            lines.append("file update")
            lines.append(f"M {files}")

        return lines

    def format_usage(self, usage: dict[str, Any]) -> list[str]:
        lines: list[str] = []

        total = usage.get("totalTokens")
        if isinstance(total, int):
            input_tokens = usage.get("inputTokens")
            cached_tokens = usage.get("cachedInputTokens")
            output_tokens = usage.get("outputTokens")
            reasoning_tokens = usage.get("reasoningTokens")

            parts: list[str] = []
            if isinstance(input_tokens, int):
                parts.append(f"input: {input_tokens}")
            if isinstance(cached_tokens, int) and cached_tokens > 0:
                parts.append(f"cached: {cached_tokens}")
            if isinstance(output_tokens, int):
                parts.append(f"output: {output_tokens}")
            if isinstance(reasoning_tokens, int):
                parts.append(f"reasoning: {reasoning_tokens}")

            if parts:
                lines.append(f"tokens used - {', '.join(parts)}")
            else:
                lines.append(f"tokens used: {total}")

            context_window = usage.get("modelContextWindow")
            if isinstance(context_window, int) and context_window > 0:
                lines.append(f"context window: {context_window}")

        return lines

    def format_permission(self, properties: dict[str, Any]) -> list[str]:
        lines: list[str] = []

        reason = properties.get("reason") or properties.get("message")
        if isinstance(reason, str) and reason:
            lines.append(f"permission: {reason}")
        else:
            lines.append("permission requested")

        return lines

    def format_error(self, error: Any) -> list[str]:
        lines: list[str] = []

        message = None
        if isinstance(error, dict):
            message = error.get("message") or error.get("error") or error.get("detail")
        elif isinstance(error, str):
            message = error

        if isinstance(message, str) and message:
            lines.append(f"error: {message}")
        else:
            lines.append("error: session error")

        return lines
