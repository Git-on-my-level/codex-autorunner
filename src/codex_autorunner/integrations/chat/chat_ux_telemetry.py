"""Shared chat UX telemetry: timing milestones, failure reason buckets, and helpers.

Platform adapters (Discord, Telegram) use these primitives so that future UX
work can be driven by comparable data across surfaces.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ...core.logging_utils import log_event


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
) -> None:
    event_name = f"chat_ux_timing.{snapshot.platform}.{event_suffix}"
    log_event(logger, level, event_name, **snapshot.to_log_fields(), **extra)


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
