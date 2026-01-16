from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .helpers import _truncate_text

STATUS_ICONS = {
    "running": "▸",
    "update": "↻",
    "done": "✓",
    "fail": "✗",
    "warn": "⚠",
}


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


@dataclass
class TurnProgressTracker:
    started_at: float
    model: str
    label: str
    max_actions: int
    max_output_chars: int
    actions: list[ProgressAction] = field(default_factory=list)
    step: int = 0
    last_output_index: Optional[int] = None
    last_thinking_index: Optional[int] = None
    finalized: bool = False

    def set_label(self, label: str) -> None:
        if label:
            self.label = label

    def add_action(
        self,
        label: str,
        text: str,
        status: str,
        *,
        item_id: Optional[str] = None,
        track_output: bool = False,
        track_thinking: bool = False,
    ) -> None:
        normalized = _normalize_text(text)
        if not normalized:
            return
        self.actions.append(
            ProgressAction(label=label, text=normalized, status=status, item_id=item_id)
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
    parts = [tracker.label, tracker.model, elapsed]
    if tracker.step:
        parts.append(f"step {tracker.step}")
    header = " · ".join(parts)
    actions = tracker.actions[-tracker.max_actions :] if tracker.max_actions > 0 else []
    lines = [header]
    for action in actions:
        icon = STATUS_ICONS.get(action.status, STATUS_ICONS["running"])
        lines.append(f"{icon} {action.label}: {action.text}")
    message = "\n".join(lines)
    if len(message) <= max_length:
        return message
    while len(lines) > 1 and len("\n".join(lines)) > max_length:
        lines.pop(1)
    message = "\n".join(lines)
    if len(message) <= max_length:
        return message
    if len(lines) > 1:
        header = lines[0]
        remaining = max_length - len(header) - 1
        if remaining > 0:
            return f"{header}\n{_truncate_text(lines[-1], remaining)}"
    return _truncate_text(message, max_length)
