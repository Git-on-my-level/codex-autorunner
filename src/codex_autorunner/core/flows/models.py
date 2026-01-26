import logging
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)


class FlowRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"

    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.STOPPED}

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
    APP_SERVER_EVENT = "app_server_event"
    TOKEN_USAGE = "token_usage"
    FLOW_STARTED = "flow_started"
    FLOW_STOPPED = "flow_stopped"
    FLOW_RESUMED = "flow_resumed"
    FLOW_COMPLETED = "flow_completed"
    FLOW_FAILED = "flow_failed"


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
