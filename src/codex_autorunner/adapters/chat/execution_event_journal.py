from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from ...core.orchestration.turn_timeline import append_turn_timeline, list_turn_timeline
from ...core.ports.run_event import (
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
from ...core.time_utils import now_iso

CHAT_EXECUTION_JOURNAL_NOTICE_KIND = "chat_execution_journal"
CHAT_EXECUTION_JOURNAL_SCHEMA_VERSION = 1


def _normalize_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = value if isinstance(value, str) else str(value)
    stripped = normalized.strip()
    return stripped or None


def _copy_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class ChatExecutionJournalEvent:
    timestamp: str
    domain: str
    name: str
    message: Optional[str] = None
    status: Optional[str] = None
    event_index: Optional[int] = None
    event_type: Optional[str] = None
    source_event_type: Optional[str] = None
    derived: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_chat_execution_journal_notice(
    *,
    domain: str,
    name: str,
    timestamp: Optional[str] = None,
    message: Optional[str] = None,
    status: Optional[str] = None,
    data: Optional[Mapping[str, Any]] = None,
) -> RunNotice:
    return RunNotice(
        timestamp=timestamp or now_iso(),
        kind=CHAT_EXECUTION_JOURNAL_NOTICE_KIND,
        message=_normalize_optional_text(message) or f"{domain}.{name}",
        data={
            "schema_version": CHAT_EXECUTION_JOURNAL_SCHEMA_VERSION,
            "domain": domain,
            "name": name,
            "status": _normalize_optional_text(status),
            "message": _normalize_optional_text(message),
            "data": _copy_mapping(data),
        },
    )


def _journal_event_from_notice(
    event: RunNotice,
    *,
    event_index: Optional[int],
    event_type: Optional[str],
) -> Optional[ChatExecutionJournalEvent]:
    if event.kind != CHAT_EXECUTION_JOURNAL_NOTICE_KIND:
        return None
    payload = _copy_mapping(event.data)
    if int(payload.get("schema_version") or 0) != CHAT_EXECUTION_JOURNAL_SCHEMA_VERSION:
        return None
    domain = _normalize_optional_text(payload.get("domain"))
    name = _normalize_optional_text(payload.get("name"))
    if domain is None or name is None:
        return None
    return ChatExecutionJournalEvent(
        timestamp=event.timestamp,
        domain=domain,
        name=name,
        message=_normalize_optional_text(payload.get("message")) or event.message,
        status=_normalize_optional_text(payload.get("status")),
        event_index=event_index,
        event_type=event_type,
        source_event_type="run_notice",
        data=_copy_mapping(payload.get("data")),
    )


def _delivery_markers_from_latency_summary(
    event: ChatExecutionJournalEvent,
) -> list[ChatExecutionJournalEvent]:
    if event.domain != "latency" or event.name != "summary":
        return []
    marker_specs = (
        ("chat_ux_delta_first_visible_ms", "first_visible_feedback"),
        ("chat_ux_delta_queue_visible_ms", "queue_visible"),
        ("chat_ux_delta_terminal_ms", "terminal_delivery"),
        ("chat_ux_delta_interrupt_visible_ms", "interrupt_requested_visible"),
    )
    derived: list[ChatExecutionJournalEvent] = []
    for field_name, marker_name in marker_specs:
        value = event.data.get(field_name)
        if not isinstance(value, (int, float)):
            continue
        derived.append(
            ChatExecutionJournalEvent(
                timestamp=event.timestamp,
                domain="delivery",
                name=marker_name,
                message=marker_name.replace("_", " "),
                status=event.status,
                event_index=event.event_index,
                event_type=event.event_type,
                source_event_type=event.source_event_type,
                derived=True,
                data={
                    "latency_field": field_name,
                    "latency_ms": float(value),
                    "derived_from": "latency.summary",
                    "event_name": event.data.get("event_name"),
                    "platform": event.data.get("chat_ux_platform"),
                },
            )
        )
    return derived


def _standard_journal_event(
    *,
    timestamp: str,
    domain: str,
    name: str,
    event_index: Optional[int],
    event_type: Optional[str],
    source_event_type: str,
    status: Optional[str] = None,
    message: Optional[str] = None,
    data: Optional[Mapping[str, Any]] = None,
) -> ChatExecutionJournalEvent:
    return ChatExecutionJournalEvent(
        timestamp=timestamp,
        domain=domain,
        name=name,
        message=message,
        status=status,
        event_index=event_index,
        event_type=event_type,
        source_event_type=source_event_type,
        data=_copy_mapping(data),
    )


def journal_events_from_run_events(
    events: Iterable[RunEvent],
    *,
    include_derived_events: bool = True,
    start_index: int = 1,
) -> list[ChatExecutionJournalEvent]:
    journal: list[ChatExecutionJournalEvent] = []
    for offset, event in enumerate(events):
        event_index = start_index + offset
        event_type = type(event).__name__
        mapped: Optional[ChatExecutionJournalEvent] = None
        if isinstance(event, Started):
            mapped = _standard_journal_event(
                timestamp=event.timestamp,
                domain="execution",
                name="started",
                event_index=event_index,
                event_type=event_type,
                source_event_type="started",
                status="running",
                data={
                    "session_id": event.session_id,
                    "thread_id": event.thread_id,
                    "turn_id": event.turn_id,
                },
            )
        elif isinstance(event, OutputDelta):
            mapped = _standard_journal_event(
                timestamp=event.timestamp,
                domain="execution",
                name="output_delta",
                event_index=event_index,
                event_type=event_type,
                source_event_type="output_delta",
                data={"delta_type": event.delta_type, "content": event.content},
            )
        elif isinstance(event, ToolCall):
            mapped = _standard_journal_event(
                timestamp=event.timestamp,
                domain="execution",
                name="tool_call",
                event_index=event_index,
                event_type=event_type,
                source_event_type="tool_call",
                status="running",
                data={
                    "tool_name": event.tool_name,
                    "tool_input": dict(event.tool_input),
                },
            )
        elif isinstance(event, ToolResult):
            mapped = _standard_journal_event(
                timestamp=event.timestamp,
                domain="execution",
                name="tool_result",
                event_index=event_index,
                event_type=event_type,
                source_event_type="tool_result",
                status=_normalize_optional_text(event.status),
                data={
                    "tool_name": event.tool_name,
                    "result": event.result,
                    "error": event.error,
                },
            )
        elif isinstance(event, ApprovalRequested):
            mapped = _standard_journal_event(
                timestamp=event.timestamp,
                domain="approval",
                name="requested",
                event_index=event_index,
                event_type=event_type,
                source_event_type="approval_requested",
                status="requested",
                message=event.description,
                data={
                    "request_id": event.request_id,
                    "context": dict(event.context),
                },
            )
        elif isinstance(event, TokenUsage):
            mapped = _standard_journal_event(
                timestamp=event.timestamp,
                domain="execution",
                name="token_usage",
                event_index=event_index,
                event_type=event_type,
                source_event_type="token_usage",
                data={"usage": dict(event.usage)},
            )
        elif isinstance(event, RunNotice):
            mapped = _journal_event_from_notice(
                event,
                event_index=event_index,
                event_type=event_type,
            )
            if mapped is None:
                mapped = _standard_journal_event(
                    timestamp=event.timestamp,
                    domain="execution",
                    name="notice",
                    event_index=event_index,
                    event_type=event_type,
                    source_event_type="run_notice",
                    message=event.message,
                    data={
                        "kind": event.kind,
                        "message": event.message,
                        "data": dict(event.data),
                    },
                )
        elif isinstance(event, Completed):
            mapped = _standard_journal_event(
                timestamp=event.timestamp,
                domain="execution",
                name="completed",
                event_index=event_index,
                event_type=event_type,
                source_event_type="completed",
                status="ok",
                message=event.final_message,
                data={"final_message": event.final_message},
            )
        elif isinstance(event, Failed):
            mapped = _standard_journal_event(
                timestamp=event.timestamp,
                domain="execution",
                name="failed",
                event_index=event_index,
                event_type=event_type,
                source_event_type="failed",
                status="error",
                message=event.error_message,
                data={"error_message": event.error_message},
            )
        if mapped is None:
            continue
        journal.append(mapped)
        if include_derived_events:
            journal.extend(_delivery_markers_from_latency_summary(mapped))
    return journal


def _run_event_from_timeline_entry(entry: Mapping[str, Any]) -> Optional[RunEvent]:
    event_type = str(entry.get("event_type") or "").strip().lower()
    event = entry.get("event")
    if not isinstance(event, Mapping):
        return None
    payload = dict(event)
    if event_type == "turn_started":
        return Started(
            timestamp=str(payload.get("timestamp") or ""),
            session_id=str(payload.get("session_id") or ""),
            thread_id=_normalize_optional_text(payload.get("thread_id")),
            turn_id=_normalize_optional_text(payload.get("turn_id")),
        )
    if event_type == "output_delta":
        return OutputDelta(
            timestamp=str(payload.get("timestamp") or ""),
            content=str(payload.get("content") or ""),
            delta_type=str(payload.get("delta_type") or "text"),
        )
    if event_type == "tool_call":
        return ToolCall(
            timestamp=str(payload.get("timestamp") or ""),
            tool_name=str(payload.get("tool_name") or ""),
            tool_input=_copy_mapping(payload.get("tool_input")),
        )
    if event_type == "tool_result":
        return ToolResult(
            timestamp=str(payload.get("timestamp") or ""),
            tool_name=str(payload.get("tool_name") or ""),
            status=str(payload.get("status") or ""),
            result=payload.get("result"),
            error=payload.get("error"),
        )
    if event_type == "approval_requested":
        return ApprovalRequested(
            timestamp=str(payload.get("timestamp") or ""),
            request_id=str(payload.get("request_id") or ""),
            description=str(payload.get("description") or ""),
            context=_copy_mapping(payload.get("context")),
        )
    if event_type == "token_usage":
        return TokenUsage(
            timestamp=str(payload.get("timestamp") or ""),
            usage=_copy_mapping(payload.get("usage")),
        )
    if event_type == "run_notice":
        return RunNotice(
            timestamp=str(payload.get("timestamp") or ""),
            kind=str(payload.get("kind") or ""),
            message=str(payload.get("message") or ""),
            data=_copy_mapping(payload.get("data")),
        )
    if event_type == "turn_completed":
        return Completed(
            timestamp=str(payload.get("timestamp") or ""),
            final_message=str(payload.get("final_message") or ""),
        )
    if event_type == "turn_failed":
        return Failed(
            timestamp=str(payload.get("timestamp") or ""),
            error_message=str(payload.get("error_message") or ""),
        )
    return None


def journal_events_from_timeline_entries(
    entries: Sequence[Mapping[str, Any]],
    *,
    include_derived_events: bool = True,
) -> list[ChatExecutionJournalEvent]:
    journal: list[ChatExecutionJournalEvent] = []
    for entry in entries:
        event = _run_event_from_timeline_entry(entry)
        if event is None:
            continue
        journal.extend(
            journal_events_from_run_events(
                [event],
                include_derived_events=include_derived_events,
                start_index=int(entry.get("event_index") or 1),
            )
        )
    return journal


def list_chat_execution_journal(
    hub_root: Any,
    *,
    execution_id: str,
    include_derived_events: bool = True,
) -> list[dict[str, Any]]:
    return [
        event.to_dict()
        for event in journal_events_from_timeline_entries(
            list_turn_timeline(hub_root, execution_id=execution_id),
            include_derived_events=include_derived_events,
        )
    ]


def append_chat_execution_journal_notices(
    hub_root: Any,
    *,
    execution_id: str,
    target_kind: Optional[str],
    target_id: Optional[str],
    notices: Iterable[RunNotice],
    repo_id: Optional[str] = None,
    run_id: Optional[str] = None,
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> int:
    return append_turn_timeline(
        hub_root,
        execution_id=execution_id,
        target_kind=target_kind,
        target_id=target_id,
        repo_id=repo_id,
        run_id=run_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        metadata=dict(metadata or {}),
        events=list(notices),
    )


def extract_token_usage_from_journal_events(
    journal_events: Sequence[Mapping[str, Any]],
) -> Optional[dict[str, Any]]:
    for entry in reversed(journal_events):
        if (
            str(entry.get("domain") or "") == "execution"
            and str(entry.get("name") or "") == "token_usage"
        ):
            data = entry.get("data")
            if not isinstance(data, Mapping):
                continue
            usage = data.get("usage")
            if isinstance(usage, Mapping):
                return dict(usage)
    return None
