import json
import logging
from typing import Any, AsyncGenerator, Dict, Optional

from ...agents.opencode.client import OpenCodeClient
from ...agents.opencode.events import SSEEvent
from ...core.ports.agent_backend import (
    AgentBackend,
    AgentEvent,
    AgentEventType,
    now_iso,
)
from ...core.ports.run_event import (
    Completed,
    Failed,
    OutputDelta,
    RunEvent,
    Started,
    ToolCall,
)

_logger = logging.getLogger(__name__)


class OpenCodeBackend(AgentBackend):
    def __init__(
        self,
        base_url: str,
        *,
        auth: Optional[tuple[str, str]] = None,
        timeout: Optional[float] = None,
        agent: Optional[str] = None,
        model: Optional[dict[str, str]] = None,
    ):
        self._client = OpenCodeClient(
            base_url=base_url,
            auth=auth,
            timeout=timeout,
        )
        self._agent = agent
        self._model = model

        self._session_id: Optional[str] = None
        self._message_count: int = 0
        self._final_messages: list[str] = []

    async def start_session(self, target: dict, context: dict) -> str:
        result = await self._client.create_session(
            title=f"Flow session {self._message_count}",
            directory=None,
        )

        self._session_id = result.get("id")
        if not self._session_id:
            raise RuntimeError("Failed to create OpenCode session: missing session ID")

        _logger.info("Started OpenCode session: %s", self._session_id)

        return self._session_id

    async def run_turn(
        self, session_id: str, message: str
    ) -> AsyncGenerator[AgentEvent, None]:
        if session_id:
            self._session_id = session_id
        if not self._session_id:
            self._session_id = await self.start_session(target={}, context={})

        _logger.info("Sending message to session %s", self._session_id)

        yield AgentEvent.stream_delta(content=message, delta_type="user_message")

        await self._client.send_message(
            self._session_id,
            message=message,
            agent=self._agent,
            model=self._model,
        )

        self._message_count += 1
        async for event in self._yield_events_until_completion():
            yield event

    async def run_turn_events(
        self, session_id: str, message: str
    ) -> AsyncGenerator[RunEvent, None]:
        if session_id:
            self._session_id = session_id
        if not self._session_id:
            self._session_id = await self.start_session(target={}, context={})

        _logger.info("Running turn events on session %s", self._session_id)

        yield Started(timestamp=now_iso(), session_id=self._session_id)

        yield OutputDelta(
            timestamp=now_iso(), content=message, delta_type="user_message"
        )

        await self._client.send_message(
            self._session_id,
            message=message,
            agent=self._agent,
            model=self._model,
        )

        self._message_count += 1

        try:
            async for run_event in self._yield_run_events_until_completion():
                yield run_event
        except Exception as e:
            _logger.error("Error during turn execution: %s", e)
            yield Failed(timestamp=now_iso(), error_message=str(e))

    async def stream_events(self, session_id: str) -> AsyncGenerator[AgentEvent, None]:
        if session_id:
            self._session_id = session_id
        if not self._session_id:
            raise RuntimeError("Session not started. Call start_session() first.")

        async for sse in self._client.stream_events(directory=None):
            for agent_event in self._convert_sse_to_agent_event(sse):
                yield agent_event

    async def interrupt(self, session_id: str) -> None:
        target_session = session_id or self._session_id
        if target_session:
            try:
                await self._client.abort(target_session)
                _logger.info("Interrupted OpenCode session %s", target_session)
            except Exception as e:
                _logger.warning("Failed to interrupt session: %s", e)

    async def final_messages(self, session_id: str) -> list[str]:
        return self._final_messages

    async def request_approval(
        self, description: str, context: Optional[Dict[str, Any]] = None
    ) -> bool:
        raise NotImplementedError("Approvals not implemented for OpenCodeBackend")

    async def _yield_events_until_completion(self) -> AsyncGenerator[AgentEvent, None]:
        paths = ["/event", "/global/event"]
        if self._session_id:
            paths.insert(0, f"/session/{self._session_id}/event")
        try:
            async for sse in self._client.stream_events(
                directory=None,
                paths=paths,
            ):
                if not self._sse_matches_session(sse):
                    continue
                for agent_event in self._convert_sse_to_agent_event(sse):
                    yield agent_event
                    if agent_event.event_type in {
                        AgentEventType.MESSAGE_COMPLETE,
                        AgentEventType.SESSION_ENDED,
                    }:
                        if agent_event.event_type == AgentEventType.MESSAGE_COMPLETE:
                            self._final_messages.append(
                                agent_event.data.get("final_message", "")
                            )
                        return
        except Exception as e:
            _logger.warning("Error in event collection: %s", e)
            yield AgentEvent.error(error_message=str(e))

    async def _yield_run_events_until_completion(
        self,
    ) -> AsyncGenerator[RunEvent, None]:
        paths = ["/event", "/global/event"]
        if self._session_id:
            paths.insert(0, f"/session/{self._session_id}/event")
        try:
            async for sse in self._client.stream_events(
                directory=None,
                paths=paths,
            ):
                if not self._sse_matches_session(sse):
                    continue
                for run_event in self._convert_sse_to_run_event(sse):
                    yield run_event
                    if isinstance(run_event, (Completed, Failed)):
                        if isinstance(run_event, Completed):
                            self._final_messages.append(run_event.final_message)
                        return
        except Exception as e:
            _logger.warning("Error in run event collection: %s", e)
            yield Failed(timestamp=now_iso(), error_message=str(e))

    def _convert_sse_to_run_event(self, sse: SSEEvent) -> list[RunEvent]:
        events: list[RunEvent] = []

        try:
            payload = json.loads(sse.data) if sse.data else {}
        except json.JSONDecodeError:
            return events

        payload_type = payload.get("type", "")

        if payload_type == "textDelta":
            text = payload.get("text", "")
            events.append(
                OutputDelta(
                    timestamp=now_iso(), content=text, delta_type="assistant_stream"
                )
            )

        elif payload_type == "toolCall":
            tool_name = payload.get("toolName", "")
            tool_input = payload.get("toolInput", {})
            events.append(
                ToolCall(
                    timestamp=now_iso(), tool_name=tool_name, tool_input=tool_input
                )
            )

        elif payload_type == "toolCallEnd":
            pass

        elif payload_type == "messageEnd":
            final_message = payload.get("message", "")
            events.append(Completed(timestamp=now_iso(), final_message=final_message))

        elif payload_type == "error":
            error_message = payload.get("message", "Unknown error")
            events.append(Failed(timestamp=now_iso(), error_message=error_message))

        elif payload_type == "sessionEnd":
            # Prefer messageEnd content if we already saw it; otherwise treat as failure.
            final_message = payload.get("message") or ""
            if final_message:
                events.append(
                    Completed(timestamp=now_iso(), final_message=final_message)
                )
            else:
                events.append(
                    Failed(
                        timestamp=now_iso(),
                        error_message=payload.get("reason", "Session ended early"),
                    )
                )

        return events

    def _convert_sse_to_agent_event(self, sse: SSEEvent) -> list[AgentEvent]:
        events: list[AgentEvent] = []

        try:
            payload = json.loads(sse.data) if sse.data else {}
        except json.JSONDecodeError:
            return events

        payload_type = payload.get("type", "")
        session_id = self._extract_session_id(payload)

        if payload_type == "textDelta":
            text = payload.get("text", "")
            event = AgentEvent.stream_delta(content=text, delta_type="assistant_stream")
            if session_id:
                event.data["session_id"] = session_id
            events.append(event)

        elif payload_type == "toolCall":
            tool_name = payload.get("toolName", "")
            tool_input = payload.get("toolInput", {})
            event = AgentEvent.tool_call(tool_name=tool_name, tool_input=tool_input)
            if session_id:
                event.data["session_id"] = session_id
            events.append(event)

        elif payload_type == "toolCallEnd":
            tool_name = payload.get("toolName", "")
            result = payload.get("result")
            error = payload.get("error")
            event = AgentEvent.tool_result(
                tool_name=tool_name, result=result, error=error
            )
            if session_id:
                event.data["session_id"] = session_id
            events.append(event)

        elif payload_type == "messageEnd":
            final_message = payload.get("message", "")
            event = AgentEvent.message_complete(final_message=final_message)
            if session_id:
                event.data["session_id"] = session_id
            events.append(event)

        elif payload_type == "error":
            error_message = payload.get("message", "Unknown error")
            event = AgentEvent.error(error_message=error_message)
            if session_id:
                event.data["session_id"] = session_id
            events.append(event)

        elif payload_type == "sessionEnd":
            events.append(
                AgentEvent(
                    type=AgentEventType.SESSION_ENDED.value,
                    timestamp=now_iso(),
                    data={
                        "reason": payload.get("reason", "unknown"),
                        "session_id": session_id,
                    },
                )
            )

        return events

    def _extract_session_id(self, payload: dict[str, Any]) -> Optional[str]:
        for key in ("session", "sessionId", "sessionID", "session_id"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        return None

    def _sse_matches_session(self, sse: SSEEvent) -> bool:
        if not self._session_id:
            return True
        try:
            payload = json.loads(sse.data) if sse.data else {}
        except json.JSONDecodeError:
            return True
        session_id = self._extract_session_id(payload)
        if session_id is None:
            # If server does not tag events, do not drop them.
            return True
        return session_id == self._session_id
