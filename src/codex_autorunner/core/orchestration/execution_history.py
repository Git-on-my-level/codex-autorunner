from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Optional

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
from ..text_utils import _truncate_text

ExecutionHistoryEventFamily = Literal[
    "tool_call",
    "tool_result",
    "output_delta",
    "run_notice",
    "token_usage",
    "terminal",
    "provider_raw",
]
HotProjectionPayloadContract = Literal[
    "structured_event",
    "delta_only",
    "terminal_summary",
    "none",
]
CheckpointSignalStatus = Literal["ok", "error", "interrupted"]
ExecutionTraceManifestStatus = Literal["open", "finalized", "archived"]

_TIMELINE_HOT_FAMILY_BY_EVENT_TYPE: dict[str, str] = {
    "turn_started": "run_notice",
    "output_delta": "output_delta",
    "tool_call": "tool_call",
    "tool_result": "tool_result",
    "approval_requested": "run_notice",
    "token_usage": "token_usage",
    "run_notice": "run_notice",
    "turn_completed": "terminal",
    "turn_failed": "terminal",
}


def timeline_hot_family_for_event_type(event_type: str) -> Optional[str]:
    return _TIMELINE_HOT_FAMILY_BY_EVENT_TYPE.get(str(event_type or "").strip())


_HOT_TOOL_INPUT_MAX_CHARS = 2048
_HOT_TOOL_RESULT_MAX_CHARS = 2048
_HOT_TEXT_PREVIEW_CHARS = 240
_HOT_NOTICE_DATA_MAX_CHARS = 1024
_HOT_DELTA_CONTENT_MAX_CHARS = 512
_HOT_NOTICE_MESSAGE_MAX_CHARS = 512


@dataclass(frozen=True)
class ExecutionRetentionRule:
    """Per-family retention contract shared by execution-history writers.

    Hot projections must remain bounded and queryable. In particular,
    `output_delta` events may persist only the delta chunk for that event index;
    callers must not write repeated cumulative thinking/progress strings into
    hot-path rows.
    """

    event_family: ExecutionHistoryEventFamily
    persist_hot_projection: bool
    capture_cold_trace: bool
    update_checkpoint: bool
    hot_payload_contract: HotProjectionPayloadContract
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionRetentionPolicy:
    """Bundle of retention rules used by orchestration execution history."""

    rules: tuple[ExecutionRetentionRule, ...]

    def rule_for_family(
        self, event_family: ExecutionHistoryEventFamily
    ) -> ExecutionRetentionRule:
        for rule in self.rules:
            if rule.event_family == event_family:
                return rule
        raise KeyError(f"Unknown execution-history event family '{event_family}'")

    def to_dict(self) -> dict[str, Any]:
        return {"rules": [rule.to_dict() for rule in self.rules]}


