from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal, Optional

if TYPE_CHECKING:
    from .flows.store import FlowStore

ArchiveTransitionMode = Literal["copy", "move"]


class LifecycleAction(str, Enum):
    KEEP = "keep"
    ARCHIVE = "archive"
    ARCHIVE_THEN_PRUNE = "archive_then_prune"
    EXPORT = "export"
    EXPORT_THEN_PRUNE = "export_then_prune"
    COMPACT = "compact"
    PRUNE = "prune"
    BLOCKED = "blocked"


class LifecycleReason(str, Enum):
    REVIEW_ARTIFACT = "review_artifact"
    COLD_TRACE = "cold_trace"
    ACTIVE_RUN_GUARD = "active_run_guard"
    RUN_NOT_TERMINAL = "run_not_terminal"
    AGE_LIMIT = "age_limit"
    COUNT_LIMIT = "count_limit"
    BYTE_BUDGET = "byte_budget"
    STALE_WORKSPACE = "stale_workspace"
    CACHE_REBUILDABLE = "cache_rebuildable"
    LOCK_GUARD = "lock_guard"
    LIVE_WORKSPACE_GUARD = "live_workspace_guard"
    CANONICAL_STORE_GUARD = "canonical_store_guard"
    STABLE_OUTPUT_GUARD = "stable_output_guard"
    PRESERVE_REQUESTED = "preserve_requested"
    POLICY_DISABLED = "policy_disabled"
    NO_CANDIDATES = "no_candidates"


@dataclass(frozen=True)
class LifecycleDecision:
    family: str
    action: LifecycleAction
    reason: LifecycleReason
    subject: str


@dataclass(frozen=True)
class LifecycleArchiveSpec:
    family: str
    key: str
    archive_dest: str
    archive_intents: frozenset[str]
    reason: LifecycleReason
    required: bool = True
    mode: ArchiveTransitionMode = "copy"
    source_resolver: Optional[Callable[[Path], Path]] = None


@dataclass(frozen=True)
class PlannedArchiveTransition:
    key: str
    source: Path
    dest: Path
    required: bool
    mode: ArchiveTransitionMode
    decision: LifecycleDecision


@dataclass(frozen=True)
class TelemetryLifecycleDecision:
    seq: int
    source_table: str
    event: dict[str, Any]
    decision: LifecycleDecision


@dataclass(frozen=True)
class TelemetryLifecyclePlan:
    decisions: tuple[TelemetryLifecycleDecision, ...]
    exported_events: tuple[dict[str, Any], ...]
    flow_event_app_prune_seqs: tuple[int, ...]
    flow_telemetry_app_prune_seqs: tuple[int, ...]
    delta_prune_seqs: tuple[int, ...]
    retained_seqs: tuple[int, ...]

    @property
    def lifecycle_decisions(self) -> tuple[LifecycleDecision, ...]:
        return tuple(item.decision for item in self.decisions)


