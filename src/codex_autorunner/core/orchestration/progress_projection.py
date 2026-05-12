from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunEvent,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from ..redaction import redact_text
from ..text_utils import _truncate_text

PROGRESS_PROJECTION_VERSION = "pma_progress_projection.v1"


@dataclass(frozen=True)
class ProgressProjectionInput:
    event_id: int
    timestamp: str
    event: RunEvent


@dataclass(frozen=True)
class ProgressProjectionItem:
    item_id: str
    kind: str
    state: str
    title: str
    summary: Optional[str]
    event_ids: tuple[int, ...]
    timestamp: str
    group_id: Optional[str] = None
    group_kind: Optional[str] = None
    tool_name: Optional[str] = None
    hidden: bool = False
    merge_key: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": PROGRESS_PROJECTION_VERSION,
            "item_id": self.item_id,
            "kind": self.kind,
            "state": self.state,
            "title": self.title,
            "summary": self.summary,
            "event_ids": list(self.event_ids),
            "event_id": self.event_ids[-1] if self.event_ids else None,
            "timestamp": self.timestamp,
            "group_id": self.group_id,
            "group_kind": self.group_kind,
            "tool_name": self.tool_name,
            "hidden": self.hidden,
        }


@dataclass
class ProgressProjectionState:
    tool_group_index: int = 0
    active_tool_group_id: Optional[str] = None
    active_tool_name: Optional[str] = None
    active_tool_call_event_id: Optional[int] = None
    last_item: Optional[ProgressProjectionItem] = None
    tool_group_items: dict[str, tuple[ProgressProjectionItem, ...]] = field(
        default_factory=dict
    )


def _stable_event_key(event_id: int) -> str:
    return f"{max(0, int(event_id)):04d}"


def _tool_name(value: Any) -> str:
    text = str(value or "").strip()
    return _truncate_text(text, 80) if text else "unknown"


def _summary(value: Any, fallback: str) -> str:
    text = str(value or fallback).strip() or fallback
    return _truncate_text(redact_text(text), 220)


def _failed_message_is_interruption(value: str) -> bool:
    lowered = value.lower()
    return "interrupt" in lowered or "cancel" in lowered or "abort" in lowered


def _with_merged_assistant_update(
    previous: ProgressProjectionItem,
    current: ProgressProjectionItem,
) -> ProgressProjectionItem:
    previous_summary = previous.summary or ""
    incoming_summary = current.summary or ""
    if not previous_summary:
        summary = incoming_summary
    elif not incoming_summary or incoming_summary == previous_summary:
        summary = previous_summary
    elif incoming_summary.startswith(previous_summary):
        summary = incoming_summary
    elif previous_summary.endswith(incoming_summary):
        summary = previous_summary
    else:
        summary = f"{previous_summary}{incoming_summary}"
    return ProgressProjectionItem(
        item_id=previous.item_id,
        kind=previous.kind,
        state=current.state,
        title=previous.title,
        summary=summary,
        event_ids=(*previous.event_ids, *current.event_ids),
        timestamp=current.timestamp,
        group_id=previous.group_id,
        group_kind=previous.group_kind,
        tool_name=previous.tool_name,
        hidden=previous.hidden,
        merge_key=previous.merge_key,
    )


def project_progress_events(
    events: Iterable[ProgressProjectionInput],
) -> list[ProgressProjectionItem]:
    """Reduce ordered run events into replay-safe PMA progress view items."""

    state = ProgressProjectionState()
    items: list[ProgressProjectionItem] = []
    for event in events:
        item = reduce_progress_event(state, event)
        if item is None or item.hidden:
            continue
        if (
            items
            and item.merge_key is not None
            and items[-1].merge_key == item.merge_key
        ):
            merged = _with_merged_assistant_update(items[-1], item)
            items[-1] = merged
            state.last_item = merged
            continue
        items.append(item)
        state.last_item = item
    return items


