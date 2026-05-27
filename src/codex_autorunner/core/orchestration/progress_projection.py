from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_STREAM_MODE_SNAPSHOT,
    ApprovalRequested,
    Completed,
    Failed,
    Interrupted,
    OutputDelta,
    RunEvent,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
    ToolResult,
    UserInputRequested,
)
from ..redaction import redact_text
from ..text_utils import _truncate_text
from .run_notice_visibility import is_internal_run_notice_kind
from .stream_text_merge import merge_assistant_stream_text

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
    merge_strategy: str = "delta"

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
            "merge_strategy": self.merge_strategy,
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


def _is_specific_notice_message(message: str) -> bool:
    """A notice message is title-worthy only when it reads as a phrase.

    Streamed progress arrives as tiny fragments (`1`, `.`, `2`) that must not
    become card titles; a real notice (`entered review mode`) should.
    """

    text = message.strip()
    if not text:
        return False
    return " " in text or len(text) > 24


def _notice_title(kind: Any, message: Any) -> str:
    kind_text = str(kind or "").strip()
    if kind_text == "thinking":
        return "Thinking"
    message_text = _truncate_text(redact_text(str(message or "").strip()), 120)
    if (
        kind_text in {"progress", "notice"}
        and message_text
        and message_text.lower() not in {"progress", "update", "notice"}
        and _is_specific_notice_message(message_text)
    ):
        return message_text
    return kind_text.replace("_", " ").title() or "Update"


def _failed_message_is_interruption(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in ("interrupt", "cancel", "abort", "stopped by user")
    )


def _with_merged_streamed_item(
    previous: ProgressProjectionItem,
    current: ProgressProjectionItem,
) -> ProgressProjectionItem:
    """Fold a streamed continuation (`assistant_update` or `progress` notice).

    Consecutive items sharing a `merge_key` collapse into one card so a turn's
    reasoning/progress stream renders as a few readable entries instead of
    thousands of per-token fragments.
    """

    summary = _merge_streamed_item_summary(previous, current)
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
        merge_strategy=current.merge_strategy,
    )


def _merge_streamed_item_summary(
    previous: ProgressProjectionItem,
    current: ProgressProjectionItem,
) -> str:
    previous_summary = previous.summary or ""
    incoming_summary = current.summary or ""
    if current.merge_strategy == RUN_EVENT_STREAM_MODE_SNAPSHOT:
        return merge_assistant_stream_text(previous_summary, incoming_summary)
    if not previous_summary:
        return incoming_summary
    return f"{previous_summary}{incoming_summary}"


def reduce_progress_event_merged(
    state: ProgressProjectionState,
    event_input: ProgressProjectionInput,
) -> tuple[ProgressProjectionItem | None, bool]:
    """Reduce one event and fold it into ``state.last_item`` when merge_key matches.

    Returns ``(item, merged_into_previous)``. *item* is the projection row callers
    should surface (stable ``item_id`` across streamed thinking snapshots).
    """

    item = reduce_progress_event(state, event_input)
    if item is None or item.hidden:
        return None, False
    if (
        state.last_item is not None
        and item.merge_key is not None
        and state.last_item.merge_key == item.merge_key
    ):
        merged = _with_merged_streamed_item(state.last_item, item)
        state.last_item = merged
        return merged, True
    state.last_item = item
    return item, False


def project_progress_events(
    events: Iterable[ProgressProjectionInput],
) -> list[ProgressProjectionItem]:
    """Reduce ordered run events into replay-safe PMA progress view items."""

    state = ProgressProjectionState()
    items: list[ProgressProjectionItem] = []
    for event in events:
        item, merged = reduce_progress_event_merged(state, event)
        if item is None or item.hidden:
            continue
        if merged and items:
            items[-1] = item
            continue
        items.append(item)
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
        # The final-reply assistant message is rendered separately as a
        # persisted timeline `assistant_message` row. Surfacing its deltas
        # (or the eventual `item/completed` for the final agentMessage) as a
        # second "Thinking" tail card would duplicate the chat bubble. Mark
        # them hidden here at the projection layer so downstream consumers
        # (tail serializer, runtime overlay, journal replay) all agree on
        # what's user-visible, instead of having a wire-method blocklist in
        # the serializer that can't distinguish commentary from final-reply.
        if event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE:
            return ProgressProjectionItem(
                item_id=f"progress:hidden:assistant_message:{event_key}",
                kind="hidden",
                state="hidden",
                title="Hidden progress",
                summary=None,
                event_ids=(event_id,),
                timestamp=timestamp,
                hidden=True,
            )
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
            merge_strategy=str(event.stream_mode or "delta"),
        )

    if isinstance(event, RunNotice):
        if is_internal_run_notice_kind(event.kind):
            return ProgressProjectionItem(
                item_id=f"progress:hidden:{event.kind or 'notice'}:{event_key}",
                kind="hidden",
                state="hidden",
                title="Hidden progress",
                summary=None,
                event_ids=(event_id,),
                timestamp=timestamp,
                hidden=True,
            )
        is_thinking = event.kind == "thinking"
        # Streamed progress/thought chunks arrive as many tiny RunNotices.
        # Give them a merge_key so consecutive fragments fold into one card,
        # the same way assistant_update deltas already do. `thinking` notices
        # carry cumulative snapshots (codex `summaryTextDelta`, opencode
        # `reasoning`), so they merge with snapshot overlap-dedup; `progress`
        # notices are append-only deltas and merge by concatenation.
        is_progress = event.kind in {"progress", "notice"}
        kind = "assistant_update" if is_thinking else "notice"
        title = _notice_title(event.kind, event.message)
        if is_thinking:
            merge_key: Optional[str] = "assistant_update"
            group_id: Optional[str] = f"assistant:{event_key}"
            group_kind: Optional[str] = "assistant_updates"
            merge_strategy = RUN_EVENT_STREAM_MODE_SNAPSHOT
        elif is_progress:
            merge_key = "progress_notice"
            group_id = f"progress:{event_key}"
            group_kind = "progress_updates"
            merge_strategy = "delta"
        else:
            merge_key = None
            group_id = None
            group_kind = None
            merge_strategy = "delta"
        return ProgressProjectionItem(
            item_id=f"progress:{kind}:{event_key}",
            kind=kind,
            state="running",
            title=title,
            summary=_summary(event.message, title),
            event_ids=(event_id,),
            timestamp=timestamp,
            group_id=group_id,
            group_kind=group_kind,
            merge_key=merge_key,
            merge_strategy=merge_strategy,
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

    if isinstance(event, UserInputRequested):
        return ProgressProjectionItem(
            item_id=f"progress:user_input:{event.request_id or event_key}",
            kind="user_input",
            state="waiting",
            title="User input requested",
            summary=_summary(event.description, "User input requested"),
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

    if isinstance(event, Interrupted):
        return ProgressProjectionItem(
            item_id=f"progress:turn_interrupted:{event_key}",
            kind="turn_interrupted",
            state="interrupted",
            title="Interrupted",
            summary=_summary("Turn interrupted", "Turn interrupted"),
            event_ids=(event_id,),
            timestamp=timestamp,
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
    event_ids_tuple: tuple[int, ...]
    if tool_state in {"completed", "failed"}:
        call_id = state.active_tool_call_event_id
        if call_id is not None and call_id != event_id:
            event_ids_tuple = (call_id, event_id)
        else:
            event_ids_tuple = (event_id,)
        state.active_tool_group_id = None
        state.active_tool_name = None
        state.active_tool_call_event_id = None
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
