import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Union

from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)


class FailureReasonCode(str, Enum):
    REPO_NOT_FOUND = "repo_not_found"
    AGENT_CRASH = "agent_crash"
    PREFLIGHT_ERROR = "preflight_error"
    UNCAUGHT_EXCEPTION = "uncaught_exception"
    OOM_KILLED = "oom_killed"
    WORKER_DEAD = "worker_dead"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    USER_STOP = "user_stop"
    UNKNOWN = "unknown"


class FlowRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"

    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.STOPPED, self.SUPERSEDED}

    def is_active(self) -> bool:
        return self in {self.PENDING, self.RUNNING, self.STOPPING}

    def is_paused(self) -> bool:
        return self == self.PAUSED


class FlowEventType(str, Enum):
    STEP_STARTED = "step_started"
    STEP_PROGRESS = "step_progress"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    AGENT_STREAM_DELTA = "agent_stream_delta"
    AGENT_MESSAGE_COMPLETE = "agent_message_complete"
    AGENT_FAILED = "agent_failed"
    APP_SERVER_EVENT = "app_server_event"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUESTED = "approval_requested"
    TOKEN_USAGE = "token_usage"
    FLOW_STARTED = "flow_started"
    FLOW_STOPPED = "flow_stopped"
    FLOW_RESUMED = "flow_resumed"
    FLOW_COMPLETED = "flow_completed"
    FLOW_FAILED = "flow_failed"
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    RUN_STATE_CHANGED = "run_state_changed"
    RUN_NO_PROGRESS = "run_no_progress"
    PLAN_UPDATED = "plan_updated"
    DIFF_UPDATED = "diff_updated"
    APP_HOOK_STARTED = "app_hook_started"
    APP_HOOK_RESULT = "app_hook_result"
    RUN_TIMEOUT = "run_timeout"
    RUN_CANCELLED = "run_cancelled"


class FlowRunRecord(BaseModel):
    id: str
    flow_type: str
    status: FlowRunStatus
    input_data: Dict[str, Any] = Field(default_factory=dict)
    state: Dict[str, Any] = Field(default_factory=dict)
    current_step: Optional[str] = None
    stop_requested: bool = False
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class FlowEvent(BaseModel):
    seq: int
    id: str
    run_id: str
    event_type: FlowEventType
    timestamp: str
    data: Dict[str, Any] = Field(default_factory=dict)
    step_id: Optional[str] = None


class FlowArtifact(BaseModel):
    id: str
    run_id: str
    kind: str
    path: str
    created_at: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


def parse_flow_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        if normalized.endswith("Z"):
            normalized = normalized.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def flow_duration_seconds(
    started_at: Optional[str],
    finished_at: Optional[str],
    status: Union[FlowRunStatus, str],
    *,
    now: Optional[datetime] = None,
) -> Optional[float]:
    start_dt = parse_flow_timestamp(started_at)
    if start_dt is None:
        return None

    end_dt = parse_flow_timestamp(finished_at)
    status_value = (
        status.value if isinstance(status, FlowRunStatus) else str(status or "").strip()
    ).lower()
    if end_dt is None and status_value in {
        FlowRunStatus.PENDING.value,
        FlowRunStatus.RUNNING.value,
        FlowRunStatus.PAUSED.value,
        FlowRunStatus.STOPPING.value,
    }:
        end_dt = now or datetime.now(timezone.utc)
    if end_dt is None:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def flow_run_duration_seconds(
    record: FlowRunRecord, *, now: Optional[datetime] = None
) -> Optional[float]:
    return flow_duration_seconds(
        record.started_at,
        record.finished_at,
        record.status,
        now=now,
    )


def format_flow_duration(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    try:
        total_seconds = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return None

    if total_seconds < 60:
        return f"{total_seconds}s"

    minutes, secs = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"

    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if mins == 0 else f"{hours}h {mins}m"

    days, rem_hours = divmod(hours, 24)
    return f"{days}d" if rem_hours == 0 else f"{days}d {rem_hours}h"
