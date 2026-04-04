from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from ...core.text_utils import _truncate_text

COMPACT_MAX_ACTIONS = 10
COMPACT_MAX_TEXT_LENGTH = 80
STATUS_ICONS = {
    "done": "✓",
    "fail": "✗",
    "warn": "⚠",
    "running": "▸",
    "update": "↻",
    "thinking": "🧠",
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


def _normalize_inline_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _normalize_output_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _truncate_tail(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[-limit:]
    return f"...{text[-(limit - 3) :]}"


def _merge_output_text(current: str, incoming: str) -> str:
    if not current:
        return incoming
    if incoming.startswith(current):
        return incoming
    if current.endswith(incoming):
        return current
    max_overlap = min(len(current), len(incoming))
    for overlap in range(max_overlap, 0, -1):
        if current[-overlap:] == incoming[:overlap]:
            return f"{current}{incoming[overlap:]}"
    return f"{current}{incoming}"


def _output_matches_final_message(output_text: str, final_text: str) -> bool:
    output_norm = _normalize_output_text(output_text).strip()
    final_norm = _normalize_output_text(final_text).strip()
    if not output_norm or not final_norm:
        return False
    if output_norm == final_norm:
        return True
    if output_norm.startswith("..."):
        tail = output_norm[3:]
        if tail and final_norm.endswith(tail):
            return True
    return False


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
    context_usage_percent: Optional[int] = None
    finalized: bool = False
    output_buffer: str = ""
    transient_action: Optional[ProgressAction] = None
    last_thinking_trace: Optional[ProgressAction] = None
    last_tool_trace: Optional[ProgressAction] = None

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
        subagent_label: Optional[str] = None,
        normalize_text: bool = True,
    ) -> None:
        normalized = (
            _normalize_inline_text(text)
            if normalize_text
            else _normalize_output_text(text)
        )
        if not normalized.strip():
            return
        if label in {"thinking", "tool", "command"}:
            trace_action = ProgressAction(
                label=label,
                text=normalized,
                status=status,
                item_id=item_id,
                subagent_label=subagent_label,
            )
            self.transient_action = trace_action
            if label == "thinking":
                self.last_thinking_trace = trace_action
            else:
                self.last_tool_trace = trace_action
            self.last_output_index = None
            self.step += 1
            return
        self.clear_transient_action()
        if label != "output":
            self.last_output_index = None
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
        if track_output:
            self.last_output_index = len(self.actions) - 1

    def update_action(self, index: Optional[int], text: str, status: str) -> None:
        if index is None or index < 0 or index >= len(self.actions):
            return
        normalized = _normalize_inline_text(text)
        if not normalized:
            return
        action = self.actions[index]
        action.text = normalized
        action.status = status

    def update_action_raw(self, index: Optional[int], text: str, status: str) -> None:
        if index is None or index < 0 or index >= len(self.actions):
            return
        normalized = _normalize_output_text(text)
        if not normalized.strip():
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

    def clear_transient_action(self) -> None:
        self.transient_action = None

    def end_output_segment(self) -> None:
        self.last_output_index = None

    def latest_output_text(self) -> str:
        for action in reversed(self.actions):
            if action.label == "output" and action.text.strip():
                return action.text
        return ""

    def drop_terminal_output_if_duplicate(self, final_text: str) -> bool:
        if not isinstance(final_text, str) or not final_text.strip():
            return False
        for index in range(len(self.actions) - 1, -1, -1):
            action = self.actions[index]
            if action.label != "output" or not action.text.strip():
                continue
            if not _output_matches_final_message(action.text, final_text):
                continue
            self.actions.pop(index)
            if self.last_output_index is not None:
                if index == self.last_output_index:
                    self.last_output_index = None
                elif index < self.last_output_index:
                    self.last_output_index -= 1
            self.output_buffer = ""
            for prior_index in range(len(self.actions) - 1, -1, -1):
                prior = self.actions[prior_index]
                if prior.label == "output" and prior.text.strip():
                    self.last_output_index = prior_index
                    self.output_buffer = prior.text
                    break
            return True
        return False

    def note_thinking(self, text: str) -> None:
        normalized = _normalize_inline_text(text)
        if not normalized:
            return
        self.add_action("thinking", normalized, "update")

    def note_output(
        self,
        text: str,
        *,
        new_segment: bool = False,
    ) -> None:
        output_piece = _normalize_output_text(text)
        if not output_piece.strip():
            return
        self.clear_transient_action()
        if new_segment:
            self.last_output_index = None
        if self.last_output_index is None:
            self.output_buffer = _truncate_tail(output_piece, self.max_output_chars)
            self.add_action(
                "output",
                self.output_buffer,
                "update",
                track_output=True,
                normalize_text=False,
            )
            return
        current_output = self.actions[self.last_output_index].text
        self.output_buffer = _truncate_tail(
            _merge_output_text(current_output, output_piece),
            self.max_output_chars,
        )
        self.update_action_raw(self.last_output_index, self.output_buffer, "update")

    def note_command(self, text: str) -> None:
        normalized = _normalize_inline_text(text)
        if not normalized:
            return
        self.add_action("command", normalized, "done")

    def note_tool(self, text: str) -> None:
        normalized = _normalize_inline_text(text)
        if not normalized:
            return
        self.add_action("tool", normalized, "done")

    def note_file_change(self, text: str) -> None:
        self.add_action("files", text, "done")

    def note_approval(self, text: str) -> None:
        self.add_action("approval", text, "warn")

    def note_error(self, text: str) -> None:
        self.add_action("error", text, "fail")


def render_progress_text(
    tracker: TurnProgressTracker,
    *,
    max_length: int,
    now: Optional[float] = None,
    render_mode: str = "live",
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

    def _render_output_blocks(blocks: list[str], limit: int) -> str:
        if not blocks:
            return ""
        candidate_blocks = list(blocks)
        rendered = "\n\n".join(candidate_blocks)
        while len(rendered) > limit and len(candidate_blocks) > 1:
            candidate_blocks.pop(0)
            rendered = "\n\n".join(candidate_blocks)
        if len(rendered) <= limit:
            return rendered
        return _truncate_tail(rendered, limit)

    is_final_mode = render_mode == "final"
    if is_final_mode:
        output_blocks = [
            action.text
            for action in tracker.actions
            if action.label == "output" and action.text.strip()
        ]
        if output_blocks:
            return _render_output_blocks(output_blocks, max_length)
        actions = [
            action
            for action in tracker.actions
            if action.label not in {"thinking", "tool", "command"}
        ]
        if tracker.max_actions > 0:
            actions = actions[-tracker.max_actions :]
        else:
            actions = []
    else:
        actions = (
            tracker.actions[-tracker.max_actions :] if tracker.max_actions > 0 else []
        )
        if not any(action.label == "output" for action in actions):
            latest_output_action = next(
                (
                    action
                    for action in reversed(tracker.actions)
                    if action.label == "output" and action.text.strip()
                ),
                None,
            )
            if latest_output_action is not None:
                if tracker.max_actions <= 0:
                    actions = [latest_output_action]
                elif tracker.max_actions == 1:
                    actions = [latest_output_action]
                else:
                    non_output_tail = [
                        action
                        for action in actions
                        if action is not latest_output_action
                    ]
                    actions = [
                        latest_output_action,
                        *non_output_tail[-(tracker.max_actions - 1) :],
                    ]
    if not is_final_mode and tracker.transient_action is not None:
        actions = [*actions, tracker.transient_action]
        if tracker.max_actions > 0 and len(actions) > tracker.max_actions:
            actions = actions[-tracker.max_actions :]
    blocks: list[list[str]] = []
    for action in actions:
        block: list[str]
        if action.label == "thinking" and action.subagent_label:
            block = [
                "---",
                f"🤖 {action.subagent_label} thinking",
                action.text or "...",
                "---",
            ]
            if blocks:
                block.insert(0, "")
        elif action.label == "thinking":
            block = [f"🧠 {action.text}"]
            if blocks:
                block.insert(0, "")
        elif action.label == "output":
            output_lines = action.text.split("\n")
            if not output_lines:
                block = [action.text]
            else:
                block = output_lines
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

    def _truncate_line_for_fallback(line: str, limit: int) -> str:
        return _truncate_text(line, limit)

    def _format_trace_line(action: Optional[ProgressAction]) -> str:
        if action is None or not action.text.strip():
            return ""
        if action.label == "thinking":
            if action.subagent_label:
                return f"🤖 {action.subagent_label} thinking: {action.text}"
            return f"🧠 {action.text}"
        icon = STATUS_ICONS.get(action.status, STATUS_ICONS["running"])
        return f"{icon} {action.label}: {action.text}"

    def _reserved_live_trace_lines() -> list[str]:
        lines: list[str] = []
        thinking_line = _format_trace_line(tracker.last_thinking_trace)
        if thinking_line:
            lines.append(thinking_line)
        tool_line = _format_trace_line(tracker.last_tool_trace)
        if tool_line:
            lines.append(tool_line)
        return lines

    def _select_fallback_line(lines_with_header: list[str]) -> str:
        for line in reversed(lines_with_header[1:]):
            stripped = line.strip()
            if not stripped or stripped == "---":
                continue
            return line
        return lines_with_header[-1] if len(lines_with_header) > 1 else ""

    while len(blocks) > 1 and len("\n".join(_render_lines(blocks))) > max_length:
        blocks.pop(0)
    lines = _render_lines(blocks)
    message = "\n".join(lines)
    if len(message) <= max_length:
        return message
    if len(lines) > 1:
        header = lines[0]
        remaining = max_length - len(header) - 1
        if remaining > 0:
            latest_output_text = tracker.latest_output_text()
            if not is_final_mode and latest_output_text.strip():
                trace_lines = _reserved_live_trace_lines()
                if trace_lines:
                    for trace_limit in (240, 180, 140, 100, 80, 60, 45, 30, 20, 12):
                        trimmed_traces = [
                            _truncate_line_for_fallback(line, trace_limit)
                            for line in trace_lines
                        ]
                        traces_chunk = "\n".join(trimmed_traces)
                        output_budget = remaining - len(traces_chunk) - 1
                        if output_budget <= 0:
                            continue
                        output_lines = latest_output_text.splitlines()
                        focus_line = (
                            output_lines[-1] if output_lines else latest_output_text
                        )
                        output_tail = _truncate_tail(focus_line, output_budget)
                        candidate = f"{header}\n{output_tail}\n{traces_chunk}"
                        if len(candidate) <= max_length:
                            return candidate
            if latest_output_text.strip():
                output_lines = latest_output_text.splitlines()
                focus_line = output_lines[-1] if output_lines else latest_output_text
                return f"{header}\n{_truncate_tail(focus_line, remaining)}"
            focus_line = _select_fallback_line(lines)
            return f"{header}\n{_truncate_line_for_fallback(focus_line, remaining)}"
    return _truncate_text(message, max_length)
