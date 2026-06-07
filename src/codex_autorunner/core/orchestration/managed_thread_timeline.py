from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
    ApprovalRequested,
    OutputDelta,
    RunEvent,
    RunNotice,
    ToolCall,
    ToolResult,
    UserInputRequested,
)
from ..text_utils import _truncate_text
from .managed_thread_delivery_ledger import SQLiteManagedThreadDeliveryLedger
from .progress_projection import (
    ProgressProjectionInput,
    ProgressProjectionItem,
    ProgressProjectionState,
    reduce_progress_event_merged,
)
from .run_notice_visibility import (
    is_context_compaction_notice_kind,
    is_internal_run_notice_kind,
)
from .turn_timeline import list_turn_timeline, list_turn_timelines

TIMELINE_CONTRACT_VERSION = "managed_thread_timeline.v3"
MAX_MANAGED_THREAD_TIMELINE_LIMIT = 200
DEFAULT_SUPPRESSED_OUTPUT_DELTA_TYPES = frozenset(
    {
        RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
        RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
        RUN_EVENT_DELTA_TYPE_LOG_LINE,
    }
)


@dataclass(frozen=True)
class ManagedThreadTimelineIdentity:
    """Backend-authored reconciliation identity for a PMA timeline item."""

    timeline_item_id: str
    progress_item_ids: tuple[str, ...] = ()
    correlation_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeline_item_id": self.timeline_item_id,
            "progress_item_ids": list(self.progress_item_ids),
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True)
class ManagedThreadTimelineProvenance:
    """Backend-authored event provenance; cursor_event_id is never item identity."""

    source_event_ids: tuple[Any, ...] = ()
    progress_event_ids: tuple[Any, ...] = ()
    cursor_event_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_event_ids": list(self.source_event_ids),
            "progress_event_ids": list(self.progress_event_ids),
            "cursor_event_id": self.cursor_event_id,
        }


@dataclass(frozen=True)
class ManagedThreadTimelineItem:
    item_id: str
    kind: str
    order_key: str
    timestamp: Optional[str]
    managed_thread_id: str
    managed_turn_id: Optional[str] = None
    status: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    identity: ManagedThreadTimelineIdentity = field(
        default_factory=lambda: ManagedThreadTimelineIdentity(timeline_item_id="")
    )
    provenance: ManagedThreadTimelineProvenance = field(
        default_factory=ManagedThreadTimelineProvenance
    )

    def to_dict(self) -> dict[str, Any]:
        if self.identity.timeline_item_id != self.item_id:
            raise ValueError(
                "managed timeline item identity must be backend-authored and "
                "match item_id"
            )
        data: dict[str, Any] = {
            "contract_version": TIMELINE_CONTRACT_VERSION,
            "item_id": self.item_id,
            "kind": self.kind,
            "order_key": self.order_key,
            "timestamp": self.timestamp,
            "managed_thread_id": self.managed_thread_id,
            "managed_turn_id": self.managed_turn_id,
            "status": self.status,
            "identity": self.identity.to_dict(),
            "provenance": self.provenance.to_dict(),
            "payload": dict(self.payload),
        }
        return data


def _dedupe_preserving_order(values: Iterable[Any]) -> list[Any]:
    deduped: list[Any] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _progress_item_ids_from_items(
    progress_items: Iterable[dict[str, Any]],
) -> list[str]:
    ids: list[str] = []
    for item in progress_items:
        item_id = _normalize_optional_text(item.get("item_id"))
        if item_id is not None and item_id not in ids:
            ids.append(item_id)
    return ids


def _progress_event_ids_from_items(
    progress_items: Iterable[dict[str, Any]],
) -> list[Any]:
    event_ids: list[Any] = []
    for item in progress_items:
        raw_event_ids = item.get("event_ids")
        if not isinstance(raw_event_ids, list):
            continue
        event_ids.extend(raw_event_ids)
    return _dedupe_preserving_order(event_ids)


def _progress_item_metadata(
    progress_item: Optional[ProgressProjectionItem],
) -> tuple[list[str], list[Any]]:
    if progress_item is None:
        return [], []
    return [progress_item.item_id], list(progress_item.event_ids)


def _timeline_identity(
    item_id: str,
    *,
    progress_item_ids: Optional[Iterable[str]] = None,
    correlation_id: Optional[Any] = None,
) -> ManagedThreadTimelineIdentity:
    return ManagedThreadTimelineIdentity(
        timeline_item_id=item_id,
        progress_item_ids=tuple(_dedupe_preserving_order(progress_item_ids or [])),
        correlation_id=_normalize_optional_text(correlation_id),
    )


def _timeline_provenance(
    *,
    source_event_ids: Optional[Iterable[Any]] = None,
    progress_event_ids: Optional[Iterable[Any]] = None,
    cursor_event_id: Optional[str] = None,
) -> ManagedThreadTimelineProvenance:
    source_ids = _dedupe_preserving_order(source_event_ids or [])
    progress_ids = _dedupe_preserving_order(
        progress_event_ids if progress_event_ids is not None else source_ids
    )
    return ManagedThreadTimelineProvenance(
        source_event_ids=tuple(source_ids),
        progress_event_ids=tuple(progress_ids),
        cursor_event_id=cursor_event_id,
    )


