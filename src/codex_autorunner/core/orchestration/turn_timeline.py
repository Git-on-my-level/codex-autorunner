from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
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
from ..text_utils import _json_dumps, _truncate_text
from .cold_trace_store import ColdTraceStore, ColdTraceWriter
from .execution_history import (
    CheckpointSignalStatus,
    ExecutionCheckpoint,
    ExecutionCheckpointSignal,
    build_hot_projection_envelope,
    route_run_event,
    timeline_hot_family_for_event_type,
)
from .execution_history_diagnostics import (
    log_dedupe,
    log_spill_to_cold,
    log_truncation,
)
from .sqlite import open_orchestration_sqlite

_EVENT_FAMILY = "turn.timeline"
_CHECKPOINT_PREVIEW_CHARS = 240
_HOT_STATE_NOTICE_MAX_CHARS = 1024
_HOT_STATE_OUTPUT_MAX_CHARS = 512
_HOT_FAMILY_ROW_LIMITS = {
    "tool_call": 128,
    "tool_result": 128,
    "output_delta": 200,
    "run_notice": 100,
    "token_usage": 64,
    "terminal": 8,
}
_COALESCED_NOTICE_KINDS = frozenset({"progress", "thinking"})
logger = logging.getLogger(__name__)


def iso_from_epoch_millis(epoch_millis: Any) -> Optional[str]:
    if not isinstance(epoch_millis, (int, float)):
        return None
    return (
        datetime.fromtimestamp(
            float(epoch_millis) / 1000.0,
            tz=timezone.utc,
        )
        .isoformat()
        .replace("+00:00", "Z")
    )


def _event_type_and_status(event: RunEvent) -> tuple[str, str]:
    if isinstance(event, Started):
        return "turn_started", "recorded"
    if isinstance(event, OutputDelta):
        return "output_delta", "recorded"
    if isinstance(event, ToolCall):
        return "tool_call", "recorded"
    if isinstance(event, ToolResult):
        return "tool_result", event.status or "recorded"
    if isinstance(event, ApprovalRequested):
        return "approval_requested", "recorded"
    if isinstance(event, TokenUsage):
        return "token_usage", "recorded"
    if isinstance(event, RunNotice):
        return "run_notice", "recorded"
    if isinstance(event, Completed):
        return "turn_completed", "ok"
    if isinstance(event, Failed):
        return "turn_failed", "error"
    return "unknown", "recorded"


def _bounded_state_text(value: str, limit: int) -> str:
    return _truncate_text(str(value or ""), limit, suffix="")


def _append_preview(preview: str, current_char_count: int, text: str) -> str:
    if not text:
        return preview
    if current_char_count >= _CHECKPOINT_PREVIEW_CHARS:
        return preview
    remaining = _CHECKPOINT_PREVIEW_CHARS - max(current_char_count, 0)
    if remaining <= 0:
        return preview
    return f"{preview}{text[:remaining]}"


