import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncGenerator, Dict, Optional

_logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AgentEventType(str, Enum):
    STREAM_DELTA = "stream_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MESSAGE_COMPLETE = "message_complete"
    ERROR = "error"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    SESION_STARTED = "session_started"  # legacy typo kept for backward tests


@dataclass
class AgentEvent:
    type: str
    timestamp: str
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def event_type(self) -> AgentEventType:
        try:
            return AgentEventType(self.type)
        except ValueError:
            return AgentEventType.ERROR

    @classmethod
    def stream_delta(cls, content: str, delta_type: str = "text") -> "AgentEvent":
        return cls(
            type=AgentEventType.STREAM_DELTA.value,
            timestamp=now_iso(),
            data={"content": content, "delta_type": delta_type},
        )

    @classmethod
    def tool_call(cls, tool_name: str, tool_input: Dict[str, Any]) -> "AgentEvent":
        return cls(
            type=AgentEventType.TOOL_CALL.value,
            timestamp=now_iso(),
            data={"tool_name": tool_name, "tool_input": tool_input},
        )

    @classmethod
    def tool_result(
        cls, tool_name: str, result: Any, error: Optional[str] = None
    ) -> "AgentEvent":
        return cls(
            type=AgentEventType.TOOL_RESULT.value,
            timestamp=now_iso(),
            data={"tool_name": tool_name, "result": result, "error": error},
        )

    @classmethod
    def message_complete(cls, final_message: str) -> "AgentEvent":
        return cls(
            type=AgentEventType.MESSAGE_COMPLETE.value,
            timestamp=now_iso(),
            data={"final_message": final_message},
        )

    @classmethod
    def error(cls, error_message: str) -> "AgentEvent":
        return cls(
            type=AgentEventType.ERROR.value,
            timestamp=now_iso(),
            data={"error": error_message},
        )

    @classmethod
    def approval_requested(
        cls, request_id: str, description: str, context: Optional[Dict[str, Any]] = None
    ) -> "AgentEvent":
        return cls(
            type=AgentEventType.APPROVAL_REQUESTED.value,
            timestamp=now_iso(),
            data={
                "request_id": request_id,
                "description": description,
                "context": context or {},
            },
        )

    @classmethod
    def approval_granted(cls, request_id: str) -> "AgentEvent":
        return cls(
            type=AgentEventType.APPROVAL_GRANTED.value,
            timestamp=now_iso(),
            data={"request_id": request_id},
        )

    @classmethod
    def approval_denied(
        cls, request_id: str, reason: Optional[str] = None
    ) -> "AgentEvent":
        return cls(
            type=AgentEventType.APPROVAL_DENIED.value,
            timestamp=now_iso(),
            data={"request_id": request_id, "reason": reason},
        )


class AgentBackend:
    async def start_session(self, target: dict, context: dict) -> str:
        raise NotImplementedError

    async def run_turn(
        self, session_id: str, message: str
    ) -> AsyncGenerator[AgentEvent, None]:
        raise NotImplementedError

    async def stream_events(self, session_id: str) -> AsyncGenerator[AgentEvent, None]:
        raise NotImplementedError

    async def run_turn_events(
        self, session_id: str, message: str
    ) -> AsyncGenerator[Any, None]:
        raise NotImplementedError

    async def interrupt(self, session_id: str) -> None:
        raise NotImplementedError

    async def final_messages(self, session_id: str) -> list[str]:
        raise NotImplementedError

    async def request_approval(
        self, description: str, context: Optional[Dict[str, Any]] = None
    ) -> bool:
        raise NotImplementedError
