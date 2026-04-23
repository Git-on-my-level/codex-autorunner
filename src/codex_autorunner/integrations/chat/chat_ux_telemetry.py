"""Shared chat UX telemetry: timing milestones, failure reason buckets, and helpers.

Platform adapters (Discord, Telegram) use these primitives so that future UX
work can be driven by comparable data across surfaces.
"""

from __future__ import annotations

import enum
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...core.ports.run_event import RunNotice
from .execution_event_journal import make_chat_execution_journal_notice


class ChatUxMilestone(enum.Enum):
    RAW_EVENT_RECEIVED = "raw_event_received"
    ACK_FINISHED = "ack_finished"
    FIRST_VISIBLE_FEEDBACK = "first_visible_feedback"
    QUEUE_VISIBLE = "queue_visible"
    FIRST_SEMANTIC_PROGRESS = "first_semantic_progress"
    INTERRUPT_REQUESTED_VISIBLE = "interrupt_requested_visible"
    TERMINAL_DELIVERY = "terminal_delivery"


class ChatUxFailureReason(enum.Enum):
    PLATFORM_ACK_TIMEOUT = "platform_ack_timeout"
    DELIVERY_REPLAY_FAILED = "delivery_replay_failed"
    BACKEND_INTERRUPT_TIMEOUT = "backend_interrupt_timeout"
    QUEUE_STARVATION = "queue_starvation"
    CALLBACK_ACK_DELAYED = "callback_ack_delayed"
    SUBMISSION_TIMEOUT = "submission_timeout"
    RUNTIME_ERROR = "runtime_error"
    GATEWAY_DELIVERY_EXPIRED = "gateway_delivery_expired"
    PROGRESS_EDIT_FAILED = "progress_edit_failed"
    ATTACHMENT_DOWNLOAD_FAILED = "attachment_download_failed"


@dataclass
class ChatUxTimingSnapshot:
    platform: str
    milestones: dict[ChatUxMilestone, float] = field(default_factory=dict)
    failure_reason: Optional[ChatUxFailureReason] = None
    conversation_id: Optional[str] = None
    channel_id: Optional[str] = None
    agent: Optional[str] = None

    def record(self, milestone: ChatUxMilestone, now: Optional[float] = None) -> None:
        if milestone not in self.milestones:
            self.milestones[milestone] = now if now is not None else time.monotonic()

    def has_milestone(self, milestone: ChatUxMilestone) -> bool:
        return milestone in self.milestones

    def delta_ms(self, start: ChatUxMilestone, end: ChatUxMilestone) -> Optional[float]:
        t_start = self.milestones.get(start)
        t_end = self.milestones.get(end)
        if t_start is not None and t_end is not None:
            return round((t_end - t_start) * 1000, 1)
        return None

    def to_log_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "chat_ux_platform": self.platform,
        }
        if self.conversation_id is not None:
            fields["chat_ux_conversation_id"] = self.conversation_id
        if self.channel_id is not None:
            fields["chat_ux_channel_id"] = self.channel_id
        if self.agent is not None:
            fields["chat_ux_agent"] = self.agent
        if self.failure_reason is not None:
            fields["chat_ux_failure_reason"] = self.failure_reason.value

        milestone_fields: dict[str, Any] = {}
        for milestone in ChatUxMilestone:
            ts = self.milestones.get(milestone)
            if ts is not None:
                milestone_fields[f"chat_ux_ts_{milestone.value}"] = ts

        delta_pairs: list[tuple[ChatUxMilestone, ChatUxMilestone, str]] = [
            (
                ChatUxMilestone.RAW_EVENT_RECEIVED,
                ChatUxMilestone.ACK_FINISHED,
                "chat_ux_delta_ack_ms",
            ),
            (
                ChatUxMilestone.RAW_EVENT_RECEIVED,
                ChatUxMilestone.FIRST_VISIBLE_FEEDBACK,
                "chat_ux_delta_first_visible_ms",
            ),
            (
                ChatUxMilestone.RAW_EVENT_RECEIVED,
                ChatUxMilestone.FIRST_SEMANTIC_PROGRESS,
                "chat_ux_delta_first_progress_ms",
            ),
            (
                ChatUxMilestone.RAW_EVENT_RECEIVED,
                ChatUxMilestone.TERMINAL_DELIVERY,
                "chat_ux_delta_terminal_ms",
            ),
            (
                ChatUxMilestone.RAW_EVENT_RECEIVED,
                ChatUxMilestone.QUEUE_VISIBLE,
                "chat_ux_delta_queue_visible_ms",
            ),
            (
                ChatUxMilestone.RAW_EVENT_RECEIVED,
                ChatUxMilestone.INTERRUPT_REQUESTED_VISIBLE,
                "chat_ux_delta_interrupt_visible_ms",
            ),
        ]
        for start_m, end_m, key in delta_pairs:
            delta = self.delta_ms(start_m, end_m)
            if delta is not None:
                milestone_fields[key] = delta

        fields.update(milestone_fields)
        return fields