def reduce_progress_event(
    state: ProgressProjectionState,
    event_input: ProgressProjectionInput,
) -> ProgressProjectionItem | None:
    event = event_input.event
    event_id = int(event_input.event_id)
    timestamp = event_input.timestamp
    event_key = _stable_event_key(event_id)

    if isinstance(event, (Started, TokenUsage)):
        return ProgressProjectionItem(
            item_id=f"progress:hidden:{event_key}",
            kind="hidden",
            state="hidden",
            title="Hidden progress",
            summary=None,
            event_ids=(event_id,),
            timestamp=timestamp,
            hidden=True,
        )

    if isinstance(event, OutputDelta):
        content = str(event.content or "")
        normalized = content.strip()
        title = "Thinking"
        summary = content or "Assistant update"
        kind = "assistant_update"
        item_state = "running"
        tool_prefixes = {
            "⏳": "started",
            "✅": "completed",
            "❌": "failed",
        }
        if normalized.startswith("🤔"):
            summary = "Thinking"
        elif normalized[:1] in tool_prefixes:
            tool_state = tool_prefixes[normalized[:1]]
            name = _tool_name(normalized[1:].strip())
            summary = name
            return _tool_item(
                state=state,
                event_id=event_id,
                timestamp=timestamp,
                event_key=event_key,
                tool_name=name,
                tool_state=tool_state,
                summary=summary,
            )
        return ProgressProjectionItem(
            item_id=f"progress:{kind}:{event_key}",
            kind=kind,
            state=item_state,
            title=title,
            summary=_truncate_text(redact_text(summary or "Assistant update"), 220),
            event_ids=(event_id,),
            timestamp=timestamp,
            group_id=f"assistant:{event_key}",
            group_kind="assistant_updates",
            merge_key="assistant_update",
        )

    if isinstance(event, RunNotice):
        kind = "assistant_update" if event.kind == "thinking" else "notice"
        title = (
            "Thinking"
            if event.kind == "thinking"
            else str(event.kind or "Notice").replace("_", " ").title()
        )
        return ProgressProjectionItem(
            item_id=f"progress:{kind}:{event_key}",
            kind=kind,
            state="running",
            title=title,
            summary=_summary(event.message, title),
            event_ids=(event_id,),
            timestamp=timestamp,
            group_id=f"assistant:{event_key}" if kind == "assistant_update" else None,
            group_kind="assistant_updates" if kind == "assistant_update" else None,
            merge_key="assistant_update" if kind == "assistant_update" else None,
        )

    if isinstance(event, ToolCall):
        return _tool_item(
            state=state,
            event_id=event_id,
            timestamp=timestamp,
            event_key=event_key,
            tool_name=_tool_name(event.tool_name),
            tool_state="started",
            summary=f"tool: {_tool_name(event.tool_name)}",
        )

    if isinstance(event, ToolResult):
        return _tool_item(
            state=state,
            event_id=event_id,
            timestamp=timestamp,
            event_key=event_key,
            tool_name=_tool_name(event.tool_name),
            tool_state=(
                "failed" if str(event.status or "").lower() == "error" else "completed"
            ),
            summary=f"tool: {_tool_name(event.tool_name)}",
        )

    if isinstance(event, ApprovalRequested):
        return ProgressProjectionItem(
            item_id=f"progress:approval:{event.request_id or event_key}",
            kind="approval",
            state="waiting",
            title="Approval requested",
            summary=_summary(event.description, "Approval requested"),
            event_ids=(event_id,),
            timestamp=timestamp,
        )

    if isinstance(event, Completed):
        return ProgressProjectionItem(
            item_id=f"progress:turn_completed:{event_key}",
            kind="turn_completed",
            state="completed",
            title="Turn completed",
            summary=_summary(event.final_message, "Turn completed"),
            event_ids=(event_id,),
            timestamp=timestamp,
            hidden=True,
        )

    if isinstance(event, Failed):
        detail = str(event.error_message or "Turn failed")
        interrupted = _failed_message_is_interruption(detail)
        return ProgressProjectionItem(
            item_id=f"progress:{'turn_interrupted' if interrupted else 'turn_failed'}:{event_key}",
            kind="turn_interrupted" if interrupted else "turn_failed",
            state="interrupted" if interrupted else "failed",
            title="Interrupted" if interrupted else "Run failed",
            summary=_summary(
                "Turn interrupted" if interrupted else detail, "Turn failed"
            ),
            event_ids=(event_id,),
            timestamp=timestamp,
        )

    return None


def _tool_item(
    *,
    state: ProgressProjectionState,
    event_id: int,
    timestamp: str,
    event_key: str,
    tool_name: str,
    tool_state: str,
    summary: str,
) -> ProgressProjectionItem:
    if tool_state == "started" or state.active_tool_group_id is None:
        state.tool_group_index += 1
        state.active_tool_group_id = f"tools:{state.tool_group_index:04d}:{tool_name}"
        state.active_tool_name = tool_name
        state.active_tool_call_event_id = event_id
    elif state.active_tool_name != tool_name:
        state.tool_group_index += 1
        state.active_tool_group_id = f"tools:{state.tool_group_index:04d}:{tool_name}"
        state.active_tool_name = tool_name
        state.active_tool_call_event_id = event_id
    group_id = state.active_tool_group_id
    if tool_state in {"completed", "failed"}:
        call_id = state.active_tool_call_event_id
        if call_id is not None and call_id != event_id:
            merged_event_ids = (call_id, event_id)
        else:
            merged_event_ids = (event_id,)
        state.active_tool_group_id = None
        state.active_tool_name = None
        state.active_tool_call_event_id = None
        event_ids_tuple = merged_event_ids
    else:
        event_ids_tuple = (event_id,)
    return ProgressProjectionItem(
        item_id=f"progress:tool:{event_key}:{tool_name}",
        kind="tool",
        state=tool_state,
        title=tool_name,
        summary=_summary(summary, "Tool activity"),
        event_ids=event_ids_tuple,
        timestamp=timestamp,
        group_id=group_id,
        group_kind="tool_group",
        tool_name=tool_name,
    )


__all__ = [
    "PROGRESS_PROJECTION_VERSION",
    "ProgressProjectionInput",
    "ProgressProjectionItem",
    "ProgressProjectionState",
    "project_progress_events",
    "reduce_progress_event",
]
