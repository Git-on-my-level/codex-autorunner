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
    last_runtime_method: Optional[str] = None
    last_progress_at: Optional[str] = None
    transport_status: Optional[str] = None
    transport_request_return_timestamp: Optional[str] = None
    token_usage: Optional[dict[str, Any]] = None
    failure_cause: Optional[str] = None
    raw_event_count: int = 0
    projection_event_cursor: int = 0
    reasoning_buffer_count: int = 0
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
        event=asdict(event),
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
    "HotProjectionEnvelope",
    "HotProjectionPayloadContract",
    "build_hot_projection_envelope",
    "classify_run_event_family",
    "provider_raw_trace_routing",
    "route_run_event",
]