def emit_chat_ux_timing(
    logger: logging.Logger,
    level: int,
    snapshot: ChatUxTimingSnapshot,
    *,
    event_suffix: str = "completed",
    **extra: Any,
) -> RunNotice:
    event_name = f"chat_ux_timing.{snapshot.platform}.{event_suffix}"
    log_event(logger, level, event_name, **snapshot.to_log_fields(), **extra)
    get_global_accumulator().record_snapshot(snapshot)
    payload = snapshot.to_log_fields()
    payload.update(dict(extra))
    payload["event_name"] = event_name
    payload["platform"] = snapshot.platform
    return make_chat_execution_journal_notice(
        domain="latency",
        name="summary",
        status=(
            str(extra.get("status")).strip()
            if extra.get("status") is not None
            else None
        ),
        message=format_chat_ux_summary(snapshot),
        data=payload,
    )


def format_chat_ux_summary(snapshot: ChatUxTimingSnapshot) -> str:
    parts: list[str] = [f"[{snapshot.platform}]"]
    if snapshot.failure_reason is not None:
        parts.append(f"fail={snapshot.failure_reason.value}")

    delta_names: list[tuple[ChatUxMilestone, ChatUxMilestone, str]] = [
        (
            ChatUxMilestone.RAW_EVENT_RECEIVED,
            ChatUxMilestone.ACK_FINISHED,
            "ack",
        ),
        (
            ChatUxMilestone.RAW_EVENT_RECEIVED,
            ChatUxMilestone.FIRST_VISIBLE_FEEDBACK,
            "first_visible",
        ),
        (
            ChatUxMilestone.RAW_EVENT_RECEIVED,
            ChatUxMilestone.FIRST_SEMANTIC_PROGRESS,
            "first_progress",
        ),
        (
            ChatUxMilestone.RAW_EVENT_RECEIVED,
            ChatUxMilestone.TERMINAL_DELIVERY,
            "terminal",
        ),
    ]
    for start_m, end_m, label in delta_names:
        delta = snapshot.delta_ms(start_m, end_m)
        if delta is not None:
            parts.append(f"{label}={delta:.0f}ms")
    return " ".join(parts)


@dataclass(frozen=True)
class ChatUxDeltaSummary:
    label: str
    count: int
    avg_ms: Optional[float] = None
    p50_ms: Optional[float] = None
    p95_ms: Optional[float] = None
    max_ms: Optional[float] = None
    failure_count: int = 0


@dataclass(frozen=True)
class ChatUxPlatformSummary:
    platform: str
    total_snapshots: int
    deltas: tuple[ChatUxDeltaSummary, ...] = ()
    failure_count: int = 0
    failure_reasons: tuple[tuple[str, int], ...] = ()


_DEFAULT_ACCUMULATOR_MAX = 200

_global_accumulator: Optional[ChatUxTimingAccumulator] = None
_accumulator_lock = threading.Lock()


def _percentile(sorted_values: list[float], p: float) -> Optional[float]:
    if not sorted_values:
        return None
    idx = p / 100.0 * (len(sorted_values) - 1)
    lower = int(math.floor(idx))
    upper = int(math.ceil(idx))
    if lower == upper:
        return round(sorted_values[lower], 1)
    frac = idx - lower
    return round(
        sorted_values[lower] + frac * (sorted_values[upper] - sorted_values[lower]), 1
    )


_DELTA_PAIRS: tuple[tuple[ChatUxMilestone, ChatUxMilestone, str], ...] = (
    (ChatUxMilestone.RAW_EVENT_RECEIVED, ChatUxMilestone.ACK_FINISHED, "ack"),
    (
        ChatUxMilestone.RAW_EVENT_RECEIVED,
        ChatUxMilestone.FIRST_VISIBLE_FEEDBACK,
        "first_visible",
    ),
    (
        ChatUxMilestone.RAW_EVENT_RECEIVED,
        ChatUxMilestone.QUEUE_VISIBLE,
        "queue_visible",
    ),
    (
        ChatUxMilestone.RAW_EVENT_RECEIVED,
        ChatUxMilestone.FIRST_SEMANTIC_PROGRESS,
        "first_progress",
    ),
    (
        ChatUxMilestone.RAW_EVENT_RECEIVED,
        ChatUxMilestone.INTERRUPT_REQUESTED_VISIBLE,
        "interrupt_visible",
    ),
    (ChatUxMilestone.RAW_EVENT_RECEIVED, ChatUxMilestone.TERMINAL_DELIVERY, "terminal"),
)