DEFAULT_EXECUTION_RETENTION_POLICY = ExecutionRetentionPolicy(
    rules=(
        ExecutionRetentionRule(
            event_family="tool_call",
            persist_hot_projection=True,
            capture_cold_trace=True,
            update_checkpoint=True,
            hot_payload_contract="structured_event",
            notes=(
                "Persist a bounded tool summary in hot projections and keep the full "
                "provider/raw payload in cold trace artifacts."
            ),
        ),
        ExecutionRetentionRule(
            event_family="tool_result",
            persist_hot_projection=True,
            capture_cold_trace=True,
            update_checkpoint=True,
            hot_payload_contract="structured_event",
            notes=(
                "Persist operator-visible tool outcome metadata hot; retain large "
                "tool payloads only in cold traces."
            ),
        ),
        ExecutionRetentionRule(
            event_family="output_delta",
            persist_hot_projection=True,
            capture_cold_trace=True,
            update_checkpoint=True,
            hot_payload_contract="delta_only",
            notes=(
                "Persist discrete delta chunks only. Never write cumulative "
                "thinking/progress text repeatedly into hot-path rows."
            ),
        ),
        ExecutionRetentionRule(
            event_family="run_notice",
            persist_hot_projection=True,
            capture_cold_trace=True,
            update_checkpoint=True,
            hot_payload_contract="structured_event",
            notes=(
                "Operational notices remain queryable hot while the full runtime "
                "message stays in cold traces when available."
            ),
        ),
        ExecutionRetentionRule(
            event_family="token_usage",
            persist_hot_projection=True,
            capture_cold_trace=True,
            update_checkpoint=True,
            hot_payload_contract="structured_event",
            notes=(
                "Persist normalized usage summaries hot and treat provider-native "
                "usage payloads as cold trace detail."
            ),
        ),
        ExecutionRetentionRule(
            event_family="terminal",
            persist_hot_projection=True,
            capture_cold_trace=True,
            update_checkpoint=True,
            hot_payload_contract="terminal_summary",
            notes=(
                "Terminal state must remain visible in hot projections and "
                "checkpoint state; cold traces retain the raw terminal payload."
            ),
        ),
        ExecutionRetentionRule(
            event_family="provider_raw",
            persist_hot_projection=False,
            capture_cold_trace=True,
            update_checkpoint=False,
            hot_payload_contract="none",
            notes=(
                "Provider/raw payloads are cold-trace-only artifacts and must not "
                "be replayed during startup recovery."
            ),
        ),
    )
)