class StateLifecycleController:
    def _resolve_archive_source(
        self, spec: LifecycleArchiveSpec, source_root: Path
    ) -> Path:
        if spec.source_resolver is not None:
            return spec.source_resolver(source_root)
        return source_root / spec.key

    def plan_archive_transitions(
        self,
        *,
        specs: Iterable[LifecycleArchiveSpec],
        source_root: Path,
        dest_root: Path,
        intent: str,
        path_filter: Optional[Iterable[str]] = None,
    ) -> tuple[PlannedArchiveTransition, ...]:
        selected_paths = set(path_filter) if path_filter is not None else None
        transitions: list[PlannedArchiveTransition] = []
        for spec in specs:
            if intent not in spec.archive_intents:
                continue
            if selected_paths is not None and spec.key not in selected_paths:
                continue
            transitions.append(
                PlannedArchiveTransition(
                    key=spec.key,
                    source=self._resolve_archive_source(spec, source_root),
                    dest=dest_root / spec.archive_dest,
                    required=spec.required,
                    mode=spec.mode,
                    decision=LifecycleDecision(
                        family=spec.family,
                        action=LifecycleAction.ARCHIVE,
                        reason=spec.reason,
                        subject=spec.key,
                    ),
                )
            )
        return tuple(transitions)

    def classify_run_telemetry(
        self,
        store: "FlowStore",
        run_id: str,
        *,
        is_terminal: bool,
    ) -> TelemetryLifecyclePlan:
        app_server_event_type = "app_server_event"
        agent_stream_delta_type = "agent_stream_delta"
        conn = store._get_conn()
        rows = conn.execute(
            """
            SELECT seq, id, run_id, event_type, timestamp, data, step_id
            FROM flow_events
            WHERE run_id = ? AND event_type IN (?, ?)
            ORDER BY seq ASC
            """,
            (
                run_id,
                app_server_event_type,
                agent_stream_delta_type,
            ),
        ).fetchall()
        telemetry_rows = conn.execute(
            """
            SELECT seq, id, run_id, event_type, timestamp, data
            FROM flow_telemetry
            WHERE run_id = ? AND event_type = ?
            ORDER BY seq ASC
        """,
            (run_id, app_server_event_type),
        ).fetchall()

        decisions: list[TelemetryLifecycleDecision] = []
        exported_events: list[dict[str, Any]] = []
        flow_event_app_prune_seqs: list[int] = []
        flow_telemetry_app_prune_seqs: list[int] = []
        delta_prune_seqs: list[int] = []
        retained_seqs: list[int] = []

        for row in rows:
            seq = row["seq"]
            event_type = row["event_type"]
            raw_data = row["data"]
            try:
                data = (
                    json.loads(raw_data)
                    if isinstance(raw_data, str)
                    else (raw_data or {})
                )
            except (json.JSONDecodeError, TypeError):
                data = {}
            event_record = {
                "seq": seq,
                "id": row["id"],
                "run_id": row["run_id"],
                "event_type": event_type,
                "timestamp": row["timestamp"],
                "data": data,
                "step_id": row["step_id"],
                "source": "flow_events",
            }
            exported_events.append(event_record)

            if event_type == app_server_event_type:
                family = "app_server_events"
                prune_target = flow_event_app_prune_seqs
            else:
                family = "agent_stream_deltas"
                prune_target = delta_prune_seqs

            if is_terminal:
                action = LifecycleAction.EXPORT_THEN_PRUNE
                reason = LifecycleReason.COLD_TRACE
                prune_target.append(seq)
            else:
                action = LifecycleAction.EXPORT
                reason = LifecycleReason.RUN_NOT_TERMINAL
                retained_seqs.append(seq)

            decisions.append(
                TelemetryLifecycleDecision(
                    seq=seq,
                    source_table="flow_events",
                    event=event_record,
                    decision=LifecycleDecision(
                        family=family,
                        action=action,
                        reason=reason,
                        subject=f"flow_events:{run_id}:{seq}",
                    ),
                )
            )

        for row in telemetry_rows:
            seq = row["seq"]
            raw_data = row["data"]
            try:
                data = (
                    json.loads(raw_data)
                    if isinstance(raw_data, str)
                    else (raw_data or {})
                )
            except (json.JSONDecodeError, TypeError):
                data = {}
            event_record = {
                "seq": seq,
                "id": row["id"],
                "run_id": row["run_id"],
                "event_type": row["event_type"],
                "timestamp": row["timestamp"],
                "data": data,
                "step_id": None,
                "source": "flow_telemetry",
            }
            exported_events.append(event_record)

            if is_terminal:
                action = LifecycleAction.EXPORT_THEN_PRUNE
                reason = LifecycleReason.COLD_TRACE
                flow_telemetry_app_prune_seqs.append(seq)
            else:
                action = LifecycleAction.EXPORT
                reason = LifecycleReason.RUN_NOT_TERMINAL
                retained_seqs.append(seq)

            decisions.append(
                TelemetryLifecycleDecision(
                    seq=seq,
                    source_table="flow_telemetry",
                    event=event_record,
                    decision=LifecycleDecision(
                        family="app_server_events",
                        action=action,
                        reason=reason,
                        subject=f"flow_telemetry:{run_id}:{seq}",
                    ),
                )
            )

        return TelemetryLifecyclePlan(
            decisions=tuple(decisions),
            exported_events=tuple(exported_events),
            flow_event_app_prune_seqs=tuple(flow_event_app_prune_seqs),
            flow_telemetry_app_prune_seqs=tuple(flow_telemetry_app_prune_seqs),
            delta_prune_seqs=tuple(delta_prune_seqs),
            retained_seqs=tuple(retained_seqs),
        )

    def summarize_decisions(
        self, decisions: Iterable[LifecycleDecision]
    ) -> dict[str, Any]:
        decision_list = list(decisions)
        by_action = Counter(decision.action.value for decision in decision_list)
        by_reason = Counter(decision.reason.value for decision in decision_list)
        by_family: dict[str, dict[str, Any]] = {}

        for decision in decision_list:
            family_summary = by_family.setdefault(
                decision.family,
                {
                    "total": 0,
                    "actions": Counter(),
                    "reasons": Counter(),
                },
            )
            family_summary["total"] += 1
            family_summary["actions"][decision.action.value] += 1
            family_summary["reasons"][decision.reason.value] += 1

        return {
            "total": len(decision_list),
            "actions": dict(sorted(by_action.items())),
            "reasons": dict(sorted(by_reason.items())),
            "families": {
                family: {
                    "total": summary["total"],
                    "actions": dict(sorted(summary["actions"].items())),
                    "reasons": dict(sorted(summary["reasons"].items())),
                }
                for family, summary in sorted(by_family.items())
            },
        }


