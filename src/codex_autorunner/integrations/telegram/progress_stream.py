from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .constants import COMPACT_MAX_ACTIONS, COMPACT_MAX_TEXT_LENGTH, STATUS_ICONS
from .helpers import _truncate_text


def format_elapsed(seconds: float) -> str:
    total = max(int(seconds), 0)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).strip()


@dataclass
class ProgressAction:
    label: str
    text: str
    status: str
    item_id: Optional[str] = None
    subagent_label: Optional[str] = None


@dataclass
class TurnProgressTracker:
    started_at: float
    agent: str
    model: str
    label: str
    max_actions: int = COMPACT_MAX_ACTIONS
    max_output_chars: int = COMPACT_MAX_TEXT_LENGTH
    actions: list[ProgressAction] = field(default_factory=list)
    step: int = 0
    last_output_index: Optional[int] = None
    last_thinking_index: Optional[int] = None
    context_usage_percent: Optional[int] = None
    finalized: bool = False

    def set_label(self, label: str) -> None:
        if label:
            self.label = label

    def set_context_usage_percent(self, percent: Optional[int]) -> None:
        if percent is None:
            self.context_usage_percent = None
            return
        self.context_usage_percent = min(max(int(percent), 0), 100)

    def add_action(
        self,
        label: str,
        text: str,
        status: str,
        *,
        item_id: Optional[str] = None,
        track_output: bool = False,
        track_thinking: bool = False,
        subagent_label: Optional[str] = None,
    ) -> None:
        normalized = _normalize_text(text)
        if not normalized:
            return
        self.actions.append(
            ProgressAction(
                label=label,
                text=normalized,
                status=status,
                item_id=item_id,
                subagent_label=subagent_label,
            )
        )
        self.step += 1
        if len(self.actions) > 100:
            removed = len(self.actions) - 100
            self.actions = self.actions[-100:]
            if self.last_output_index is not None:
                self.last_output_index -= removed
                if self.last_output_index < 0:
                    self.last_output_index = None
            if self.last_thinking_index is not None:
                self.last_thinking_index -= removed
                if self.last_thinking_index < 0:
                    self.last_thinking_index = None
        if track_output:
            self.last_output_index = len(self.actions) - 1
        if track_thinking:
            self.last_thinking_index = len(self.actions) - 1

    def update_action(self, index: Optional[int], text: str, status: str) -> None:
        if index is None or index < 0 or index >= len(self.actions):
            return
        normalized = _normalize_text(text)
        if not normalized:
            return
        action = self.actions[index]
        action.text = normalized
        action.status = status

    def update_action_by_item_id(
        self,
        item_id: Optional[str],
        text: str,
        status: str,
        *,
        label: Optional[str] = None,
        subagent_label: Optional[str] = None,
    ) -> bool:
        if not item_id:
            return False
        for index, action in enumerate(self.actions):
            if action.item_id == item_id:
                if label:
                    action.label = label
                if subagent_label:
                    action.subagent_label = subagent_label
                self.update_action(index, text, status)
                return True
        return False

    def note_thinking(self, text: str) -> None:
        if self.last_thinking_index is None:
            self.add_action("thinking", text, "update", track_thinking=True)
            return
        self.update_action(self.last_thinking_index, text, "update")

    def note_output(self, text: str) -> None:
        normalized = _truncate_text(_normalize_text(text), self.max_output_chars)
        if not normalized:
            return
        if self.last_output_index is None:
            self.add_action("output", normalized, "update", track_output=True)
            return
        self.update_action(self.last_output_index, normalized, "update")

    def note_command(self, text: str) -> None:
        self.add_action("command", text, "done")
        self.last_output_index = None

    def note_tool(self, text: str) -> None:
        self.add_action("tool", text, "done")
        self.last_output_index = None

    def note_file_change(self, text: str) -> None:
        self.add_action("files", text, "done")

    def note_approval(self, text: str) -> None:
        self.add_action("approval", text, "warn")

    def note_error(self, text: str) -> None:
        self.add_action("error", text, "fail")


def render_progress_text(
    tracker: TurnProgressTracker, *, max_length: int, now: Optional[float] = None
) -> str:
    if now is None:
        now = time.monotonic()
    elapsed = format_elapsed(now - tracker.started_at)
    parts = [tracker.label, f"agent {tracker.agent}", tracker.model, elapsed]
    if tracker.step:
        parts.append(f"step {tracker.step}")
    if tracker.context_usage_percent is not None:
        parts.append(f"ctx {tracker.context_usage_percent}%")
    header = " · ".join(parts)
    thinking_action = None
    if tracker.last_thinking_index is not None:
        if 0 <= tracker.last_thinking_index < len(tracker.actions):
            thinking_action = tracker.actions[tracker.last_thinking_index]
    actions = tracker.actions[-tracker.max_actions :] if tracker.max_actions > 0 else []
    if thinking_action is not None:
        actions = [action for action in actions if action is not thinking_action]
        actions.append(thinking_action)
        if tracker.max_actions <= 0:
            actions = [thinking_action]
        elif len(actions) > tracker.max_actions:
            actions = actions[-tracker.max_actions :]
    blocks: list[list[str]] = []
    for action in actions:
        block: list[str]
        if action is thinking_action:
            block = [f"🧠 {action.text}"]
            if blocks:
                block.insert(0, "")
        elif action.subagent_label and action.label == "thinking":
            block = [
                "---",
                f"🤖 {action.subagent_label} thinking",
                action.text or "...",
                "---",
            ]
            if blocks:
                block.insert(0, "")
        else:
            icon = STATUS_ICONS.get(action.status, STATUS_ICONS["running"])
            block = [f"{icon} {action.label}: {action.text}"]
        blocks.append(block)

    def _render_lines(action_blocks: list[list[str]]) -> list[str]:
        lines: list[str] = [header]
        for block in action_blocks:
            lines.extend(block)
        return lines

    lines = _render_lines(blocks)
    message = "\n".join(lines)
    if len(message) <= max_length:
        return message
    while blocks and len("\n".join(_render_lines(blocks))) > max_length:
        blocks.pop(0)
    lines = _render_lines(blocks)
    message = "\n".join(lines)
    if len(message) <= max_length:
        return message
    if len(lines) > 1:
        header = lines[0]
        remaining = max_length - len(header) - 1
        if remaining > 0:
            return f"{header}\n{_truncate_text(lines[-1], remaining)}"
    return _truncate_text(message, max_length)