@dataclass(frozen=True)
class ExecutionHistoryRoutingDecision:
    """Resolved storage routing for one normalized event family."""

    event_family: ExecutionHistoryEventFamily
    persist_hot_projection: bool
    capture_cold_trace: bool
    update_checkpoint: bool
    hot_payload_contract: HotProjectionPayloadContract
    notes: str

    @classmethod
    def from_rule(
        cls, rule: ExecutionRetentionRule
    ) -> "ExecutionHistoryRoutingDecision":
        return cls(
            event_family=rule.event_family,
            persist_hot_projection=rule.persist_hot_projection,
            capture_cold_trace=rule.capture_cold_trace,
            update_checkpoint=rule.update_checkpoint,
            hot_payload_contract=rule.hot_payload_contract,
            notes=rule.notes,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HotProjectionEnvelope:
    """Typed payload written into hot operational projection rows."""

    event_index: int
    event_type: str
    event_family: ExecutionHistoryEventFamily
    event: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    storage_layer: str = "hot_projection"
    hot_payload_contract: HotProjectionPayloadContract = "structured_event"
    captures_cold_trace: bool = False
    updates_checkpoint: bool = False

    def to_payload(self) -> dict[str, Any]:
        payload = dict(self.metadata)
        payload.update(
            {
                "event_index": self.event_index,
                "event_type": self.event_type,
                "event_family": self.event_family,
                "storage_layer": self.storage_layer,
                "hot_payload_contract": self.hot_payload_contract,
                "captures_cold_trace": self.captures_cold_trace,
                "updates_checkpoint": self.updates_checkpoint,
                "event": dict(self.event),
            }
        )
        return payload


@dataclass(frozen=True)
class ExecutionTraceManifest:
    """Manifest for a cold full-fidelity execution trace artifact.

    The manifest is hot-path metadata only. Recovery code may inspect the
    manifest, but it must not replay the referenced artifact to reconstruct the
    current execution state.
    """

    trace_id: str
    execution_id: str
    artifact_relpath: str
    trace_format: str
    event_count: int
    byte_count: int = 0
    checksum: Optional[str] = None
    schema_version: int = 1
    status: ExecutionTraceManifestStatus = "open"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    backend_thread_id: Optional[str] = None
    backend_turn_id: Optional[str] = None
    includes_families: tuple[ExecutionHistoryEventFamily, ...] = ()
    redactions_applied: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionCheckpointSignal:
    """Compact terminal-signal summary copied into execution checkpoints."""

    source: str
    status: CheckpointSignalStatus
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionCheckpoint:
    """Compact execution snapshot safe for startup recovery.

    Checkpoints intentionally store only bounded scalars, counters, and short
    previews. They must not embed raw provider payloads, full reasoning traces,
    or cumulative progress transcripts.
    """

    status: str
    execution_id: Optional[str] = None
    thread_target_id: Optional[str] = None
    backend_thread_id: Optional[str] = None
    backend_turn_id: Optional[str] = None
    completion_source: Optional[str] = None
    assistant_text_preview: str = ""
    assistant_char_count: int = 0
    progress_text_preview: str = ""
    progress_text_kind: Optional[str] = None
    progress_char_count: int = 0
    last_runtime_method: Optional[str] = None
    last_progress_at: Optional[str] = None
    transport_status: Optional[str] = None
    transport_request_return_timestamp: Optional[str] = None
    token_usage: Optional[dict[str, Any]] = None
    failure_cause: Optional[str] = None
    raw_event_count: int = 0
    projection_event_cursor: int = 0
    reasoning_buffer_count: int = 0
    hot_projection_state: Optional[dict[str, Any]] = None
    terminal_signals: tuple[ExecutionCheckpointSignal, ...] = ()
    trace_manifest_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["terminal_signals"] = [
            signal.to_dict() for signal in self.terminal_signals
        ]
        return payload


def classify_run_event_family(event: RunEvent) -> ExecutionHistoryEventFamily:
    if isinstance(event, ToolCall):
        return "tool_call"
    if isinstance(event, ToolResult):
        return "tool_result"
    if isinstance(event, OutputDelta):
        return "output_delta"
    if isinstance(event, TokenUsage):
        return "token_usage"
    if isinstance(event, (Completed, Failed)):
        return "terminal"
    if isinstance(event, (Started, ApprovalRequested, RunNotice)):
        return "run_notice"
    raise TypeError(f"Unsupported run event type '{type(event).__name__}'")


def route_run_event(
    event: RunEvent,
    *,
    policy: ExecutionRetentionPolicy = DEFAULT_EXECUTION_RETENTION_POLICY,
) -> ExecutionHistoryRoutingDecision:
    family = classify_run_event_family(event)
    return ExecutionHistoryRoutingDecision.from_rule(policy.rule_for_family(family))


def provider_raw_trace_routing(
    *,
    policy: ExecutionRetentionPolicy = DEFAULT_EXECUTION_RETENTION_POLICY,
) -> ExecutionHistoryRoutingDecision:
    return ExecutionHistoryRoutingDecision.from_rule(
        policy.rule_for_family("provider_raw")
    )


def truncate_hot_event_payload(
    event: RunEvent,
    contract: HotProjectionPayloadContract,
) -> dict[str, Any]:
    if contract == "none":
        return {}
    raw = asdict(event)
    if contract == "terminal_summary":
        bounded: dict[str, Any] = {}
        if "timestamp" in raw:
            bounded["timestamp"] = raw["timestamp"]
        if "final_message" in raw:
            text = str(raw["final_message"] or "")
            bounded["final_message"] = text[:_HOT_TEXT_PREVIEW_CHARS]
            if len(text) > _HOT_TEXT_PREVIEW_CHARS:
                bounded["final_message_truncated"] = True
                bounded["final_message_chars"] = len(text)
        if "error_message" in raw:
            text = str(raw["error_message"] or "")
            bounded["error_message"] = text[:_HOT_TEXT_PREVIEW_CHARS]
            if len(text) > _HOT_TEXT_PREVIEW_CHARS:
                bounded["error_message_truncated"] = True
                bounded["error_message_chars"] = len(text)
        return bounded
    if contract == "delta_only":
        bounded = {}
        for key in ("timestamp", "delta_type"):
            if key in raw:
                bounded[key] = raw[key]
        content = str(raw.get("content", "") or "")
        bounded["content"] = _truncate_text(content, _HOT_DELTA_CONTENT_MAX_CHARS)
        bounded["content_chars"] = len(content)
        if len(content) > _HOT_DELTA_CONTENT_MAX_CHARS:
            bounded["content_truncated"] = True
            bounded["details_in_cold_trace"] = True
        return bounded
    bounded = dict(raw)
    if isinstance(event, ToolCall):
        tool_input = raw.get("tool_input")
        if isinstance(tool_input, dict):
            serialized = str(tool_input)
            if len(serialized) > _HOT_TOOL_INPUT_MAX_CHARS:
                bounded["tool_input_preview"] = serialized[:_HOT_TOOL_INPUT_MAX_CHARS]
                bounded["tool_input_truncated"] = True
                bounded["details_in_cold_trace"] = True
                del bounded["tool_input"]
    elif isinstance(event, ToolResult):
        result = raw.get("result")
        if result is not None:
            serialized = str(result)
            if len(serialized) > _HOT_TOOL_RESULT_MAX_CHARS:
                bounded["result_preview"] = serialized[:_HOT_TOOL_RESULT_MAX_CHARS]
                bounded["result_truncated"] = True
                bounded["details_in_cold_trace"] = True
                del bounded["result"]
        error = raw.get("error")
        if error is not None:
            serialized = str(error)
            if len(serialized) > _HOT_TOOL_RESULT_MAX_CHARS:
                bounded["error_preview"] = serialized[:_HOT_TOOL_RESULT_MAX_CHARS]
                bounded["error_truncated"] = True
                bounded["details_in_cold_trace"] = True
                del bounded["error"]
    elif isinstance(event, RunNotice):
        message = str(raw.get("message", "") or "")
        bounded["message"] = _truncate_text(message, _HOT_NOTICE_MESSAGE_MAX_CHARS)
        if len(message) > _HOT_NOTICE_MESSAGE_MAX_CHARS:
            bounded["message_truncated"] = True
            bounded["message_chars"] = len(message)
            bounded["details_in_cold_trace"] = True
        data = raw.get("data")
        if isinstance(data, dict) and data:
            serialized = str(data)
            if len(serialized) > _HOT_NOTICE_DATA_MAX_CHARS:
                bounded["data_preview"] = serialized[:_HOT_NOTICE_DATA_MAX_CHARS]
                bounded["data_truncated"] = True
                bounded["details_in_cold_trace"] = True
                del bounded["data"]
    return bounded


def build_hot_projection_envelope(
    *,
    event_index: int,
    event_type: str,
    event: RunEvent,
    metadata: Optional[Mapping[str, Any]] = None,
    routing: Optional[ExecutionHistoryRoutingDecision] = None,
) -> HotProjectionEnvelope:
    resolved_routing = routing or route_run_event(event)
    return HotProjectionEnvelope(
        event_index=event_index,
        event_type=event_type,
        event_family=resolved_routing.event_family,
        event=truncate_hot_event_payload(event, resolved_routing.hot_payload_contract),
        metadata=dict(metadata or {}),
        hot_payload_contract=resolved_routing.hot_payload_contract,
        captures_cold_trace=resolved_routing.capture_cold_trace,
        updates_checkpoint=resolved_routing.update_checkpoint,
    )


__all__ = [
    "CheckpointSignalStatus",
    "DEFAULT_EXECUTION_RETENTION_POLICY",
    "ExecutionCheckpoint",
    "ExecutionCheckpointSignal",
    "ExecutionHistoryEventFamily",
    "ExecutionHistoryRoutingDecision",
    "ExecutionRetentionPolicy",
    "ExecutionRetentionRule",
    "ExecutionTraceManifest",
    "ExecutionTraceManifestStatus",
    "HotProjectionEnvelope",
    "HotProjectionPayloadContract",
    "build_hot_projection_envelope",
    "classify_run_event_family",
    "provider_raw_trace_routing",
    "route_run_event",
    "truncate_hot_event_payload",
]
