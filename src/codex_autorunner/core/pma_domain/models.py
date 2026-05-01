from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..text_utils import _normalize_text
from .constants import (
    DEFAULT_PMA_LANE_ID,
    NOTICE_KIND_ESCALATION,
    NOTICE_KIND_NOOP,
    NOTICE_KIND_PROGRESS,
    NOTICE_KIND_TERMINAL_FOLLOWUP,
    SOURCE_KIND_MANAGED_THREAD_COMPLETED,
    SUBSCRIPTION_STATE_ACTIVE,
    SUPPRESSED_REASON_DUPLICATE_NOOP,
    TIMER_STATE_PENDING,
    TIMER_TYPE_ONE_SHOT,
    TIMER_TYPE_WATCHDOG,
    VALID_SURFACE_KINDS,
    WAKEUP_STATE_DISPATCHED,
    WAKEUP_STATE_PENDING,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_bool(value: Any, *, fallback: Optional[bool] = None) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return fallback


def _normalize_non_negative_int(
    value: Any, *, fallback: Optional[int] = None
) -> Optional[int]:
    if value is None:
        return fallback
    try:
        parsed = int(value)
    except (ValueError, TypeError):
        return fallback
    if parsed < 0:
        return fallback
    return parsed


def _normalize_positive_int(
    value: Any, *, fallback: Optional[int] = None
) -> Optional[int]:
    parsed = _normalize_non_negative_int(value, fallback=fallback)
    if parsed is None:
        return None
    if parsed <= 0:
        return fallback
    return parsed


def _normalize_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _normalize_text(item)
        if text is None:
            continue
        norm = text.lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _normalize_lane_id(value: Any) -> str:
    text = _normalize_text(value)
    if text is None:
        return DEFAULT_PMA_LANE_ID
    if text in VALID_SURFACE_KINDS:
        return text
    return text


def _normalize_timer_type(value: Any) -> str:
    text = _normalize_text(value) or TIMER_TYPE_ONE_SHOT
    if text == TIMER_TYPE_WATCHDOG:
        return TIMER_TYPE_WATCHDOG
    return TIMER_TYPE_ONE_SHOT


def _normalize_surface_kind(value: Any) -> Optional[str]:
    text = _normalize_text(value)
    if text is None:
        return None
    if text in VALID_SURFACE_KINDS:
        return text
    return None


def _normalize_due_timestamp(
    value: Any, *, field_name: str = "due_at"
) -> Optional[str]:
    text = _normalize_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PmaOriginContext:
    thread_id: Optional[str] = None
    lane_id: Optional[str] = None
    agent: Optional[str] = None
    profile: Optional[str] = None

    def is_empty(self) -> bool:
        return not any((self.thread_id, self.lane_id, self.agent, self.profile))

    def to_metadata(self) -> dict[str, str]:
        metadata: dict[str, str] = {}
        if self.thread_id:
            metadata["thread_id"] = self.thread_id
        if self.lane_id:
            metadata["lane_id"] = self.lane_id
        if self.agent:
            metadata["agent"] = self.agent
        if self.profile:
            metadata["profile"] = self.profile
        return metadata


@dataclass(frozen=True)
class PmaSubscription:
    subscription_id: str
    created_at: str
    updated_at: str
    state: str = SUBSCRIPTION_STATE_ACTIVE
    event_types: tuple[str, ...] = ()
    repo_id: Optional[str] = None
    run_id: Optional[str] = None
    thread_id: Optional[str] = None
    lane_id: str = DEFAULT_PMA_LANE_ID
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    max_matches: Optional[int] = None
    match_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict, hash=False)

    def is_active(self) -> bool:
        return self.state == SUBSCRIPTION_STATE_ACTIVE

    def is_exhausted(self) -> bool:
        if self.max_matches is None:
            return False
        return self.match_count >= self.max_matches


@dataclass(frozen=True)
class PmaTimer:
    timer_id: str
    due_at: str
    created_at: str
    updated_at: str
    state: str = TIMER_STATE_PENDING
    fired_at: Optional[str] = None
    timer_type: str = TIMER_TYPE_ONE_SHOT
    idle_seconds: Optional[int] = None
    subscription_id: Optional[str] = None
    repo_id: Optional[str] = None
    run_id: Optional[str] = None
    thread_id: Optional[str] = None
    lane_id: str = DEFAULT_PMA_LANE_ID
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict, hash=False)

    def is_pending(self) -> bool:
        return self.state == TIMER_STATE_PENDING

    def is_watchdog(self) -> bool:
        return self.timer_type == TIMER_TYPE_WATCHDOG