class ChatUxTimingAccumulator:
    def __init__(self, max_snapshots: int = _DEFAULT_ACCUMULATOR_MAX) -> None:
        self._max = max_snapshots
        self._snapshots: deque[ChatUxTimingSnapshot] = deque(maxlen=max_snapshots)
        self._lock = threading.Lock()

    def record_snapshot(self, snapshot: ChatUxTimingSnapshot) -> None:
        with self._lock:
            self._snapshots.append(snapshot)

    @property
    def snapshot_count(self) -> int:
        with self._lock:
            return len(self._snapshots)

    def platform_summaries(self) -> list[ChatUxPlatformSummary]:
        with self._lock:
            by_platform: dict[str, list[ChatUxTimingSnapshot]] = {}
            for snap in self._snapshots:
                by_platform.setdefault(snap.platform, []).append(snap)

        results: list[ChatUxPlatformSummary] = []
        for platform in sorted(by_platform):
            snaps = by_platform[platform]
            deltas = self._compute_delta_summaries(snaps)
            failure_count = sum(1 for s in snaps if s.failure_reason is not None)
            reason_counts: dict[str, int] = {}
            for s in snaps:
                if s.failure_reason is not None:
                    reason_counts[s.failure_reason.value] = (
                        reason_counts.get(s.failure_reason.value, 0) + 1
                    )
            results.append(
                ChatUxPlatformSummary(
                    platform=platform,
                    total_snapshots=len(snaps),
                    deltas=tuple(deltas),
                    failure_count=failure_count,
                    failure_reasons=tuple(sorted(reason_counts.items())),
                )
            )
        return results

    def _compute_delta_summaries(
        self, snaps: list[ChatUxTimingSnapshot]
    ) -> list[ChatUxDeltaSummary]:
        results: list[ChatUxDeltaSummary] = []
        for start_m, end_m, label in _DELTA_PAIRS:
            values = sorted(
                d for s in snaps for d in [s.delta_ms(start_m, end_m)] if d is not None
            )
            if not values:
                continue
            failure_count = sum(
                1
                for s in snaps
                if s.delta_ms(start_m, end_m) is not None
                and s.failure_reason is not None
            )
            results.append(
                ChatUxDeltaSummary(
                    label=label,
                    count=len(values),
                    avg_ms=round(sum(values) / len(values), 1),
                    p50_ms=_percentile(values, 50),
                    p95_ms=_percentile(values, 95),
                    max_ms=round(max(values), 1),
                    failure_count=failure_count,
                )
            )
        return results

    def format_diagnostic_lines(self) -> list[str]:
        summaries = self.platform_summaries()
        if not summaries:
            return ["Chat UX timing: no data collected yet."]
        lines: list[str] = []
        for ps in summaries:
            lines.append(
                f"Chat UX timing [{ps.platform}]: {ps.total_snapshots} ops, {ps.failure_count} failures"
            )
            for ds in ps.deltas:
                parts = [f"  {ds.label}: n={ds.count}"]
                if ds.avg_ms is not None:
                    parts.append(f"avg={ds.avg_ms:.0f}ms")
                if ds.p50_ms is not None:
                    parts.append(f"p50={ds.p50_ms:.0f}ms")
                if ds.p95_ms is not None:
                    parts.append(f"p95={ds.p95_ms:.0f}ms")
                if ds.max_ms is not None:
                    parts.append(f"max={ds.max_ms:.0f}ms")
                if ds.failure_count:
                    parts.append(f"fail={ds.failure_count}")
                lines.append(" ".join(parts))
            for reason, count in ps.failure_reasons:
                lines.append(f"  failure: {reason} x{count}")
        return lines


def get_global_accumulator() -> ChatUxTimingAccumulator:
    global _global_accumulator
    with _accumulator_lock:
        if _global_accumulator is None:
            _global_accumulator = ChatUxTimingAccumulator()
        return _global_accumulator


def reset_global_accumulator() -> None:
    global _global_accumulator
    with _accumulator_lock:
        _global_accumulator = None
