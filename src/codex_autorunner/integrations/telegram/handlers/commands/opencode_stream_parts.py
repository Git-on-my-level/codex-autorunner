"""OpenCode stream part handler for Telegram turn progress tracking.

Extracted from execution.py to reduce the inline callback complexity and make
the progress handling pattern reusable across execution paths.

This module is Telegram-owned UX; it does not belong in the shared chat/runtime
layer. It translates OpenCode protocol part events into TurnProgressTracker
mutations and schedules progress edits on the handler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ...helpers import _compact_preview
from ..utils import _build_opencode_token_usage

if TYPE_CHECKING:
    from ...progress_stream import TurnProgressTracker


class OpenCodeStreamPartHandler:
    __slots__ = (
        "_handlers",
        "_turn_key",
        "_thread_id",
        "_turn_id",
        "_workspace_root",
        "_opencode_client",
        "_model_payload",
        "_reasoning_buffers",
        "_watched_session_ids",
        "_subagent_labels",
        "_opencode_context_window",
        "_context_window_resolved",
    )

    def __init__(
        self,
        handlers: Any,
        turn_key: Any,
        thread_id: str,
        turn_id: str,
        workspace_root: Any,
        opencode_client: Any,
        model_payload: Optional[dict[str, Any]],
    ) -> None:
        self._handlers = handlers
        self._turn_key = turn_key
        self._thread_id = thread_id
        self._turn_id = turn_id
        self._workspace_root = workspace_root
        self._opencode_client = opencode_client
        self._model_payload = model_payload
        self._reasoning_buffers: dict[str, str] = {}
        self._watched_session_ids: set[str] = {thread_id}
        self._subagent_labels: dict[str, str] = {}
        self._opencode_context_window: Optional[int] = None
        self._context_window_resolved = False

    @property
    def watched_session_ids(self) -> set[str]:
        return self._watched_session_ids

    def _resolve_session_id(self, part: dict[str, Any]) -> str:
        for key in ("sessionID", "sessionId", "session_id"):
            value = part.get(key)
            if isinstance(value, str) and value:
                return value
        return self._thread_id

    def _resolve_subagent_label(self, session_id: str) -> str:
        label = self._subagent_labels.get(session_id)
        if label is None:
            label = "@subagent"
            self._subagent_labels.setdefault(session_id, label)
        return label

    def _map_tool_status(self, status: Optional[str]) -> str:
        if not isinstance(status, str):
            return "update"
        status_lower = status.lower()
        if status_lower in ("completed", "done", "success"):
            return "done"
        if status_lower in ("error", "failed", "fail"):
            return "fail"
        if status_lower in ("pending", "running"):
            return "running"
        return "update"

    def _handle_reasoning(
        self,
        tracker: TurnProgressTracker,
        part: dict[str, Any],
        delta_text: Optional[str],
        session_id: str,
        is_primary: bool,
    ) -> None:
        part_id = part.get("id") or part.get("partId") or "reasoning"
        buffer_key = f"{session_id}:{part_id}"
        buffer = self._reasoning_buffers.get(buffer_key, "")
        if delta_text:
            buffer = f"{buffer}{delta_text}"
        else:
            raw_text = part.get("text")
            if isinstance(raw_text, str) and raw_text:
                buffer = raw_text
        if not buffer:
            return
        self._reasoning_buffers[buffer_key] = buffer
        preview = _compact_preview(buffer, limit=240)
        if is_primary:
            tracker.note_thinking(preview)
            return
        subagent_label = self._resolve_subagent_label(session_id)
        if not tracker.update_action_by_item_id(
            buffer_key,
            preview,
            "update",
            label="thinking",
            subagent_label=subagent_label,
        ):
            tracker.add_action(
                "thinking",
                preview,
                "update",
                item_id=buffer_key,
                subagent_label=subagent_label,
            )

    def _handle_text(
        self,
        tracker: TurnProgressTracker,
        part: dict[str, Any],
        delta_text: Optional[str],
    ) -> None:
        if delta_text:
            tracker.note_output(delta_text)
            return
        raw_text = part.get("text")
        if isinstance(raw_text, str) and raw_text:
            tracker.note_output(raw_text)

    def _handle_tool_task_subagent(
        self,
        part: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        metadata = state.get("metadata")
        if not isinstance(metadata, dict):
            return
        child_session_id = metadata.get("sessionId") or metadata.get("sessionID")
        if not isinstance(child_session_id, str) or not child_session_id:
            return
        self._watched_session_ids.add(child_session_id)
        input_payload = state.get("input")
        child_label = None
        if isinstance(input_payload, dict):
            child_label = input_payload.get("subagent_type") or input_payload.get(
                "subagentType"
            )
        if isinstance(child_label, str) and child_label.strip():
            child_label = child_label.strip()
            if not child_label.startswith("@"):
                child_label = f"@{child_label}"
            self._subagent_labels.setdefault(child_session_id, child_label)
        else:
            self._subagent_labels.setdefault(child_session_id, "@subagent")

    def _handle_tool_task_label(self, state: dict[str, Any], base_label: str) -> str:
        detail_parts: list[str] = []
        metadata = state.get("metadata")
        title = state.get("title")
        if isinstance(title, str) and title.strip():
            detail_parts.append(title.strip())
        input_payload = state.get("input")
        if isinstance(input_payload, dict):
            description = input_payload.get("description")
            if isinstance(description, str) and description.strip():
                detail_parts.append(description.strip())
        summary = None
        if isinstance(metadata, dict):
            summary = metadata.get("summary")
        if isinstance(summary, str) and summary.strip():
            detail_parts.append(summary.strip())
        if not detail_parts:
            return base_label
        seen: set[str] = set()
        unique_parts = [
            part_text
            for part_text in detail_parts
            if part_text not in seen and not seen.add(part_text)
        ]
        detail_text = " / ".join(unique_parts)
        return f"{base_label} - {_compact_preview(detail_text, limit=160)}"

    def _handle_tool(
        self,
        tracker: TurnProgressTracker,
        part: dict[str, Any],
        session_id: str,
        is_primary: bool,
    ) -> None:
        tool_id = part.get("callID") or part.get("id")
        tool_name = part.get("tool") or part.get("name") or "tool"
        status = None
        state = part.get("state")
        if isinstance(state, dict):
            status = state.get("status")
        label = (
            f"{tool_name} ({status})"
            if isinstance(status, str) and status
            else str(tool_name)
        )
        if (
            is_primary
            and isinstance(tool_name, str)
            and tool_name == "task"
            and isinstance(state, dict)
        ):
            self._handle_tool_task_subagent(part, state)
            label = self._handle_tool_task_label(state, label)
        mapped_status = self._map_tool_status(status)
        scoped_tool_id = (
            f"{session_id}:{tool_id}" if isinstance(tool_id, str) and tool_id else None
        )
        if is_primary:
            if not tracker.update_action_by_item_id(
                scoped_tool_id,
                label,
                mapped_status,
                label="tool",
            ):
                tracker.add_action(
                    "tool",
                    label,
                    mapped_status,
                    item_id=scoped_tool_id,
                )
            return
        subagent_label = self._resolve_subagent_label(session_id)
        if not tracker.update_action_by_item_id(
            scoped_tool_id,
            label,
            mapped_status,
            label=subagent_label,
        ):
            tracker.add_action(
                subagent_label,
                label,
                mapped_status,
                item_id=scoped_tool_id,
            )

    def _handle_patch(
        self,
        tracker: TurnProgressTracker,
        part: dict[str, Any],
        session_id: str,
    ) -> None:
        patch_id = part.get("id") or part.get("hash")
        files = part.get("files")
        scoped_patch_id = (
            f"{session_id}:{patch_id}"
            if isinstance(patch_id, str) and patch_id
            else None
        )
        if isinstance(files, list) and files:
            summary = ", ".join(str(file) for file in files)
            if not tracker.update_action_by_item_id(
                scoped_patch_id, summary, "done", label="files"
            ):
                tracker.add_action(
                    "files",
                    summary,
                    "done",
                    item_id=scoped_patch_id,
                )
            return
        if not tracker.update_action_by_item_id(
            scoped_patch_id, "Patch", "done", label="files"
        ):
            tracker.add_action(
                "files",
                "Patch",
                "done",
                item_id=scoped_patch_id,
            )

    async def _handle_usage(
        self,
        part: dict[str, Any],
        session_id: str,
        is_primary: bool,
    ) -> None:
        token_usage = (
            _build_opencode_token_usage(part) if isinstance(part, dict) else None
        )
        if not token_usage:
            return
        if not is_primary:
            return
        last_usage = token_usage.get("last")
        if isinstance(last_usage, dict):
            token_usage["total"] = dict(last_usage)
        if (
            "modelContextWindow" not in token_usage
            and not self._context_window_resolved
        ):
            self._opencode_context_window = (
                await self._handlers._resolve_opencode_model_context_window(
                    self._opencode_client,
                    self._workspace_root,
                    self._model_payload,
                )
            )
            self._context_window_resolved = True
        if (
            "modelContextWindow" not in token_usage
            and isinstance(self._opencode_context_window, int)
            and self._opencode_context_window > 0
        ):
            token_usage["modelContextWindow"] = self._opencode_context_window
        self._handlers._cache_token_usage(
            token_usage,
            turn_id=self._turn_id,
            thread_id=self._thread_id,
        )
        await self._handlers._note_progress_context_usage(
            token_usage,
            turn_id=self._turn_id,
            thread_id=self._thread_id,
        )

    async def __call__(
        self,
        part_type: str,
        part: dict[str, Any],
        delta_text: Optional[str],
    ) -> None:
        if self._turn_key is None:
            return
        tracker = self._handlers._turn_progress_trackers.get(self._turn_key)
        if tracker is None:
            return
        session_id = self._resolve_session_id(part)
        is_primary = session_id == self._thread_id

        if part_type == "reasoning":
            self._handle_reasoning(tracker, part, delta_text, session_id, is_primary)
        elif part_type == "text":
            self._handle_text(tracker, part, delta_text)
        elif part_type == "tool":
            self._handle_tool(tracker, part, session_id, is_primary)
        elif part_type == "patch":
            self._handle_patch(tracker, part, session_id)
        elif part_type == "agent":
            agent_name = part.get("name") or "agent"
            tracker.add_action("agent", str(agent_name), "done")
        elif part_type == "step-start":
            tracker.add_action("step", "started", "update")
        elif part_type == "step-finish":
            reason = part.get("reason") or "finished"
            tracker.add_action("step", str(reason), "done")
        elif part_type == "usage":
            await self._handle_usage(part, session_id, is_primary)

        await self._handlers._schedule_progress_edit(self._turn_key)