def _decode_payload_json(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _collect_execution_family_hot_rows(
    hub_root: Any, execution_id: str
) -> dict[str, int]:
    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute(
            """
            SELECT event_type, COUNT(*) AS cnt
              FROM orch_event_projections
             INDEXED BY idx_orch_event_projections_family_execution_order
             WHERE event_family = ?
               AND execution_id = ?
             GROUP BY event_type
            """,
            (_EVENT_FAMILY, execution_id),
        ).fetchall()
    family_hot_rows: dict[str, int] = {}
    for row in rows:
        family = timeline_hot_family_for_event_type(row["event_type"])
        if family:
            family_hot_rows[family] = family_hot_rows.get(family, 0) + int(
                row["cnt"] or 0
            )
    return family_hot_rows


def _seed_notice_memory(hub_root: Any, execution_id: str) -> dict[str, str]:
    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute(
            """
            SELECT payload_json
              FROM orch_event_projections
             INDEXED BY idx_orch_event_projections_family_execution_order
             WHERE event_family = ?
               AND execution_id = ?
               AND event_type = 'run_notice'
             ORDER BY event_id ASC
            """,
            (_EVENT_FAMILY, execution_id),
        ).fetchall()
    last_notice_by_kind: dict[str, str] = {}
    for row in rows:
        payload = _decode_payload_json(row["payload_json"])
        event_payload = payload.get("event")
        if not isinstance(event_payload, dict):
            continue
        kind = str(event_payload.get("kind") or "").strip()
        if kind:
            last_notice_by_kind[kind] = _bounded_state_text(
                str(event_payload.get("message") or ""),
                _HOT_STATE_NOTICE_MAX_CHARS,
            )
    return last_notice_by_kind


def _seed_output_memory(hub_root: Any, execution_id: str) -> dict[str, str]:
    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute(
            """
            SELECT payload_json
              FROM orch_event_projections
             INDEXED BY idx_orch_event_projections_family_execution_order
             WHERE event_family = ?
               AND execution_id = ?
               AND event_type = 'output_delta'
             ORDER BY event_id ASC
            """,
            (_EVENT_FAMILY, execution_id),
        ).fetchall()
    last_output_by_type: dict[str, str] = {}
    for row in rows:
        payload = _decode_payload_json(row["payload_json"])
        event_payload = payload.get("event")
        if not isinstance(event_payload, dict):
            continue
        delta_type = str(event_payload.get("delta_type") or "").strip()
        if delta_type:
            last_output_by_type[delta_type] = _bounded_state_text(
                str(event_payload.get("content") or ""),
                _HOT_STATE_OUTPUT_MAX_CHARS,
            )
    return last_output_by_type


class _HotProjectionState:
    def __init__(self, checkpoint: Optional[ExecutionCheckpoint]) -> None:
        checkpoint_state = (
            dict(checkpoint.hot_projection_state)
            if checkpoint and isinstance(checkpoint.hot_projection_state, dict)
            else {}
        )
        self.family_hot_rows: dict[str, int] = {
            str(key): max(int(value), 0)
            for key, value in dict(
                checkpoint_state.get("family_hot_rows") or {}
            ).items()
        }
        self.deduped_counts: dict[str, int] = {
            str(key): max(int(value), 0)
            for key, value in dict(checkpoint_state.get("deduped_counts") or {}).items()
        }
        self.spilled_counts: dict[str, int] = {
            str(key): max(int(value), 0)
            for key, value in dict(checkpoint_state.get("spilled_counts") or {}).items()
        }
        self.spilled_without_cold_trace_counts: dict[str, int] = {
            str(key): max(int(value), 0)
            for key, value in dict(
                checkpoint_state.get("spilled_without_cold_trace_counts") or {}
            ).items()
        }
        self.last_notice_by_kind: dict[str, str] = {
            str(key): _bounded_state_text(str(value or ""), _HOT_STATE_NOTICE_MAX_CHARS)
            for key, value in dict(
                checkpoint_state.get("last_notice_by_kind") or {}
            ).items()
        }
        self.last_output_by_type: dict[str, str] = {
            str(key): _bounded_state_text(str(value or ""), _HOT_STATE_OUTPUT_MAX_CHARS)
            for key, value in dict(
                checkpoint_state.get("last_output_by_type") or {}
            ).items()
        }

    def seed_from_db(self, hub_root: Any, execution_id: str) -> None:
        if any(
            (
                self.family_hot_rows,
                self.last_notice_by_kind,
                self.last_output_by_type,
            )
        ):
            return
        self.family_hot_rows = _collect_execution_family_hot_rows(
            hub_root, execution_id
        )
        self.last_notice_by_kind = _seed_notice_memory(hub_root, execution_id)
        self.last_output_by_type = _seed_output_memory(hub_root, execution_id)

    def note_hot_persisted(self, family: str) -> None:
        self.family_hot_rows[family] = self.family_hot_rows.get(family, 0) + 1

    def note_deduped(self, family: str) -> None:
        self.deduped_counts[family] = self.deduped_counts.get(family, 0) + 1

    def note_spilled(self, family: str, *, has_cold_trace: bool) -> None:
        self.spilled_counts[family] = self.spilled_counts.get(family, 0) + 1
        if not has_cold_trace:
            self.spilled_without_cold_trace_counts[family] = (
                self.spilled_without_cold_trace_counts.get(family, 0) + 1
            )

    def update_notice_memory(self, kind: str, message: str) -> None:
        if kind:
            self.last_notice_by_kind[kind] = _bounded_state_text(
                message, _HOT_STATE_NOTICE_MAX_CHARS
            )

    def update_output_memory(self, delta_type: str, content: str) -> None:
        if delta_type:
            self.last_output_by_type[delta_type] = _bounded_state_text(
                content, _HOT_STATE_OUTPUT_MAX_CHARS
            )

    def allows_hot_persist(self, family: str) -> bool:
        limit = _HOT_FAMILY_ROW_LIMITS.get(family)
        if limit is None:
            return True
        return self.family_hot_rows.get(family, 0) < limit

    def as_checkpoint_state(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "family_hot_rows": dict(sorted(self.family_hot_rows.items())),
        }
        if self.deduped_counts:
            payload["deduped_counts"] = dict(sorted(self.deduped_counts.items()))
        if self.spilled_counts:
            payload["spilled_counts"] = dict(sorted(self.spilled_counts.items()))
        if self.spilled_without_cold_trace_counts:
            payload["spilled_without_cold_trace_counts"] = dict(
                sorted(self.spilled_without_cold_trace_counts.items())
            )
        if self.last_notice_by_kind:
            payload["last_notice_by_kind"] = dict(
                sorted(self.last_notice_by_kind.items())
            )
        if self.last_output_by_type:
            payload["last_output_by_type"] = dict(
                sorted(self.last_output_by_type.items())
            )
        return payload


class _CheckpointAccumulator:
    def __init__(
        self,
        checkpoint: Optional[ExecutionCheckpoint],
        *,
        execution_id: str,
        thread_target_id: Optional[str],
        metadata: dict[str, Any],
        trace_manifest_id: Optional[str],
        hot_state: _HotProjectionState,
    ) -> None:
        self.execution_id = execution_id
        self.thread_target_id = thread_target_id or (
            checkpoint.thread_target_id if checkpoint is not None else None
        )
        self.backend_thread_id = str(
            metadata.get("backend_thread_id") or ""
        ).strip() or (checkpoint.backend_thread_id if checkpoint is not None else None)
        self.backend_turn_id = str(metadata.get("backend_turn_id") or "").strip() or (
            checkpoint.backend_turn_id if checkpoint is not None else None
        )
        self.completion_source = str(
            metadata.get("completion_source") or ""
        ).strip() or (checkpoint.completion_source if checkpoint is not None else None)
        self.assistant_text_preview = (
            checkpoint.assistant_text_preview if checkpoint is not None else ""
        )
        self.assistant_char_count = (
            int(checkpoint.assistant_char_count) if checkpoint is not None else 0
        )
        self.progress_text_preview = (
            checkpoint.progress_text_preview if checkpoint is not None else ""
        )
        self.progress_text_kind = (
            checkpoint.progress_text_kind if checkpoint is not None else None
        )
        self.progress_char_count = (
            int(checkpoint.progress_char_count) if checkpoint is not None else 0
        )
        self.last_runtime_method = (
            checkpoint.last_runtime_method if checkpoint is not None else None
        )
        self.last_progress_at = (
            checkpoint.last_progress_at if checkpoint is not None else None
        )
        self.transport_status = str(metadata.get("status") or "").strip() or (
            checkpoint.transport_status if checkpoint is not None else None
        )
        self.transport_request_return_timestamp = (
            checkpoint.transport_request_return_timestamp
            if checkpoint is not None
            else None
        )
        self.token_usage = (
            dict(checkpoint.token_usage)
            if checkpoint is not None and isinstance(checkpoint.token_usage, dict)
            else None
        )
        self.failure_cause = (
            checkpoint.failure_cause if checkpoint is not None else None
        )
        self.raw_event_count = (
            int(checkpoint.raw_event_count) if checkpoint is not None else 0
        )
        self.projection_event_cursor = (
            int(checkpoint.projection_event_cursor) if checkpoint is not None else 0
        )
        self.reasoning_buffer_count = (
            int(checkpoint.reasoning_buffer_count) if checkpoint is not None else 0
        )
        self.trace_manifest_id = trace_manifest_id or (
            checkpoint.trace_manifest_id if checkpoint is not None else None
        )
        self._hot_state = hot_state
        self._terminal_signals: list[ExecutionCheckpointSignal] = list(
            checkpoint.terminal_signals if checkpoint is not None else ()
        )
        self._terminal_signal_keys: set[tuple[str, CheckpointSignalStatus, str]] = {
            (signal.source, signal.status, signal.timestamp)
            for signal in self._terminal_signals
        }
        self.status = (
            str(metadata.get("status") or "").strip()
            or (checkpoint.status if checkpoint is not None else "running")
            or "running"
        )

    def note_event(self, event: RunEvent, *, event_type: str, event_index: int) -> None:
        self.projection_event_cursor = max(self.projection_event_cursor, event_index)
        timestamp = str(getattr(event, "timestamp", "") or "").strip()
        if timestamp:
            self.last_progress_at = timestamp
        self.last_runtime_method = event_type

        if isinstance(event, OutputDelta):
            if event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE:
                self.assistant_text_preview = _truncate_text(
                    event.content, _CHECKPOINT_PREVIEW_CHARS
                )
                self.assistant_char_count = len(event.content)
            elif event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM:
                self.assistant_text_preview = _append_preview(
                    self.assistant_text_preview,
                    self.assistant_char_count,
                    event.content,
                )
                self.assistant_char_count += len(event.content)
            return

        if isinstance(event, RunNotice):
            self.progress_text_preview = _truncate_text(
                event.message, _CHECKPOINT_PREVIEW_CHARS
            )
            self.progress_text_kind = event.kind or None
            self.progress_char_count = len(event.message)
            if event.kind == "thinking":
                self.reasoning_buffer_count = max(self.reasoning_buffer_count, 1)
            return

        if isinstance(event, ApprovalRequested):
            self.progress_text_preview = _truncate_text(
                event.description, _CHECKPOINT_PREVIEW_CHARS
            )
            self.progress_text_kind = "approval"
            self.progress_char_count = len(event.description)
            return

        if isinstance(event, TokenUsage):
            self.token_usage = dict(event.usage)
            return

        if isinstance(event, Completed):
            self.status = "ok"
            self.assistant_text_preview = _truncate_text(
                event.final_message, _CHECKPOINT_PREVIEW_CHARS
            )
            self.assistant_char_count = len(event.final_message)
            self._note_terminal_signal("turn_completed", "ok", timestamp)
            return

        if isinstance(event, Failed):
            self.status = "error"
            self.failure_cause = event.error_message
            self._note_terminal_signal("turn_failed", "error", timestamp)
            return

    def _note_terminal_signal(
        self, source: str, status: CheckpointSignalStatus, timestamp: str
    ) -> None:
        key = (source, status, timestamp)
        if key in self._terminal_signal_keys:
            return
        self._terminal_signal_keys.add(key)
        self._terminal_signals.append(
            ExecutionCheckpointSignal(
                source=source,
                status=status,
                timestamp=timestamp,
            )
        )

    def build(self) -> ExecutionCheckpoint:
        return ExecutionCheckpoint(
            status=self.status or "running",
            execution_id=self.execution_id,
            thread_target_id=self.thread_target_id,
            backend_thread_id=self.backend_thread_id or None,
            backend_turn_id=self.backend_turn_id or None,
            completion_source=self.completion_source or None,
            assistant_text_preview=self.assistant_text_preview,
            assistant_char_count=max(self.assistant_char_count, 0),
            progress_text_preview=self.progress_text_preview,
            progress_text_kind=self.progress_text_kind,
            progress_char_count=max(self.progress_char_count, 0),
            last_runtime_method=self.last_runtime_method,
            last_progress_at=self.last_progress_at,
            transport_status=self.transport_status,
            transport_request_return_timestamp=self.transport_request_return_timestamp,
            token_usage=self.token_usage,
            failure_cause=self.failure_cause,
            raw_event_count=max(self.raw_event_count, 0),
            projection_event_cursor=max(self.projection_event_cursor, 0),
            reasoning_buffer_count=max(self.reasoning_buffer_count, 0),
            hot_projection_state=self._hot_state.as_checkpoint_state(),
            terminal_signals=tuple(self._terminal_signals),
            trace_manifest_id=self.trace_manifest_id,
        )


def _maybe_coalesce_hot_event(
    event: RunEvent,
    hot_state: _HotProjectionState,
) -> tuple[Optional[RunEvent], dict[str, Any]]:
    metadata: dict[str, Any] = {}
    if isinstance(event, RunNotice):
        kind = str(event.kind or "").strip()
        message = str(event.message or "")
        previous = hot_state.last_notice_by_kind.get(kind, "")
        if previous and message == previous:
            hot_state.update_notice_memory(kind, message)
            hot_state.note_deduped("run_notice")
            return None, {"hot_duplicate_notice": True}
        if (
            kind in _COALESCED_NOTICE_KINDS
            and previous
            and len(message) > len(previous)
            and message.startswith(previous)
        ):
            delta = message[len(previous) :]
            if not delta:
                hot_state.update_notice_memory(kind, message)
                hot_state.note_deduped("run_notice")
                return None, {"hot_duplicate_notice": True}
            hot_state.update_notice_memory(kind, message)
            metadata.update(
                {
                    "hot_coalesced_to_delta": True,
                    "hot_original_message_chars": len(message),
                    "hot_previous_message_chars": len(previous),
                    "details_in_cold_trace": True,
                }
            )
            return (
                RunNotice(
                    timestamp=event.timestamp,
                    kind=event.kind,
                    message=delta,
                    data=dict(event.data),
                ),
                metadata,
            )
        hot_state.update_notice_memory(kind, message)
        return event, metadata

    if isinstance(event, OutputDelta):
        delta_type = str(event.delta_type or "").strip()
        content = str(event.content or "")
        hot_state.update_output_memory(delta_type, content)
    return event, metadata


def _log_hot_projection_truncation(
    *,
    execution_id: str,
    event_family: str,
    event: RunEvent,
    hot_event_payload: dict[str, Any],
    contract: str,
) -> None:
    if hot_event_payload.get("content_truncated") and isinstance(event, OutputDelta):
        log_truncation(
            execution_id=execution_id,
            event_family=event_family,
            original_chars=int(
                hot_event_payload.get("content_chars") or len(event.content)
            ),
            truncated_chars=len(str(hot_event_payload.get("content") or "")),
            contract=contract,
        )
    if hot_event_payload.get("final_message_truncated") and isinstance(
        event, Completed
    ):
        log_truncation(
            execution_id=execution_id,
            event_family=event_family,
            original_chars=int(
                hot_event_payload.get("final_message_chars") or len(event.final_message)
            ),
            truncated_chars=len(str(hot_event_payload.get("final_message") or "")),
            contract=contract,
        )
    if hot_event_payload.get("error_message_truncated") and isinstance(event, Failed):
        log_truncation(
            execution_id=execution_id,
            event_family=event_family,
            original_chars=int(
                hot_event_payload.get("error_message_chars") or len(event.error_message)
            ),
            truncated_chars=len(str(hot_event_payload.get("error_message") or "")),
            contract=contract,
        )
    if hot_event_payload.get("tool_input_truncated") and isinstance(event, ToolCall):
        log_truncation(
            execution_id=execution_id,
            event_family=event_family,
            original_chars=len(str(event.tool_input)),
            truncated_chars=len(str(hot_event_payload.get("tool_input_preview") or "")),
            contract=contract,
        )
    if hot_event_payload.get("result_truncated") and isinstance(event, ToolResult):
        log_truncation(
            execution_id=execution_id,
            event_family=event_family,
            original_chars=len(str(event.result)),
            truncated_chars=len(str(hot_event_payload.get("result_preview") or "")),
            contract=contract,
        )
    if hot_event_payload.get("error_truncated") and isinstance(event, ToolResult):
        log_truncation(
            execution_id=execution_id,
            event_family=event_family,
            original_chars=len(str(event.error)),
            truncated_chars=len(str(hot_event_payload.get("error_preview") or "")),
            contract=contract,
        )
    if hot_event_payload.get("message_truncated") and isinstance(event, RunNotice):
        log_truncation(
            execution_id=execution_id,
            event_family=event_family,
            original_chars=int(
                hot_event_payload.get("message_chars") or len(event.message)
            ),
            truncated_chars=len(str(hot_event_payload.get("message") or "")),
            contract=contract,
        )
    if hot_event_payload.get("data_truncated") and isinstance(event, RunNotice):
        log_truncation(
            execution_id=execution_id,
            event_family=event_family,
            original_chars=len(str(event.data)),
            truncated_chars=len(str(hot_event_payload.get("data_preview") or "")),
            contract=contract,
        )


def _append_event_to_cold_trace(
    *,
    cold_trace_writer: ColdTraceWriter,
    event: RunEvent,
    event_type: str,
    routing: Any,
) -> None:
    try:
        cold_trace_writer.append(
            event_family=routing.event_family,
            event_type=event_type,
            payload=asdict(event),
        )
    except Exception as exc:
        raise RuntimeError(
            "Failed to append execution event to the cold trace"
        ) from exc


def append_turn_events_to_cold_trace(
    cold_trace_writer: ColdTraceWriter,
    *,
    events: Iterable[RunEvent],
) -> int:
    count = 0
    for event in events:
        event_type, _status = _event_type_and_status(event)
        routing = route_run_event(event)
        if not routing.capture_cold_trace:
            continue
        _append_event_to_cold_trace(
            cold_trace_writer=cold_trace_writer,
            event=event,
            event_type=event_type,
            routing=routing,
        )
        count += 1
    return count


def persist_turn_timeline(
    hub_root,
    *,
    execution_id: str,
    target_kind: Optional[str],
    target_id: Optional[str],
    repo_id: Optional[str] = None,
    run_id: Optional[str] = None,
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    events: Iterable[RunEvent],
    start_index: int = 1,
    cold_trace_writer: Optional[ColdTraceWriter] = None,
) -> int:
    normalized_execution_id = str(execution_id or "").strip()
    if not normalized_execution_id:
        return 0

    base_metadata = dict(metadata or {})
    trace_manifest_id = (
        str(base_metadata.get("trace_manifest_id") or "").strip() or None
    )
    if cold_trace_writer is not None and trace_manifest_id is None:
        trace_manifest_id = cold_trace_writer.trace_id
    if trace_manifest_id is not None:
        base_metadata["trace_manifest_id"] = trace_manifest_id
    store = ColdTraceStore(hub_root)
    existing_checkpoint = store.load_checkpoint(normalized_execution_id)
    hot_state = _HotProjectionState(existing_checkpoint)
    hot_state.seed_from_db(hub_root, normalized_execution_id)
    checkpoint_accumulator = _CheckpointAccumulator(
        existing_checkpoint,
        execution_id=normalized_execution_id,
        thread_target_id=target_id if target_kind == "thread_target" else None,
        metadata=base_metadata,
        trace_manifest_id=trace_manifest_id,
        hot_state=hot_state,
    )
    count = 0
    next_index = max(int(start_index or 1), 1)
    cold_trace_available = bool(
        cold_trace_writer is not None
        and getattr(cold_trace_writer, "is_writable", True)
    )
    with open_orchestration_sqlite(hub_root) as conn:
        for event in events:
            index = next_index + count
            event_type, status = _event_type_and_status(event)
            routing = route_run_event(event)
            checkpoint_accumulator.note_event(
                event,
                event_type=event_type,
                event_index=index,
            )

            if (
                cold_trace_available
                and cold_trace_writer is not None
                and routing.capture_cold_trace
            ):
                try:
                    _append_event_to_cold_trace(
                        cold_trace_writer=cold_trace_writer,
                        event=event,
                        event_type=event_type,
                        routing=routing,
                    )
                except Exception:
                    cold_trace_available = False
                    base_metadata.pop("trace_manifest_id", None)
                    checkpoint_accumulator.trace_manifest_id = None
                    disable_writer = getattr(cold_trace_writer, "disable", None)
                    if callable(disable_writer):
                        disable_writer()
                    logger.warning(
                        "Cold trace append failed; continuing with hot-only timeline persistence "
                        "(execution_id=%s, event_type=%s)",
                        normalized_execution_id,
                        event_type,
                        exc_info=True,
                    )

            count += 1
            if not routing.persist_hot_projection:
                continue
            family = routing.event_family
            hot_event, event_metadata = _maybe_coalesce_hot_event(event, hot_state)
            if hot_event is None:
                if event_metadata.get("hot_duplicate_notice"):
                    log_dedupe(
                        execution_id=normalized_execution_id,
                        event_family=family,
                        dedupe_reason="duplicate_notice",
                        deduped_count=hot_state.deduped_counts.get(family, 0),
                    )
                continue
            if not hot_state.allows_hot_persist(family):
                hot_state.note_spilled(
                    family,
                    has_cold_trace=bool(
                        cold_trace_available
                        and cold_trace_writer is not None
                        and routing.capture_cold_trace
                    ),
                )
                log_spill_to_cold(
                    execution_id=normalized_execution_id,
                    event_family=family,
                    has_cold_trace=bool(
                        cold_trace_available
                        and cold_trace_writer is not None
                        and routing.capture_cold_trace
                    ),
                    hot_rows_so_far=hot_state.family_hot_rows.get(family, 0),
                    hot_limit=_HOT_FAMILY_ROW_LIMITS.get(family, 0),
                )
                continue
            event_payload = build_hot_projection_envelope(
                event_index=index,
                event_type=event_type,
                event=hot_event,
                metadata={**base_metadata, **event_metadata},
                routing=routing,
            ).to_payload()
            hot_event_payload = event_payload.get("event")
            if isinstance(hot_event_payload, dict):
                _log_hot_projection_truncation(
                    execution_id=normalized_execution_id,
                    event_family=family,
                    event=hot_event,
                    hot_event_payload=hot_event_payload,
                    contract=str(
                        event_payload.get("hot_payload_contract")
                        or routing.hot_payload_contract
                    ),
                )
            event_id = f"turn-timeline:{normalized_execution_id}:{index:04d}"
            event_timestamp = str(getattr(hot_event, "timestamp", "") or "").strip()
            if not event_timestamp:
                continue
            conn.execute(
                """
                INSERT INTO orch_event_projections (
                    event_id,
                    event_family,
                    event_type,
                    target_kind,
                    target_id,
                    execution_id,
                    repo_id,
                    resource_kind,
                    resource_id,
                    run_id,
                    timestamp,
                    status,
                    payload_json,
                    processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    event_family = excluded.event_family,
                    event_type = excluded.event_type,
                    target_kind = excluded.target_kind,
                    target_id = excluded.target_id,
                    execution_id = excluded.execution_id,
                    repo_id = excluded.repo_id,
                    resource_kind = excluded.resource_kind,
                    resource_id = excluded.resource_id,
                    run_id = excluded.run_id,
                    timestamp = excluded.timestamp,
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    processed = excluded.processed
                """,
                (
                    event_id,
                    _EVENT_FAMILY,
                    event_type,
                    target_kind,
                    target_id,
                    normalized_execution_id,
                    repo_id,
                    resource_kind,
                    resource_id,
                    run_id,
                    event_timestamp,
                    status,
                    _json_dumps(event_payload),
                    1,
                ),
            )
            hot_state.note_hot_persisted(family)
    if count > 0:
        store.save_checkpoint(checkpoint_accumulator.build())
    return count


def append_turn_timeline(
    hub_root,
    *,
    execution_id: str,
    target_kind: Optional[str],
    target_id: Optional[str],
    repo_id: Optional[str] = None,
    run_id: Optional[str] = None,
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    events: Iterable[RunEvent],
) -> int:
    normalized_execution_id = str(execution_id or "").strip()
    if not normalized_execution_id:
        return 0
    with open_orchestration_sqlite(hub_root) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM orch_event_projections
             WHERE event_family = ?
               AND execution_id = ?
            """,
            (_EVENT_FAMILY, normalized_execution_id),
        ).fetchone()
    start_index = int(row["cnt"] or 0) + 1 if row is not None else 1
    return persist_turn_timeline(
        hub_root,
        execution_id=normalized_execution_id,
        target_kind=target_kind,
        target_id=target_id,
        repo_id=repo_id,
        run_id=run_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        metadata=metadata,
        events=events,
        start_index=start_index,
    )


def list_turn_timeline(hub_root, *, execution_id: str) -> list[dict[str, Any]]:
    normalized_execution_id = str(execution_id or "").strip()
    if not normalized_execution_id:
        return []
    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute(
            """
            SELECT event_id, event_type, timestamp, status, payload_json
              FROM orch_event_projections
             INDEXED BY idx_orch_event_projections_family_execution_order
             WHERE event_family = ?
               AND execution_id = ?
             ORDER BY timestamp ASC, event_id ASC
            """,
            (_EVENT_FAMILY, normalized_execution_id),
        ).fetchall()
    entries: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (ValueError, TypeError):
            payload = {}
        data = dict(payload) if isinstance(payload, dict) else {}
        data.setdefault("event_id", str(row["event_id"] or ""))
        data.setdefault("event_type", str(row["event_type"] or ""))
        data.setdefault("timestamp", str(row["timestamp"] or ""))
        data.setdefault("status", str(row["status"] or ""))
        entries.append(data)
    return entries


__all__ = [
    "append_turn_timeline",
    "append_turn_events_to_cold_trace",
    "iso_from_epoch_millis",
    "list_turn_timeline",
    "persist_turn_timeline",
]