def _with_contract_metadata(
    item: dict[str, Any],
    *,
    progress_item_ids: list[str],
    source_event_ids: list[Any],
    progress_event_ids: list[Any],
    cursor_event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Attach v2 identity/provenance to live timeline frames.

    Transport/SSE event ids are cursors. They stay in provenance.cursor_event_id
    and are intentionally not copied into identity.timeline_item_id.
    """

    item_id = str(item.get("item_id") or "")
    return {
        "contract_version": TIMELINE_CONTRACT_VERSION,
        **item,
        "identity": {
            "timeline_item_id": item_id,
            "progress_item_ids": list(progress_item_ids),
            "correlation_id": None,
        },
        "provenance": {
            "source_event_ids": list(source_event_ids),
            "progress_event_ids": list(progress_event_ids),
            "cursor_event_id": cursor_event_id,
        },
    }


def _normalize_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _progress_label_text(value: Any) -> str:
    text = _normalize_optional_text(value)
    if text is None:
        return ""
    return " ".join(text.replace("_", " ").replace(".", " ").split())


def _is_internal_run_notice(event: dict[str, Any], progress_item: Any = None) -> bool:
    progress = progress_item if isinstance(progress_item, dict) else {}
    return any(
        is_internal_run_notice_kind(candidate)
        for candidate in (
            event.get("kind"),
            event.get("title"),
            event.get("event_type"),
            event.get("progress_kind"),
            progress.get("kind"),
            progress.get("title"),
            progress.get("progress_kind"),
        )
    )


def _context_compaction_preview(summary: Optional[str], fallback: str) -> str:
    text = _normalize_optional_text(summary)
    if text is None:
        return fallback
    first_line = next(
        (line.strip() for line in text.splitlines() if line.strip()), text
    )
    return _truncate_text(first_line, 240)


def _context_compaction_payload(
    *,
    source: str,
    provider: Optional[str],
    summary: Optional[str],
    preview: Optional[str],
    scope: str,
    started_fresh_session: bool,
    stored_by_car: bool,
    raw_event: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    normalized_summary = _normalize_optional_text(summary)
    normalized_preview = _normalize_optional_text(
        preview
    ) or _context_compaction_preview(
        normalized_summary,
        "No retained summary was exposed.",
    )
    return {
        "source": source,
        "provider": _normalize_optional_text(provider),
        "summary": normalized_summary,
        "preview": normalized_preview,
        "scope": scope,
        "started_fresh_session": started_fresh_session,
        "stored_by_car": stored_by_car,
        "raw_event": raw_event,
    }


def _provider_compaction_payload_from_notice(
    event: dict[str, Any],
) -> Optional[dict[str, Any]]:
    run_notice = event.get("run_notice")
    notice = run_notice if isinstance(run_notice, dict) else {}
    data = event.get("data")
    event_data = data if isinstance(data, dict) else {}
    notice_data = notice.get("data")
    if isinstance(notice_data, dict):
        event_data = {**notice_data, **event_data}
    progress_item = event.get("progress_item")
    progress = progress_item if isinstance(progress_item, dict) else {}
    if not any(
        is_context_compaction_notice_kind(candidate)
        for candidate in (
            event.get("kind"),
            event.get("title"),
            event.get("event_type"),
            event.get("progress_kind"),
            notice.get("kind"),
            progress.get("kind"),
            progress.get("title"),
            event_data.get("kind"),
            event_data.get("event_type"),
        )
    ):
        return None
    summary = (
        _normalize_optional_text(event_data.get("summary"))
        or _normalize_optional_text(event_data.get("retained_context"))
        or _normalize_optional_text(event_data.get("compact_seed"))
        or _normalize_optional_text(progress.get("summary"))
        or _normalize_optional_text(event.get("summary"))
    )
    message = _normalize_optional_text(
        event.get("message")
    ) or _normalize_optional_text(notice.get("message"))
    if (
        summary is None
        and message
        and message.lower()
        not in {
            "context compaction",
            "provider context compaction",
            "runtime context compaction",
        }
    ):
        summary = message
    provider = (
        _normalize_optional_text(event_data.get("provider"))
        or _normalize_optional_text(event_data.get("provider_id"))
        or _normalize_optional_text(event_data.get("runtime"))
        or _normalize_optional_text(event_data.get("agent"))
        or _normalize_optional_text(progress.get("provider"))
    )
    return _context_compaction_payload(
        source="provider",
        provider=provider,
        summary=summary,
        preview=_normalize_optional_text(event_data.get("preview")),
        scope="provider_session",
        started_fresh_session=False,
        stored_by_car=False,
        raw_event=event,
    )


def _progress_display_title(
    *,
    fallback_kind: Any,
    title: Any = None,
    summary: Any = None,
    phase: Any = None,
    event_type: Any = None,
    progress_kind: Any = None,
) -> str:
    title_text = _truncate_text(_progress_label_text(title), 120)
    if title_text and title_text.lower() not in {
        "progress",
        "update",
        "notice",
        "assistant update",
    }:
        return title_text

    if any(
        _progress_label_text(candidate).lower() == "assistant update"
        for candidate in (event_type, progress_kind, fallback_kind)
    ):
        return "Thinking"

    for candidate in (summary, phase):
        candidate_text = _truncate_text(_progress_label_text(candidate), 120)
        if candidate_text and candidate_text.lower() not in {
            "progress",
            "update",
            "notice",
            "assistant update",
        }:
            return candidate_text

    for candidate in (event_type, progress_kind):
        candidate_text = _truncate_text(_progress_label_text(candidate), 120)
        if candidate_text and candidate_text.lower() not in {
            "progress",
            "update",
            "notice",
            "assistant update",
        }:
            return candidate_text

    fallback_text = _truncate_text(_progress_label_text(fallback_kind), 120)
    return fallback_text or "Update"


def _metadata(turn: dict[str, Any]) -> dict[str, Any]:
    value = turn.get("metadata")
    return dict(value) if isinstance(value, dict) else {}


def _turn_timestamp(turn: dict[str, Any]) -> Optional[str]:
    return _normalize_optional_text(
        turn.get("started_at") or turn.get("created_at") or turn.get("finished_at")
    )


def _order_key(timestamp: Optional[str], sequence: int, item_id: str) -> str:
    return f"{sequence:08d}|{timestamp or ''}|{item_id}"


def _event_timestamp(entry: dict[str, Any]) -> Optional[str]:
    event = entry.get("event")
    if isinstance(event, dict):
        timestamp = _normalize_optional_text(event.get("timestamp"))
        if timestamp is not None:
            return timestamp
    return _normalize_optional_text(entry.get("timestamp"))


def _event_payload(entry: dict[str, Any]) -> dict[str, Any]:
    event = entry.get("event")
    return dict(event) if isinstance(event, dict) else {}


def _is_default_timeline_suppressed_output_delta(event: dict[str, Any]) -> bool:
    return (
        str(event.get("delta_type") or "").strip()
        in DEFAULT_SUPPRESSED_OUTPUT_DELTA_TYPES
    )


def _event_index(entry: dict[str, Any], fallback: int) -> int:
    raw = entry.get("event_index")
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return fallback


def _streamed_intermediate_item_id(
    managed_turn_id: str,
    *,
    progress_item: Optional[ProgressProjectionItem],
    event_stable_id: str,
) -> str:
    if progress_item is not None and progress_item.event_ids:
        return f"turn:{managed_turn_id}:intermediate:{progress_item.event_ids[-1]:04d}"
    return f"turn:{managed_turn_id}:intermediate:{event_stable_id}"


def _merge_streamed_intermediate_timeline_item(
    items: list[ManagedThreadTimelineItem],
    *,
    progress_item: Optional[ProgressProjectionItem],
    progress_merged: bool,
    event_index: int,
    timestamp: Optional[str],
    intermediate_kind: str,
    text: str,
    event_type: str,
    event: dict[str, Any],
) -> bool:
    if not progress_merged or not items or items[-1].kind != "intermediate":
        return False
    last = items[-1]
    progress_item_ids, progress_event_ids = _progress_item_metadata(progress_item)
    source_event_ids = list(last.provenance.source_event_ids or ())
    if event_index not in source_event_ids:
        source_event_ids.append(event_index)
    progress_event_ids = list(progress_event_ids or [event_index])
    merged_text = (
        progress_item.summary
        if progress_item is not None and progress_item.summary
        else text
    )
    items[-1] = ManagedThreadTimelineItem(
        item_id=last.item_id,
        kind=last.kind,
        order_key=last.order_key,
        timestamp=timestamp or last.timestamp,
        managed_thread_id=last.managed_thread_id,
        managed_turn_id=last.managed_turn_id,
        status=last.status,
        identity=_timeline_identity(
            last.item_id,
            progress_item_ids=progress_item_ids or last.identity.progress_item_ids,
        ),
        provenance=_timeline_provenance(
            source_event_ids=source_event_ids,
            progress_event_ids=progress_event_ids,
        ),
        payload={
            **dict(last.payload or {}),
            "intermediate_kind": intermediate_kind,
            "text": merged_text,
            "event_type": event_type,
            "event": event,
            "source_event_ids": source_event_ids,
            "source_event_type": event_type,
            "detail_available": True,
            "hidden": bool(progress_item is not None and progress_item.hidden),
            "progress_item": (
                progress_item.to_dict() if progress_item is not None else None
            ),
        },
    )
    return True


def _progress_item_for_entry(
    entry: dict[str, Any],
    *,
    event_index: int,
    timestamp: Optional[str],
    state: ProgressProjectionState,
) -> tuple[Optional[ProgressProjectionItem], bool]:
    if timestamp is None:
        return None, False
    event_type = str(entry.get("event_type") or "")
    event = _event_payload(entry)
    run_event: Optional[RunEvent] = None
    if event_type == "output_delta":
        run_event = OutputDelta(
            timestamp=timestamp,
            content=str(event.get("content") or ""),
            delta_type=str(event.get("delta_type") or "text"),
            stream_mode=str(event.get("stream_mode") or "delta"),
        )
    elif event_type == "run_notice":
        data = event.get("data")
        run_event = RunNotice(
            timestamp=timestamp,
            kind=str(event.get("kind") or ""),
            message=str(event.get("message") or ""),
            data=dict(data) if isinstance(data, dict) else {},
        )
    elif event_type == "tool_call":
        tool_input = event.get("tool_input")
        run_event = ToolCall(
            timestamp=timestamp,
            tool_name=str(event.get("tool_name") or ""),
            tool_input=dict(tool_input) if isinstance(tool_input, dict) else {},
        )
    elif event_type == "tool_result":
        run_event = ToolResult(
            timestamp=timestamp,
            tool_name=str(event.get("tool_name") or ""),
            status=str(event.get("status") or ""),
            result=event.get("result"),
            error=event.get("error"),
        )
    elif event_type == "approval_requested":
        context = event.get("context")
        run_event = ApprovalRequested(
            timestamp=timestamp,
            request_id=str(event.get("request_id") or ""),
            description=str(event.get("description") or ""),
            context=dict(context) if isinstance(context, dict) else {},
        )
    elif event_type == "user_input_requested":
        questions = event.get("questions")
        context = event.get("context")
        run_event = UserInputRequested(
            timestamp=timestamp,
            request_id=str(event.get("request_id") or ""),
            description=str(event.get("description") or ""),
            questions=(
                tuple(
                    dict(question)
                    for question in questions
                    if isinstance(question, dict)
                )
                if isinstance(questions, list)
                else ()
            ),
            context=dict(context) if isinstance(context, dict) else {},
        )
    if run_event is None:
        return None, False
    return reduce_progress_event_merged(
        state,
        ProgressProjectionInput(
            event_id=event_index,
            timestamp=timestamp,
            event=run_event,
        ),
    )


def _assistant_text_from_timeline(entries: Iterable[dict[str, Any]]) -> Optional[str]:
    final_message: Optional[str] = None
    for entry in entries:
        if str(entry.get("event_type") or "") != "turn_completed":
            continue
        event = _event_payload(entry)
        final_message = str(event.get("final_message") or "")
    return final_message if final_message else None


def _terminal_timestamp_from_timeline(
    entries: Iterable[dict[str, Any]],
) -> Optional[str]:
    timestamp: Optional[str] = None
    for entry in entries:
        if str(entry.get("event_type") or "") not in {
            "turn_completed",
            "turn_failed",
            "turn_interrupted",
        }:
            continue
        timestamp = _event_timestamp(entry) or timestamp
    return timestamp


def _terminal_event_ids_from_timeline(entries: Iterable[dict[str, Any]]) -> list[int]:
    event_ids: list[int] = []
    for fallback, entry in enumerate(entries, start=1):
        if str(entry.get("event_type") or "") not in {
            "turn_completed",
            "turn_failed",
            "turn_interrupted",
        }:
            continue
        event_ids.append(_event_index(entry, fallback))
    return event_ids


def _append_user_message(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    turn: dict[str, Any],
    sequence: int,
) -> int:
    managed_turn_id = str(turn.get("managed_turn_id") or "")
    timestamp = _turn_timestamp(turn)
    metadata = _metadata(turn)
    attachments = metadata.get("attachments")
    if not isinstance(attachments, list):
        attachments = []
    raw_prompt = str(turn.get("prompt") or "")
    user_visible_text = _normalize_optional_text(metadata.get("user_visible_text"))
    if user_visible_text is None:
        user_visible_text = raw_prompt
    raw_model_prompt = _normalize_optional_text(metadata.get("raw_model_prompt"))
    if raw_model_prompt is None:
        raw_model_prompt = _normalize_optional_text(metadata.get("runtime_prompt"))
    if raw_model_prompt is None:
        raw_model_prompt = raw_prompt
    capsule_refs = _capsule_refs_from_metadata(metadata)
    item_id = f"turn:{managed_turn_id}:user"
    items.append(
        ManagedThreadTimelineItem(
            item_id=item_id,
            kind="user_message",
            order_key=_order_key(timestamp, sequence, item_id),
            timestamp=timestamp,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            status=str(turn.get("status") or ""),
            identity=_timeline_identity(
                item_id,
                correlation_id=turn.get("client_turn_id"),
            ),
            provenance=_timeline_provenance(),
            payload={
                "text": user_visible_text,
                "text_preview": _truncate_text(user_visible_text, 240),
                "visibility": "user_visible",
                "user_visible_text": user_visible_text,
                "raw_model_prompt": raw_model_prompt,
                "capsule_refs": capsule_refs,
                "client_turn_id": turn.get("client_turn_id"),
                "request_kind": turn.get("request_kind"),
                "attachments": [a for a in attachments if isinstance(a, dict)],
            },
        )
    )
    return sequence + 1


def _capsule_refs_from_metadata(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw_refs = metadata.get("capsule_refs")
    if not isinstance(raw_refs, list):
        return []
    refs: list[dict[str, Any]] = []
    for raw_ref in raw_refs:
        if not isinstance(raw_ref, dict):
            continue
        capsule_id = _normalize_optional_text(raw_ref.get("capsule_id"))
        capsule_version = _normalize_optional_text(
            raw_ref.get("capsule_version") or raw_ref.get("version")
        )
        visibility = _normalize_optional_text(raw_ref.get("visibility"))
        scope = _normalize_optional_text(raw_ref.get("scope"))
        source_digest = _normalize_optional_text(raw_ref.get("source_digest"))
        if not (
            capsule_id and capsule_version and visibility and scope and source_digest
        ):
            continue
        ref: dict[str, Any] = {
            "capsule_id": capsule_id,
            "capsule_version": capsule_version,
            "visibility": visibility,
            "scope": scope,
            "source_digest": source_digest,
        }
        for key in ("payload_digest", "render_decision", "reason"):
            value = _normalize_optional_text(raw_ref.get(key))
            if value is not None:
                ref[key] = value
        refs.append(ref)
    return refs


def _append_status(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    turn: dict[str, Any],
    sequence: int,
    terminal_timestamp: Optional[str] = None,
    source_event_ids: Optional[list[int]] = None,
) -> int:
    managed_turn_id = str(turn.get("managed_turn_id") or "")
    status = str(turn.get("status") or "unknown")
    timestamp = terminal_timestamp or _turn_timestamp(turn)
    item_id = f"turn:{managed_turn_id}:status:{status}"
    items.append(
        ManagedThreadTimelineItem(
            item_id=item_id,
            kind="status",
            order_key=_order_key(timestamp, sequence, item_id),
            timestamp=timestamp,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            status=status,
            identity=_timeline_identity(item_id),
            provenance=_timeline_provenance(source_event_ids=source_event_ids or []),
            payload={
                "status": status,
                "error": turn.get("error"),
                "started_at": turn.get("started_at"),
                "finished_at": turn.get("finished_at"),
                "backend_turn_id": turn.get("backend_turn_id"),
                "source_event_ids": list(source_event_ids or []),
            },
        )
    )
    return sequence + 1


def _append_timeline_event_items(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    entries: list[dict[str, Any]],
    sequence: int,
) -> int:
    tool_group: Optional[dict[str, Any]] = None
    projection_state = ProgressProjectionState()

    def flush_tool_group() -> None:
        # Timeline ordering is the contract; do not let grouped tool events drift
        # past later approvals, notices, or output items.
        nonlocal sequence, tool_group
        if tool_group is None:
            return
        progress_items = [
            item
            for item in tool_group.get("progress_items", [])
            if isinstance(item, dict)
        ]
        source_event_ids = list(tool_group.get("source_event_ids") or [])
        progress_item_ids = _progress_item_ids_from_items(progress_items)
        progress_event_ids = _progress_event_ids_from_items(progress_items)
        item_id = (
            f"turn:{managed_turn_id}:tool:"
            f"{tool_group.get('first_index')}:{tool_group.get('tool_name')}"
        )
        timestamp = _normalize_optional_text(tool_group.get("timestamp"))
        items.append(
            ManagedThreadTimelineItem(
                item_id=item_id,
                kind="tool_group",
                order_key=_order_key(timestamp, sequence, item_id),
                timestamp=timestamp,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                status=str(tool_group.get("status") or "running"),
                identity=_timeline_identity(
                    item_id,
                    progress_item_ids=progress_item_ids,
                ),
                provenance=_timeline_provenance(
                    source_event_ids=source_event_ids,
                    progress_event_ids=progress_event_ids or source_event_ids,
                ),
                payload={
                    "tool_name": tool_group.get("tool_name"),
                    "call": tool_group.get("call"),
                    "result": tool_group.get("result"),
                    "progress_items": progress_items,
                    "source_event_ids": source_event_ids,
                    "source_event_type": "tool_group",
                },
            )
        )
        sequence += 1
        tool_group = None

    for fallback, entry in enumerate(entries, start=1):
        event_type = str(entry.get("event_type") or "")
        event = _event_payload(entry)
        event_index = _event_index(entry, fallback)
        timestamp = _event_timestamp(entry)
        event_stable_id = f"{event_index:04d}"
        progress_item, progress_merged = _progress_item_for_entry(
            entry,
            event_index=event_index,
            timestamp=timestamp,
            state=projection_state,
        )

        if event_type in {"tool_call", "tool_result"}:
            tool_name = str(event.get("tool_name") or "unknown")
            if event_type == "tool_call":
                flush_tool_group()
                tool_group = {
                    "tool_name": tool_name,
                    "first_index": event_index,
                    "timestamp": timestamp,
                    "call": event,
                    "result": None,
                    "status": "running",
                    "source_event_ids": [event_index],
                    "progress_items": (
                        [progress_item.to_dict()] if progress_item is not None else []
                    ),
                }
            else:
                if tool_group is None or tool_group.get("tool_name") != tool_name:
                    flush_tool_group()
                    tool_group = {
                        "tool_name": tool_name,
                        "first_index": event_index,
                        "timestamp": timestamp,
                        "call": None,
                        "result": None,
                        "status": "running",
                        "source_event_ids": [event_index],
                        "progress_items": [],
                    }
                tool_group["result"] = event
                tool_group["status"] = str(event.get("status") or "completed")
                source_event_ids = list(tool_group.get("source_event_ids") or [])
                if event_index not in source_event_ids:
                    source_event_ids.append(event_index)
                tool_group["source_event_ids"] = source_event_ids
                if progress_item is not None:
                    tool_group.setdefault("progress_items", []).append(
                        progress_item.to_dict()
                    )
                flush_tool_group()
            continue

        flush_tool_group()
        if event_type in {"approval_requested", "user_input_requested"}:
            request_id = str(event.get("request_id") or event_stable_id)
            is_user_input = event_type == "user_input_requested"
            item_id = (
                f"turn:{managed_turn_id}:user_input:{request_id}"
                if is_user_input
                else f"turn:{managed_turn_id}:approval:{request_id}"
            )
            progress_item_ids, progress_event_ids = _progress_item_metadata(
                progress_item
            )
            items.append(
                ManagedThreadTimelineItem(
                    item_id=item_id,
                    kind="user_input" if is_user_input else "approval",
                    order_key=_order_key(timestamp, sequence, item_id),
                    timestamp=timestamp,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=managed_turn_id,
                    status=str(entry.get("status") or "recorded"),
                    identity=_timeline_identity(
                        item_id,
                        progress_item_ids=progress_item_ids,
                    ),
                    provenance=_timeline_provenance(
                        source_event_ids=[event_index],
                        progress_event_ids=progress_event_ids or [event_index],
                    ),
                    payload={
                        **event,
                        "source_event_ids": [event_index],
                        "source_event_type": event_type,
                        "progress_item": (
                            progress_item.to_dict()
                            if progress_item is not None
                            else None
                        ),
                    },
                )
            )
            sequence += 1
            continue

        if event_type == "run_notice":
            provider_compaction = _provider_compaction_payload_from_notice(event)
            if provider_compaction is not None:
                item_id = f"turn:{managed_turn_id}:context_compaction:{event_stable_id}"
                items.append(
                    ManagedThreadTimelineItem(
                        item_id=item_id,
                        kind="lifecycle",
                        order_key=_order_key(timestamp, sequence, item_id),
                        timestamp=timestamp,
                        managed_thread_id=managed_thread_id,
                        managed_turn_id=managed_turn_id,
                        status=str(entry.get("status") or "recorded"),
                        identity=_timeline_identity(item_id),
                        provenance=_timeline_provenance(source_event_ids=[event_index]),
                        payload={
                            "lifecycle_kind": "context_compaction",
                            "title": "Runtime compacted context",
                            "text": "The provider summarized or pruned its internal session context.",
                            "context_compaction": provider_compaction,
                            "source_event_ids": [event_index],
                            "source_event_type": event_type,
                        },
                    )
                )
                sequence += 1
                continue
            if _is_internal_run_notice(event, progress_item):
                continue
            if _merge_streamed_intermediate_timeline_item(
                items,
                progress_item=progress_item,
                progress_merged=progress_merged,
                event_index=event_index,
                timestamp=timestamp,
                intermediate_kind=str(event.get("kind") or "notice"),
                text=str(event.get("message") or ""),
                event_type=event_type,
                event=event,
            ):
                continue
            item_id = _streamed_intermediate_item_id(
                managed_turn_id,
                progress_item=progress_item,
                event_stable_id=event_stable_id,
            )
            progress_item_ids, progress_event_ids = _progress_item_metadata(
                progress_item
            )
            progress_item_dict = (
                progress_item.to_dict() if progress_item is not None else None
            )
            items.append(
                ManagedThreadTimelineItem(
                    item_id=item_id,
                    kind="intermediate",
                    order_key=_order_key(timestamp, sequence, item_id),
                    timestamp=timestamp,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=managed_turn_id,
                    status=str(entry.get("status") or "recorded"),
                    identity=_timeline_identity(
                        item_id,
                        progress_item_ids=progress_item_ids,
                    ),
                    provenance=_timeline_provenance(
                        source_event_ids=[event_index],
                        progress_event_ids=progress_event_ids or [event_index],
                    ),
                    payload={
                        "intermediate_kind": event.get("kind") or "notice",
                        "text": event.get("message") or "",
                        "event_type": event_type,
                        "event": event,
                        "source_event_ids": [event_index],
                        "source_event_type": event_type,
                        "detail_available": True,
                        "hidden": bool(
                            progress_item is not None and progress_item.hidden
                        ),
                        "progress_item": progress_item_dict,
                    },
                )
            )
            sequence += 1
            continue

        if event_type == "output_delta":
            if _is_default_timeline_suppressed_output_delta(event):
                continue
            if _merge_streamed_intermediate_timeline_item(
                items,
                progress_item=progress_item,
                progress_merged=progress_merged,
                event_index=event_index,
                timestamp=timestamp,
                intermediate_kind=str(event.get("delta_type") or "output"),
                text=str(event.get("content") or ""),
                event_type=event_type,
                event=event,
            ):
                continue
            item_id = _streamed_intermediate_item_id(
                managed_turn_id,
                progress_item=progress_item,
                event_stable_id=event_stable_id,
            )
            progress_item_ids, progress_event_ids = _progress_item_metadata(
                progress_item
            )
            items.append(
                ManagedThreadTimelineItem(
                    item_id=item_id,
                    kind="intermediate",
                    order_key=_order_key(timestamp, sequence, item_id),
                    timestamp=timestamp,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=managed_turn_id,
                    status=str(entry.get("status") or "recorded"),
                    identity=_timeline_identity(
                        item_id,
                        progress_item_ids=progress_item_ids,
                    ),
                    provenance=_timeline_provenance(
                        source_event_ids=[event_index],
                        progress_event_ids=progress_event_ids or [event_index],
                    ),
                    payload={
                        "intermediate_kind": event.get("delta_type") or "output",
                        "text": event.get("content") or "",
                        "event_type": event_type,
                        "event": event,
                        "source_event_ids": [event_index],
                        "source_event_type": event_type,
                        "detail_available": True,
                        "progress_item": (
                            progress_item.to_dict()
                            if progress_item is not None
                            else None
                        ),
                    },
                )
            )
            sequence += 1

    flush_tool_group()
    return sequence


def timeline_item_from_tail_event(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    tail_event: dict[str, Any],
) -> dict[str, Any] | None:
    """Project one live tail event into the canonical PMA timeline item shape.

    Live web streams should append/update the same durable item contract returned
    by `/timeline`; chat adapters can keep rendering compact progress summaries.
    """

    normalized_thread_id = _normalize_optional_text(managed_thread_id)
    normalized_turn_id = _normalize_optional_text(managed_turn_id)
    if normalized_thread_id is None or normalized_turn_id is None:
        return None

    event_type = str(tail_event.get("event_type") or "").strip()
    event_id = str(tail_event.get("event_id") or "").strip()
    timestamp = _normalize_optional_text(tail_event.get("received_at"))
    progress_item = tail_event.get("progress_item")
    progress = dict(progress_item) if isinstance(progress_item, dict) else {}
    raw_progress_items = tail_event.get("progress_items")
    progress_items = (
        [dict(item) for item in raw_progress_items if isinstance(item, dict)]
        if isinstance(raw_progress_items, list)
        else ([progress] if progress else [])
    )
    progress_kind = str(
        tail_event.get("progress_kind") or progress.get("kind") or ""
    ).strip()
    progress_group_id = _normalize_optional_text(
        tail_event.get("progress_group_id") or progress.get("group_id")
    )
    progress_item_id = _normalize_optional_text(
        tail_event.get("progress_item_id") or progress.get("item_id")
    )
    stable_suffix = (
        progress_group_id
        or progress_item_id
        or str(tail_event.get("summary") or "")
        or event_type
        or "event"
    )
    source_event_ids = tail_event.get("source_event_ids")
    if not isinstance(source_event_ids, list):
        source_event_ids = tail_event.get("progress_event_ids")
    if not isinstance(source_event_ids, list):
        source_event_ids = _progress_event_ids_from_items(progress_items)
    if not isinstance(source_event_ids, list) or not source_event_ids:
        source_event_ids = progress.get("event_ids")
    if not isinstance(source_event_ids, list):
        source_event_ids = []
    try:
        source_event_key = f"{int(source_event_ids[-1]):04d}"
    except (IndexError, TypeError, ValueError):
        source_event_key = progress_item_id or stable_suffix

    base = {
        "order_key": _order_key(
            timestamp,
            int(tail_event.get("event_id") or 0),
            stable_suffix,
        ),
        "timestamp": timestamp,
        "managed_thread_id": normalized_thread_id,
        "managed_turn_id": normalized_turn_id,
        "status": str(tail_event.get("progress_state") or "recorded"),
    }

    if event_type in {"tool_started", "tool_completed", "tool_failed"}:
        tool_name = (
            _normalize_optional_text(tail_event.get("tool_name"))
            or _normalize_optional_text(progress.get("tool_name"))
            or "tool"
        )
        tool_stable_id = stable_suffix
        if source_event_ids:
            try:
                tool_stable_id = str(min(int(x) for x in source_event_ids))
            except (TypeError, ValueError):
                tool_stable_id = str(source_event_ids[0])
        elif progress_group_id and progress_group_id.startswith("tools:"):
            parts = progress_group_id.split(":", 2)
            if len(parts) >= 2 and parts[1].strip():
                try:
                    tool_stable_id = str(int(parts[1]))
                except ValueError:
                    tool_stable_id = parts[1].strip()
        item_id = f"turn:{normalized_turn_id}:tool:{tool_stable_id}:{tool_name}"
        state = str(tail_event.get("tool_state") or progress.get("state") or "")
        result = None
        if state in {"completed", "failed"}:
            result = {
                "status": "error" if state == "failed" else "completed",
                "summary": tail_event.get("summary"),
            }
        item = {
            **base,
            "item_id": item_id,
            "kind": "tool_group",
            "status": state or base["status"],
            "payload": {
                "tool_name": tool_name,
                "call": {
                    "tool_name": tool_name,
                    "summary": tail_event.get("summary"),
                },
                "result": result,
                "progress_items": progress_items,
                "source_event_ids": source_event_ids,
                "source_event_type": event_type,
                "detail_available": True,
                "live_tail_event": dict(tail_event),
            },
        }
        return _with_contract_metadata(
            item,
            progress_item_ids=_progress_item_ids_from_items(progress_items),
            source_event_ids=source_event_ids,
            progress_event_ids=_progress_event_ids_from_items(progress_items)
            or list(source_event_ids),
            cursor_event_id=event_id or None,
        )

    if (
        event_type in {"progress", "assistant_update"} and progress_kind != "approval"
    ) or progress_kind in {"assistant_update", "notice"}:
        provider_compaction = _provider_compaction_payload_from_notice(tail_event)
        if provider_compaction is not None:
            item_id = f"turn:{normalized_turn_id}:context_compaction:{source_event_key}"
            item = {
                **base,
                "item_id": item_id,
                "kind": "lifecycle",
                "payload": {
                    "lifecycle_kind": "context_compaction",
                    "title": "Runtime compacted context",
                    "text": "The provider summarized or pruned its internal session context.",
                    "context_compaction": provider_compaction,
                    "source_event_ids": source_event_ids,
                    "source_event_type": event_type,
                    "live_tail_event": dict(tail_event),
                },
            }
            return _with_contract_metadata(
                item,
                progress_item_ids=_progress_item_ids_from_items(progress_items),
                source_event_ids=source_event_ids,
                progress_event_ids=_progress_event_ids_from_items(progress_items)
                or list(source_event_ids),
                cursor_event_id=event_id or None,
            )
        if _is_internal_run_notice(tail_event, progress):
            return None
        item_id = f"turn:{normalized_turn_id}:intermediate:{source_event_key}"
        text = str(tail_event.get("summary") or "")
        title = _progress_display_title(
            fallback_kind=progress_kind or event_type or "Update",
            title=tail_event.get("title") or progress.get("title"),
            summary=tail_event.get("summary") or progress.get("summary"),
            phase=(
                tail_event.get("phase")
                or progress.get("phase")
                or progress.get("assistant_phase")
                or progress.get("tool_phase")
            ),
            event_type=event_type,
            progress_kind=progress_kind,
        )
        intermediate_kind = (
            "thinking"
            if progress_kind == "assistant_update" or title.lower() == "thinking"
            else event_type or "notice"
        )
        item = {
            **base,
            "item_id": item_id,
            "kind": "intermediate",
            "payload": {
                "intermediate_kind": intermediate_kind,
                "title": title,
                "text": text,
                "event_type": event_type,
                "event": dict(tail_event),
                "source_event_ids": source_event_ids,
                "source_event_type": event_type,
                "detail_available": True,
                "hidden": bool(progress.get("hidden") is True),
                "progress_item": progress or None,
                "live_tail_event": dict(tail_event),
            },
        }
        return _with_contract_metadata(
            item,
            progress_item_ids=_progress_item_ids_from_items(progress_items),
            source_event_ids=source_event_ids,
            progress_event_ids=list(source_event_ids),
            cursor_event_id=event_id or None,
        )

    if progress_kind == "approval" or event_type == "approval_requested":
        request_id = stable_suffix
        if progress_item_id and progress_item_id.startswith("progress:approval:"):
            request_id = progress_item_id.removeprefix("progress:approval:")
        item = {
            **base,
            "item_id": f"turn:{normalized_turn_id}:approval:{request_id}",
            "kind": "approval",
            "status": str(tail_event.get("progress_state") or "waiting"),
            "payload": {
                "request_id": request_id,
                "description": tail_event.get("summary") or "Approval requested",
                "source_event_ids": source_event_ids,
                "source_event_type": event_type,
                "detail_available": True,
                "progress_item": progress or None,
                "live_tail_event": dict(tail_event),
            },
        }
        return _with_contract_metadata(
            item,
            progress_item_ids=_progress_item_ids_from_items(progress_items),
            source_event_ids=source_event_ids,
            progress_event_ids=list(source_event_ids),
            cursor_event_id=event_id or None,
        )

    if event_type in {"turn_completed", "turn_failed", "turn_interrupted"}:
        status = {
            "turn_completed": "ok",
            "turn_failed": "error",
            "turn_interrupted": "interrupted",
        }[event_type]
        item_id = f"turn:{normalized_turn_id}:status:{status}"
        item = {
            **base,
            "item_id": item_id,
            "kind": "status",
            "status": status,
            "payload": {
                "status": status,
                "error": tail_event.get("summary") if status == "error" else None,
                "event_type": event_type,
                "event": dict(tail_event),
                "source_event_ids": source_event_ids,
                "source_event_type": event_type,
                "live_tail_event": dict(tail_event),
            },
        }
        return _with_contract_metadata(
            item,
            progress_item_ids=_progress_item_ids_from_items(progress_items),
            source_event_ids=source_event_ids,
            progress_event_ids=list(source_event_ids),
            cursor_event_id=event_id or None,
        )

    return None


def _append_assistant_message(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    turn: dict[str, Any],
    entries: list[dict[str, Any]],
    sequence: int,
) -> int:
    managed_turn_id = str(turn.get("managed_turn_id") or "")
    assistant_text = _normalize_optional_text(turn.get("assistant_text"))
    if assistant_text is None:
        assistant_text = _assistant_text_from_timeline(entries)
    if assistant_text is None:
        return sequence
    timestamp = (
        _terminal_timestamp_from_timeline(entries)
        or _normalize_optional_text(turn.get("finished_at"))
        or _turn_timestamp(turn)
    )
    source_event_ids = _terminal_event_ids_from_timeline(entries)
    item_id = f"turn:{managed_turn_id}:assistant"
    items.append(
        ManagedThreadTimelineItem(
            item_id=item_id,
            kind="assistant_message",
            order_key=_order_key(timestamp, sequence, item_id),
            timestamp=timestamp,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            status=str(turn.get("status") or ""),
            identity=_timeline_identity(item_id),
            provenance=_timeline_provenance(source_event_ids=source_event_ids),
            payload={
                "text": assistant_text,
                "text_preview": _truncate_text(assistant_text, 240),
                "backend_turn_id": turn.get("backend_turn_id"),
                "source_event_ids": source_event_ids,
            },
        )
    )
    return sequence + 1


def _append_artifacts(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    turn: dict[str, Any],
    sequence: int,
) -> int:
    # User attachments are already carried on the user-message item (and render
    # as inline attachment pills), so they are intentionally not duplicated as
    # standalone artifact items here. Only genuine surfaced artifacts, which have
    # no message of their own, become standalone artifact cards.
    managed_turn_id = str(turn.get("managed_turn_id") or "")
    metadata = _metadata(turn)
    timestamp = _turn_timestamp(turn)
    values = metadata.get("artifacts")
    if not isinstance(values, list):
        return sequence
    for index, value in enumerate(values, start=1):
        if not isinstance(value, dict):
            continue
        item_id = f"turn:{managed_turn_id}:artifacts:{index}"
        items.append(
            ManagedThreadTimelineItem(
                item_id=item_id,
                kind="artifact",
                order_key=_order_key(timestamp, sequence, item_id),
                timestamp=timestamp,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                status=str(turn.get("status") or ""),
                identity=_timeline_identity(item_id),
                provenance=_timeline_provenance(),
                payload={"artifact_kind": "artifact", **value},
            )
        )
        sequence += 1
    return sequence


def _list_delivery_records(hub_root: Any, managed_thread_id: str) -> list[Any]:
    ledger = SQLiteManagedThreadDeliveryLedger(hub_root, durable=False)
    return ledger.list_records_for_managed_thread(managed_thread_id)


def _append_delivery_state_items(
    items: list[ManagedThreadTimelineItem],
    *,
    hub_root: Any,
    managed_thread_id: str,
    sequence: int,
) -> int:
    for record in _list_delivery_records(hub_root, managed_thread_id):
        item_id = f"delivery:{record.delivery_id}"
        timestamp = record.updated_at or record.created_at
        items.append(
            ManagedThreadTimelineItem(
                item_id=item_id,
                kind="delivery_state",
                order_key=_order_key(timestamp, sequence, item_id),
                timestamp=timestamp,
                managed_thread_id=managed_thread_id,
                managed_turn_id=record.managed_turn_id,
                status=record.state.value,
                identity=_timeline_identity(item_id),
                provenance=_timeline_provenance(),
                payload={
                    "delivery_id": record.delivery_id,
                    "surface_kind": record.target.surface_kind,
                    "surface_key": record.target.surface_key,
                    "adapter_key": record.target.adapter_key,
                    "state": record.state.value,
                    "attempt_count": record.attempt_count,
                    "delivered_at": record.delivered_at,
                    "last_error": record.last_error,
                    "final_status": record.envelope.final_status,
                },
            )
        )
        sequence += 1
    return sequence


def _decode_action_payload(action: dict[str, Any]) -> dict[str, Any]:
    payload_json = action.get("payload_json")
    if not isinstance(payload_json, str) or not payload_json.strip():
        return {}
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {"payload_decode_error": True, "payload_json": payload_json}
    return dict(payload) if isinstance(payload, dict) else {}


def _turn_merge_timestamp(turn: dict[str, Any]) -> Optional[str]:
    """Timestamp used to interleave compaction lifecycle rows with turns."""
    return _normalize_optional_text(turn.get("started_at") or turn.get("created_at"))


def _sorted_compact_actions(
    thread_store: Any, managed_thread_id: str
) -> list[dict[str, Any]]:
    list_actions = getattr(thread_store, "list_thread_actions", None)
    if not callable(list_actions):
        return []
    actions: list[dict[str, Any]] = []
    for action in list_actions(managed_thread_id):
        if str(action.get("action_type") or "") != "managed_thread_compact":
            continue
        action_id = str(action.get("action_id") or "")
        if action_id:
            actions.append(action)
    actions.sort(
        key=lambda a: (
            _normalize_optional_text(a.get("created_at")) or "\uffff",
            str(a.get("action_id") or ""),
        )
    )
    return actions


def _append_compact_lifecycle_item(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    action: dict[str, Any],
    sequence: int,
) -> int:
    action_type = str(action.get("action_type") or "")
    action_id = str(action.get("action_id") or "")
    payload = _decode_action_payload(action)
    timestamp = _normalize_optional_text(action.get("created_at"))
    item_id = f"action:{action_id}:compact"
    summary = _normalize_optional_text(
        payload.get("summary")
    ) or _normalize_optional_text(payload.get("compact_seed"))
    preview = _normalize_optional_text(payload.get("summary_preview"))
    items.append(
        ManagedThreadTimelineItem(
            item_id=item_id,
            kind="lifecycle",
            order_key=_order_key(timestamp, sequence, item_id),
            timestamp=timestamp,
            managed_thread_id=managed_thread_id,
            managed_turn_id=None,
            status="recorded",
            identity=_timeline_identity(item_id),
            provenance=_timeline_provenance(),
            payload={
                **payload,
                "lifecycle_kind": "context_compaction",
                "title": "Context compacted by CAR",
                "text": "Earlier conversation was summarized and will be injected into the next turn.",
                "context_compaction": _context_compaction_payload(
                    source="car",
                    provider=_normalize_optional_text(payload.get("provider")),
                    summary=summary,
                    preview=preview,
                    scope="managed_thread",
                    started_fresh_session=bool(payload.get("reset_backend") is True),
                    stored_by_car=True,
                ),
                "action_id": action_id,
                "action_type": action_type,
            },
        )
    )
    return sequence + 1


def _append_turn_timeline_items(
    items: list[ManagedThreadTimelineItem],
    *,
    hub_root: Any,
    managed_thread_id: str,
    turn: dict[str, Any],
    sequence: int,
    turn_timeline_entries: Optional[list[dict[str, Any]]] = None,
) -> int:
    managed_turn_id = str(turn.get("managed_turn_id") or "").strip()
    if not managed_turn_id:
        return sequence
    entries = (
        turn_timeline_entries
        if turn_timeline_entries is not None
        else list_turn_timeline(hub_root, execution_id=managed_turn_id)
    )
    sequence = _append_user_message(
        items,
        managed_thread_id=managed_thread_id,
        turn=turn,
        sequence=sequence,
    )
    if str(turn.get("status") or "") in {"queued", "running"}:
        sequence = _append_status(
            items,
            managed_thread_id=managed_thread_id,
            turn=turn,
            sequence=sequence,
        )
    sequence = _append_timeline_event_items(
        items,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        entries=entries,
        sequence=sequence,
    )
    sequence = _append_assistant_message(
        items,
        managed_thread_id=managed_thread_id,
        turn=turn,
        entries=entries,
        sequence=sequence,
    )
    if str(turn.get("status") or "") not in {"queued", "running"}:
        terminal_event_ids = _terminal_event_ids_from_timeline(entries)
        sequence = _append_status(
            items,
            managed_thread_id=managed_thread_id,
            turn=turn,
            sequence=sequence,
            terminal_timestamp=(
                _terminal_timestamp_from_timeline(entries)
                or _normalize_optional_text(turn.get("finished_at"))
            ),
            source_event_ids=terminal_event_ids,
        )
    sequence = _append_artifacts(
        items,
        managed_thread_id=managed_thread_id,
        turn=turn,
        sequence=sequence,
    )
    return sequence


def build_managed_thread_timeline(
    hub_root: Any,
    *,
    thread_store: Any,
    managed_thread_id: str,
    limit: int = 500,
) -> dict[str, Any]:
    normalized_thread_id = str(managed_thread_id or "").strip()
    if not normalized_thread_id:
        raise ValueError("managed_thread_id is required")
    bounded_limit = max(int(limit or 500), 1)
    thread = thread_store.get_thread(normalized_thread_id)
    if thread is None:
        raise KeyError(normalized_thread_id)

    turns = list(
        reversed(thread_store.list_turns(normalized_thread_id, limit=bounded_limit))
    )
    turn_timeline_entries = list_turn_timelines(
        hub_root,
        execution_ids=[str(turn.get("managed_turn_id") or "") for turn in turns],
    )
    compact_actions = _sorted_compact_actions(thread_store, normalized_thread_id)
    items: list[ManagedThreadTimelineItem] = []
    sequence = 1
    turn_index = 0
    action_index = 0
    while turn_index < len(turns) and action_index < len(compact_actions):
        turn = turns[turn_index]
        action = compact_actions[action_index]
        turn_ts = _turn_merge_timestamp(turn)
        act_ts = _normalize_optional_text(action.get("created_at"))
        if turn_ts is None or act_ts is None:
            emit_turn_first = True
        else:
            emit_turn_first = act_ts >= turn_ts
        if emit_turn_first:
            sequence = _append_turn_timeline_items(
                items,
                hub_root=hub_root,
                managed_thread_id=normalized_thread_id,
                turn=turn,
                sequence=sequence,
                turn_timeline_entries=turn_timeline_entries.get(
                    str(turn.get("managed_turn_id") or "")
                ),
            )
            turn_index += 1
        else:
            sequence = _append_compact_lifecycle_item(
                items,
                managed_thread_id=normalized_thread_id,
                action=action,
                sequence=sequence,
            )
            action_index += 1
    while turn_index < len(turns):
        sequence = _append_turn_timeline_items(
            items,
            hub_root=hub_root,
            managed_thread_id=normalized_thread_id,
            turn=turns[turn_index],
            sequence=sequence,
            turn_timeline_entries=turn_timeline_entries.get(
                str(turns[turn_index].get("managed_turn_id") or "")
            ),
        )
        turn_index += 1
    while action_index < len(compact_actions):
        sequence = _append_compact_lifecycle_item(
            items,
            managed_thread_id=normalized_thread_id,
            action=compact_actions[action_index],
            sequence=sequence,
        )
        action_index += 1
    sequence = _append_delivery_state_items(
        items,
        hub_root=hub_root,
        managed_thread_id=normalized_thread_id,
        sequence=sequence,
    )

    ordered = sorted(items, key=lambda item: item.order_key)
    return {
        "managed_thread_id": normalized_thread_id,
        "contract_version": TIMELINE_CONTRACT_VERSION,
        "projection": {
            "kind": "transcript",
            "limit": bounded_limit,
            "omits_output_delta_types": sorted(DEFAULT_SUPPRESSED_OUTPUT_DELTA_TYPES),
            "raw_trace_available": True,
            "raw_trace_route": (
                f"/hub/pma/threads/{normalized_thread_id}/turns/{{managed_turn_id}}"
            ),
        },
        "items": [item.to_dict() for item in ordered],
        "item_count": len(ordered),
    }


__all__ = [
    "TIMELINE_CONTRACT_VERSION",
    "ManagedThreadTimelineIdentity",
    "ManagedThreadTimelineItem",
    "ManagedThreadTimelineProvenance",
    "MAX_MANAGED_THREAD_TIMELINE_LIMIT",
    "build_managed_thread_timeline",
    "timeline_item_from_tail_event",
]
