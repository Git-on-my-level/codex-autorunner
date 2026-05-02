"""PMA tail event types, rendering helpers, and status snapshot dataclasses.

Extracted from ``pma_thread_commands.py`` to reduce complexity in the command
module while keeping all tail-event schema-drift normalization in one place.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from .pma_control_plane import (
    coerce_optional_int as _coerce_optional_int,
)
from .pma_control_plane import (
    format_resource_owner_label as _format_resource_owner_label,
)

_MAX_TAIL_DISPLAY_FETCH_LIMIT = 200
_NOISY_DECODER_METHODS = frozenset(
    {
        "server.heartbeat",
        "server.connected",
        "session.updated",
        "session.diff",
    }
)


class PmaVerbosityLevel(str, Enum):
    INFO = "info"
    DEBUG = "debug"


def format_seconds(seconds: Optional[int]) -> str:
    if seconds is None:
        return "-"
    value = max(0, int(seconds))
    if value < 60:
        return f"{value}s"
    minutes, sec = divmod(value, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, rem_minutes = divmod(minutes, 60)
    return f"{hours}h{rem_minutes}m"


def format_received_at_label(value: Any) -> str:
    result = _format_timestamp(str(value or "").strip())
    return result or "-"


def _format_timestamp(timestamp: str) -> str:
    if not timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return timestamp


def _format_event_id_range(
    start_event_id: Optional[int], end_event_id: Optional[int]
) -> str:
    if not isinstance(start_event_id, int) or start_event_id <= 0:
        return ""
    if isinstance(end_event_id, int) and end_event_id > start_event_id:
        return f"#{start_event_id}-#{end_event_id} "
    return f"#{start_event_id} "


def format_rendered_tail_event_line(event: "PmaRenderedTailEvent") -> str:
    ts_out = _format_timestamp(event.received_at)
    prefix = f"[{ts_out}] " if ts_out else ""
    id_part = _format_event_id_range(event.start_event_id, event.end_event_id)
    line = f"{prefix}{id_part}{event.event_type}: {event.summary}".rstrip()
    if event.count > 1 and event.event_type != "assistant_update":
        line = f"{line} (x{event.count})"
    return line.rstrip()


def format_tail_event_line(event: dict[str, Any]) -> str:
    parsed_event = PmaTailEvent.from_dict(event)
    if parsed_event is None:
        return ""
    return format_rendered_tail_event_line(
        PmaRenderedTailEvent.from_tail_event(parsed_event)
    )


def _noisy_decoder_method(summary: str) -> Optional[str]:
    prefix = "No decoder for method:"
    if not summary.startswith(prefix):
        return None
    method = summary[len(prefix) :].strip().lower()
    return method or None


def _should_suppress_info_tail_event(event: "PmaTailEvent") -> bool:
    method = _noisy_decoder_method(event.summary)
    return method in _NOISY_DECODER_METHODS


def display_tail_fetch_limit(
    *,
    limit: int,
    level: PmaVerbosityLevel,
    output_json: bool,
) -> int:
    if output_json or level == PmaVerbosityLevel.DEBUG:
        return limit
    return max(limit, _MAX_TAIL_DISPLAY_FETCH_LIMIT)


def display_tail_events(
    events: tuple["PmaTailEvent", ...],
    *,
    level: PmaVerbosityLevel,
    limit: Optional[int] = None,
) -> tuple["PmaRenderedTailEvent", ...]:
    if level == PmaVerbosityLevel.DEBUG:
        rendered = tuple(
            PmaRenderedTailEvent.from_tail_event(event) for event in events
        )
    else:
        rendered = _collapse_info_events(events)
    if limit is None or limit <= 0:
        return rendered
    return rendered[-limit:]


def _try_merge_event(
    previous: "PmaRenderedTailEvent", event: "PmaTailEvent"
) -> Optional["PmaRenderedTailEvent"]:
    if (
        previous.event_type == "assistant_update"
        and event.event_type == "assistant_update"
    ):
        return previous.merged_with(event, summary=event.summary)
    if previous.dedupe_key == (event.event_type, event.summary):
        return previous.merged_with(event)
    return None


def _collapse_info_events(
    events: tuple["PmaTailEvent", ...],
) -> tuple["PmaRenderedTailEvent", ...]:
    collapsed: list[PmaRenderedTailEvent] = []
    for event in events:
        if _should_suppress_info_tail_event(event):
            continue
        rendered = PmaRenderedTailEvent.from_tail_event(event)
        if collapsed:
            merged = _try_merge_event(collapsed[-1], event)
            if merged is not None:
                collapsed[-1] = merged
                continue
        collapsed.append(rendered)
    return tuple(collapsed)


@dataclass(frozen=True)
class PmaTailEvent:
    event_type: str
    summary: str
    received_at: str
    event_id: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Any) -> Optional["PmaTailEvent"]:
        if not isinstance(data, dict):
            return None
        event_id = data.get("event_id")
        normalized_event_id = event_id if isinstance(event_id, int) else None
        return cls(
            event_type=str(data.get("event_type") or "event"),
            summary=str(data.get("summary") or ""),
            received_at=str(data.get("received_at") or ""),
            event_id=normalized_event_id,
        )


@dataclass(frozen=True)
class PmaRenderedTailEvent:
    event_type: str
    summary: str
    received_at: str
    start_event_id: Optional[int] = None
    end_event_id: Optional[int] = None
    count: int = 1

    @classmethod
    def from_tail_event(cls, event: PmaTailEvent) -> "PmaRenderedTailEvent":
        return cls(
            event_type=event.event_type,
            summary=event.summary,
            received_at=event.received_at,
            start_event_id=event.event_id,
            end_event_id=event.event_id,
        )

    @property
    def dedupe_key(self) -> tuple[str, str]:
        return (self.event_type, self.summary)

    def merged_with(
        self,
        event: PmaTailEvent,
        *,
        summary: Optional[str] = None,
    ) -> "PmaRenderedTailEvent":
        return PmaRenderedTailEvent(
            event_type=self.event_type,
            summary=self.summary if summary is None else summary,
            received_at=self.received_at or event.received_at,
            start_event_id=self.start_event_id,
            end_event_id=event.event_id or self.end_event_id,
            count=self.count + 1,
        )


class PmaInfoTailStreamRenderer:
    def __init__(self) -> None:
        self._pending_assistant: Optional[PmaRenderedTailEvent] = None
        self._initial_replay_keys: deque[tuple[str, str]] = deque()
        self._last_visible_key: Optional[tuple[str, str]] = None

    def note_snapshot_events(self, events: tuple[PmaRenderedTailEvent, ...]) -> None:
        self._initial_replay_keys = deque(event.dedupe_key for event in events)
        if events:
            self._last_visible_key = events[-1].dedupe_key

    def consume(self, event: PmaTailEvent) -> list[str]:
        lines: list[str] = []
        if _should_suppress_info_tail_event(event):
            return lines
        if (
            self._pending_assistant is not None
            and event.event_type != "assistant_update"
        ):
            flushed = self._emit_rendered(self._pending_assistant)
            if flushed is not None:
                lines.append(flushed)
            self._pending_assistant = None
        if event.event_type == "assistant_update":
            rendered = PmaRenderedTailEvent.from_tail_event(event)
            if self._pending_assistant is None:
                self._pending_assistant = rendered
            else:
                self._pending_assistant = self._pending_assistant.merged_with(
                    event,
                    summary=event.summary,
                )
            return lines
        rendered = PmaRenderedTailEvent.from_tail_event(event)
        emitted = self._emit_rendered(rendered)
        if emitted is not None:
            lines.append(emitted)
        return lines

    def flush(self) -> list[str]:
        if self._pending_assistant is None:
            return []
        emitted = self._emit_rendered(self._pending_assistant)
        self._pending_assistant = None
        return [emitted] if emitted is not None else []

    def _emit_rendered(self, event: PmaRenderedTailEvent) -> Optional[str]:
        key = event.dedupe_key
        if self._initial_replay_keys:
            expected = self._initial_replay_keys[0]
            if key == expected:
                self._initial_replay_keys.popleft()
                self._last_visible_key = key
                return None
            self._initial_replay_keys.clear()
        if key == self._last_visible_key:
            return None
        self._last_visible_key = key
        return format_rendered_tail_event_line(event)


@dataclass(frozen=True)
class PmaLastToolSnapshot:
    name: str
    status: str
    in_flight: bool

    @classmethod
    def from_dict(cls, data: Any) -> Optional["PmaLastToolSnapshot"]:
        if not isinstance(data, dict):
            return None
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        return cls(
            name=name,
            status=str(data.get("status") or "-"),
            in_flight=bool(data.get("in_flight")),
        )

    def render_line(self) -> str:
        return (
            "last_tool="
            + self.name
            + " status="
            + self.status
            + " in_flight="
            + ("yes" if self.in_flight else "no")
        )


@dataclass(frozen=True)
class PmaActiveTurnDiagnostics:
    request_kind: str
    model: str
    reasoning: str
    stalled: bool
    stream_available: bool
    prompt_preview: str
    last_event_type: str
    last_event_summary: str
    last_event_at: Any
    backend_thread_id: str
    backend_turn_id: str
    stall_reason: str

    @classmethod
    def from_dict(cls, data: Any) -> Optional["PmaActiveTurnDiagnostics"]:
        if not isinstance(data, dict):
            return None
        return cls(
            request_kind=str(data.get("request_kind") or "-"),
            model=str(data.get("model") or "-"),
            reasoning=str(data.get("reasoning") or "-"),
            stalled=bool(data.get("stalled")),
            stream_available=bool(data.get("stream_available")),
            prompt_preview=str(data.get("prompt_preview") or "").strip(),
            last_event_type=str(data.get("last_event_type") or "").strip(),
            last_event_summary=str(data.get("last_event_summary") or "").strip(),
            last_event_at=data.get("last_event_at"),
            backend_thread_id=str(data.get("backend_thread_id") or "").strip(),
            backend_turn_id=str(data.get("backend_turn_id") or "").strip(),
            stall_reason=str(data.get("stall_reason") or "").strip(),
        )

    def render_lines(self) -> list[str]:
        lines = [
            "active_turn: "
            f"kind={self.request_kind} model={self.model} reasoning={self.reasoning} "
            f"stream={'yes' if self.stream_available else 'no'} "
            f"stalled={'yes' if self.stalled else 'no'}"
        ]
        if self.prompt_preview:
            lines.append(f"prompt: {self.prompt_preview}")
        if self.last_event_type or self.last_event_summary:
            lines.append(
                "last_event: "
                + (self.last_event_type or "-")
                + " @"
                + format_received_at_label(self.last_event_at)
                + (f" {self.last_event_summary}" if self.last_event_summary else "")
            )
        if self.backend_thread_id or self.backend_turn_id:
            lines.append(
                "backend: "
                f"thread={self.backend_thread_id or '-'} "
                f"turn={self.backend_turn_id or '-'}"
            )
        if self.stall_reason:
            lines.append(f"stall_reason: {self.stall_reason}")
        return lines


@dataclass(frozen=True)
class PmaTailSnapshot:
    managed_turn_id: str
    turn_status: str
    activity: str
    phase: str
    elapsed_seconds: Optional[int]
    idle_seconds: Optional[int]
    guidance: str
    diagnostics: Optional[PmaActiveTurnDiagnostics]
    last_tool: Optional[PmaLastToolSnapshot]
    lifecycle_events: tuple[str, ...]
    events: tuple[PmaTailEvent, ...]

    @classmethod
    def from_dict(cls, data: Any) -> "PmaTailSnapshot":
        payload = data if isinstance(data, dict) else {}
        lifecycle = payload.get("lifecycle_events")
        raw_events = payload.get("events")
        return cls(
            managed_turn_id=str(payload.get("managed_turn_id") or "-"),
            turn_status=str(payload.get("turn_status") or "none"),
            activity=str(payload.get("activity") or "idle"),
            phase=str(payload.get("phase") or "-"),
            elapsed_seconds=_coerce_optional_int(payload.get("elapsed_seconds")),
            idle_seconds=_coerce_optional_int(payload.get("idle_seconds")),
            guidance=str(payload.get("guidance") or "").strip(),
            diagnostics=PmaActiveTurnDiagnostics.from_dict(
                payload.get("active_turn_diagnostics")
            ),
            last_tool=PmaLastToolSnapshot.from_dict(payload.get("last_tool")),
            lifecycle_events=tuple(
                str(item) for item in (lifecycle if isinstance(lifecycle, list) else [])
            ),
            events=tuple(
                event
                for item in (raw_events if isinstance(raw_events, list) else [])
                if (event := PmaTailEvent.from_dict(item)) is not None
            ),
        )

    def display_events(
        self,
        *,
        level: PmaVerbosityLevel,
        limit: Optional[int] = None,
    ) -> tuple[PmaRenderedTailEvent, ...]:
        return display_tail_events(self.events, level=level, limit=limit)

    def render_lines(
        self,
        *,
        level: PmaVerbosityLevel = PmaVerbosityLevel.INFO,
        limit: Optional[int] = None,
    ) -> list[str]:
        lines = [
            "managed_turn_id="
            + self.managed_turn_id
            + " turn_status="
            + self.turn_status
            + " activity="
            + self.activity
            + " phase="
            + self.phase
            + " elapsed="
            + format_seconds(self.elapsed_seconds)
            + " idle="
            + format_seconds(self.idle_seconds)
        ]
        if self.guidance:
            lines.append(f"guidance: {self.guidance}")
        if self.diagnostics is not None:
            lines.extend(self.diagnostics.render_lines())
        if self.last_tool is not None:
            lines.append(self.last_tool.render_line())
        if self.lifecycle_events:
            lines.append("lifecycle: " + ", ".join(self.lifecycle_events))
        visible_events = self.display_events(level=level, limit=limit)
        if not visible_events:
            lines.append("No tail events.")
            if self.turn_status == "running" and self.idle_seconds is not None:
                idle_seconds = int(self.idle_seconds or 0)
                if idle_seconds >= 30:
                    lines.append(f"No events for {idle_seconds}s (possibly stalled).")
            return lines
        lines.extend(format_rendered_tail_event_line(event) for event in visible_events)
        return [line for line in lines if line]


@dataclass(frozen=True)
class PmaQueuedTurnSnapshot:
    managed_turn_id: str
    request_kind: str
    state: str
    position: Optional[int]
    enqueued_at: str
    prompt_preview: str

    @classmethod
    def from_dict(cls, data: Any) -> Optional["PmaQueuedTurnSnapshot"]:
        if not isinstance(data, dict):
            return None
        return cls(
            managed_turn_id=str(data.get("managed_turn_id") or "-"),
            request_kind=str(data.get("request_kind") or "-"),
            state=str(data.get("state") or "-"),
            position=_coerce_optional_int(data.get("position")),
            enqueued_at=str(data.get("enqueued_at") or "-"),
            prompt_preview=str(data.get("prompt_preview") or "")[:80],
        )

    def render_line(self) -> str:
        position = self.position if self.position is not None else "-"
        return (
            "queued_turn_id="
            + self.managed_turn_id
            + " position="
            + str(position)
            + " state="
            + self.state
            + " kind="
            + self.request_kind
            + " enqueued="
            + self.enqueued_at
            + " prompt="
            + self.prompt_preview
        )


@dataclass(frozen=True)
class PmaThreadStatusSnapshot:
    managed_thread_id: str
    agent: str
    owner_label: str
    operator_status: str
    runtime_status: str
    lifecycle_status: str
    is_alive: bool
    status_reason: str
    managed_turn_id: str
    turn_status: str
    activity: str
    phase: str
    elapsed_seconds: Optional[int]
    idle_seconds: Optional[int]
    guidance: str
    diagnostics: Optional[PmaActiveTurnDiagnostics]
    last_tool: Optional[PmaLastToolSnapshot]
    recent_progress: tuple[PmaTailEvent, ...]
    latest_turn_id: str
    latest_assistant_text: str
    latest_output_excerpt: str
    queue_depth: int
    queued_turns: tuple[PmaQueuedTurnSnapshot, ...]

    @classmethod
    def from_dict(cls, data: Any) -> "PmaThreadStatusSnapshot":
        from ...core.managed_thread_status import derive_managed_thread_operator_status

        payload = data if isinstance(data, dict) else {}
        raw_thread = payload.get("thread")
        thread: dict[str, Any] = raw_thread if isinstance(raw_thread, dict) else {}
        raw_turn = payload.get("turn")
        turn: dict[str, Any] = raw_turn if isinstance(raw_turn, dict) else {}
        raw_thread_status = str(
            payload.get("status")
            or thread.get("normalized_status")
            or thread.get("status")
            or "-"
        )
        queue_depth_raw = payload.get("queue_depth")
        recent_progress = payload.get("recent_progress")
        queued_turns = payload.get("queued_turns")
        return cls(
            managed_thread_id=str(payload.get("managed_thread_id") or ""),
            agent=str(thread.get("agent") or "-"),
            owner_label=_format_resource_owner_label(thread),
            operator_status=str(payload.get("operator_status") or "").strip()
            or derive_managed_thread_operator_status(
                normalized_status=raw_thread_status,
                lifecycle_status=str(thread.get("lifecycle_status") or "-"),
            ),
            runtime_status=raw_thread_status,
            lifecycle_status=str(thread.get("lifecycle_status") or "-"),
            is_alive=bool(payload.get("is_alive")),
            status_reason=str(
                payload.get("status_reason")
                or payload.get("status_reason_code")
                or thread.get("status_reason_code")
                or thread.get("status_reason")
                or "-"
            ),
            managed_turn_id=str(turn.get("managed_turn_id") or "-"),
            turn_status=str(turn.get("status") or "-"),
            activity=str(turn.get("activity") or "-"),
            phase=str(turn.get("phase") or "-"),
            elapsed_seconds=_coerce_optional_int(turn.get("elapsed_seconds")),
            idle_seconds=_coerce_optional_int(turn.get("idle_seconds")),
            guidance=str(turn.get("guidance") or "").strip(),
            diagnostics=PmaActiveTurnDiagnostics.from_dict(
                payload.get("active_turn_diagnostics")
            ),
            last_tool=PmaLastToolSnapshot.from_dict(turn.get("last_tool")),
            recent_progress=tuple(
                event
                for item in (
                    recent_progress if isinstance(recent_progress, list) else []
                )
                if (event := PmaTailEvent.from_dict(item)) is not None
            ),
            latest_turn_id=str(payload.get("latest_turn_id") or "").strip(),
            latest_assistant_text=str(payload.get("latest_assistant_text") or ""),
            latest_output_excerpt=str(
                payload.get("latest_output_excerpt") or ""
            ).strip(),
            queue_depth=_coerce_optional_int(queue_depth_raw) or 0,
            queued_turns=tuple(
                turn_item
                for item in (queued_turns if isinstance(queued_turns, list) else [])
                if (turn_item := PmaQueuedTurnSnapshot.from_dict(item)) is not None
            ),
        )

    def display_recent_progress(
        self,
        *,
        level: PmaVerbosityLevel,
        limit: Optional[int] = None,
    ) -> tuple[PmaRenderedTailEvent, ...]:
        return display_tail_events(self.recent_progress, level=level, limit=limit)

    def render_lines(
        self,
        *,
        level: PmaVerbosityLevel = PmaVerbosityLevel.INFO,
        limit: Optional[int] = None,
    ) -> list[str]:
        lines = [
            " ".join(
                [
                    f"id={self.managed_thread_id}",
                    f"agent={self.agent}",
                    self.owner_label,
                    f"operator_status={self.operator_status}",
                    f"runtime_status={self.runtime_status}",
                    f"lifecycle_status={self.lifecycle_status}",
                    f"alive={'yes' if self.is_alive else 'no'}",
                ]
            ),
            f"status_reason={self.status_reason}",
            "managed_turn_id="
            + self.managed_turn_id
            + " turn_status="
            + self.turn_status
            + " activity="
            + self.activity
            + " phase="
            + self.phase
            + " elapsed="
            + format_seconds(self.elapsed_seconds)
            + " idle="
            + format_seconds(self.idle_seconds),
        ]
        if self.guidance:
            lines.append(f"guidance: {self.guidance}")
        if self.diagnostics is not None:
            lines.extend(self.diagnostics.render_lines())
        if self.last_tool is not None:
            lines.append(self.last_tool.render_line())
        visible_progress = self.display_recent_progress(level=level, limit=limit)
        if visible_progress:
            lines.append("recent progress:")
            lines.extend(
                format_rendered_tail_event_line(event) for event in visible_progress
            )
        else:
            lines.append("No recent progress events.")
        if self.queue_depth > 0:
            lines.append(f"queued={self.queue_depth}")
            lines.extend(item.render_line() for item in self.queued_turns[:5])
        if self.latest_output_excerpt:
            lines.append("assistant_text_excerpt:")
            lines.append(self.latest_output_excerpt)
        return [line for line in lines if line]