def lifecycle_action_for_cleanup_action(action: Any) -> LifecycleAction:
    action_value = getattr(action, "value", action)
    mapping = {
        "keep": LifecycleAction.KEEP,
        "prune": LifecycleAction.PRUNE,
        "compact": LifecycleAction.COMPACT,
        "archive_then_prune": LifecycleAction.ARCHIVE_THEN_PRUNE,
        "skip_blocked": LifecycleAction.BLOCKED,
    }
    return mapping.get(str(action_value), LifecycleAction.KEEP)


def lifecycle_reason_for_cleanup_reason(reason: Any) -> LifecycleReason:
    reason_value = getattr(reason, "value", reason)
    mapping = {
        "age_limit": LifecycleReason.AGE_LIMIT,
        "count_limit": LifecycleReason.COUNT_LIMIT,
        "byte_budget": LifecycleReason.BYTE_BUDGET,
        "stale_workspace": LifecycleReason.STALE_WORKSPACE,
        "cache_rebuildable": LifecycleReason.CACHE_REBUILDABLE,
        "active_run_guard": LifecycleReason.ACTIVE_RUN_GUARD,
        "lock_guard": LifecycleReason.LOCK_GUARD,
        "live_workspace_guard": LifecycleReason.LIVE_WORKSPACE_GUARD,
        "canonical_store_guard": LifecycleReason.CANONICAL_STORE_GUARD,
        "stable_output_guard": LifecycleReason.STABLE_OUTPUT_GUARD,
        "preserve_requested": LifecycleReason.PRESERVE_REQUESTED,
        "policy_disabled": LifecycleReason.POLICY_DISABLED,
        "no_candidates": LifecycleReason.NO_CANDIDATES,
    }
    return mapping.get(str(reason_value), LifecycleReason.CANONICAL_STORE_GUARD)


DEFAULT_STATE_LIFECYCLE_CONTROLLER = StateLifecycleController()


__all__ = [
    "DEFAULT_STATE_LIFECYCLE_CONTROLLER",
    "LifecycleAction",
    "LifecycleArchiveSpec",
    "LifecycleDecision",
    "LifecycleReason",
    "PlannedArchiveTransition",
    "StateLifecycleController",
    "TelemetryLifecycleDecision",
    "TelemetryLifecyclePlan",
    "lifecycle_action_for_cleanup_action",
    "lifecycle_reason_for_cleanup_reason",
]