@dataclass(frozen=True)
class PmaWakeup:
    wakeup_id: str
    created_at: str
    updated_at: str
    state: str = WAKEUP_STATE_PENDING
    dispatched_at: Optional[str] = None
    source: str = "automation"
    repo_id: Optional[str] = None
    run_id: Optional[str] = None
    thread_id: Optional[str] = None
    lane_id: str = DEFAULT_PMA_LANE_ID
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    reason: Optional[str] = None
    timestamp: Optional[str] = None
    idempotency_key: Optional[str] = None
    subscription_id: Optional[str] = None
    timer_id: Optional[str] = None
    event_id: Optional[str] = None
    event_type: Optional[str] = None
    event_data: dict[str, Any] = field(default_factory=dict, hash=False)
    metadata: dict[str, Any] = field(default_factory=dict, hash=False)

    def is_pending(self) -> bool:
        return self.state == WAKEUP_STATE_PENDING

    def is_dispatched(self) -> bool:
        return self.state == WAKEUP_STATE_DISPATCHED


@dataclass(frozen=True)
class PmaDispatchAttempt:
    route: str
    delivery_mode: str
    surface_kind: str
    surface_key: Optional[str] = None
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None


@dataclass(frozen=True)
class PmaDispatchDecision:
    requested_delivery: str
    suppress_publish: bool = False
    attempts: tuple[PmaDispatchAttempt, ...] = ()


@dataclass(frozen=True)
class PmaDeliveryTarget:
    surface_kind: str
    surface_key: Optional[str] = None


@dataclass(frozen=True)
class PmaDeliveryAttempt:
    route: str
    delivery_mode: str
    target: PmaDeliveryTarget
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None


@dataclass(frozen=True)
class PmaDeliveryIntent:
    message: str
    correlation_id: str
    source_kind: str
    requested_delivery: str
    attempts: tuple[PmaDeliveryAttempt, ...] = ()
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None
    run_id: Optional[str] = None
    managed_thread_id: Optional[str] = None


@dataclass(frozen=True)
class PmaDeliveryState:
    delivery_id: str
    wakeup_id: Optional[str] = None
    dispatch_decision: Optional[PmaDispatchDecision] = None
    status: str = "pending"
    attempts_made: int = 0
    last_attempt_at: Optional[str] = None
    last_error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class PublishNoticeContext:
    trigger: str
    status: str
    correlation_id: str
    repo_id: Optional[str] = None
    run_id: Optional[str] = None
    thread_id: Optional[str] = None
    output: Optional[str] = None
    detail: Optional[str] = None
    token_usage_footer: Optional[str] = None

    def notice_kind(self) -> str:
        from .publish_policy import is_noop_duplicate_message

        if self.status == "ok" and is_noop_duplicate_message(self.output or ""):
            return NOTICE_KIND_NOOP
        if self.status == "ok":
            return NOTICE_KIND_TERMINAL_FOLLOWUP
        if self.status == "error":
            return NOTICE_KIND_ESCALATION
        return NOTICE_KIND_PROGRESS


@dataclass(frozen=True)
class PublishSuppressionDecision:
    suppressed: bool
    reason: str = ""
    notice_kind: str = ""

    @staticmethod
    def not_suppressed(*, notice_kind: str) -> PublishSuppressionDecision:
        return PublishSuppressionDecision(
            suppressed=False,
            notice_kind=notice_kind,
        )

    @staticmethod
    def duplicate_noop(*, notice_kind: str) -> PublishSuppressionDecision:
        return PublishSuppressionDecision(
            suppressed=True,
            reason=SUPPRESSED_REASON_DUPLICATE_NOOP,
            notice_kind=notice_kind,
        )

    @staticmethod
    def evaluate(
        *,
        source_kind: str,
        managed_thread_id: Optional[str],
        target_matches_thread_binding: bool,
        notice_kind: str,
    ) -> PublishSuppressionDecision:
        if (
            source_kind == SOURCE_KIND_MANAGED_THREAD_COMPLETED
            and managed_thread_id is not None
            and target_matches_thread_binding
            and notice_kind == NOTICE_KIND_NOOP
        ):
            return PublishSuppressionDecision.duplicate_noop(notice_kind=notice_kind)
        return PublishSuppressionDecision.not_suppressed(notice_kind=notice_kind)
