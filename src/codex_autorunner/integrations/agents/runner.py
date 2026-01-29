from __future__ import annotations

import logging
from typing import AsyncGenerator, Callable, Optional

from ...core.ports.agent_backend import AgentBackend, AgentEvent
from ...core.ports.run_event import (
    Failed,
    OutputDelta,
    RunEvent,
    Started,
)

_logger = logging.getLogger(__name__)

LogHandler = Callable[[str], None]
EventCallback = Callable[[RunEvent], None]


async def run_turn_with_backend(
    backend: AgentBackend,
    message: str,
    session_id: Optional[str],
    *,
    log_handler: Optional[LogHandler] = None,
    event_callback: Optional[EventCallback] = None,
) -> int:
    """
    Execute a turn using the AgentBackend protocol.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    try:
        if not session_id:
            session_id = await backend.start_session(target={}, context={})

        if event_callback:
            event_callback(Started(timestamp=timestamp(), session_id=session_id))

        if log_handler:
            log_handler(message)

        events_consumed = False
        if hasattr(backend, "run_turn_events"):
            async for run_event in backend.run_turn_events(session_id, message):
                events_consumed = True
                if event_callback:
                    event_callback(run_event)
                if log_handler and isinstance(run_event, OutputDelta):
                    log_handler(run_event.content)

        if not events_consumed:
            async for agent_event in backend.run_turn(session_id, message):
                if isinstance(agent_event, AgentEvent):
                    if log_handler:
                        if agent_event.data.get("content"):
                            log_handler(agent_event.data["content"])
                elif isinstance(agent_event, str):
                    if log_handler:
                        log_handler(agent_event)

        return 0
    except Exception as exc:
        _logger.error("Turn execution failed: %s", exc)
        if event_callback:
            event_callback(Failed(timestamp=timestamp(), error_message=str(exc)))
        return 1


async def stream_turn_events(
    backend: AgentBackend,
    session_id: str,
) -> AsyncGenerator[AgentEvent, None]:
    """
    Stream events from a backend for an existing session.

    This is used for external streaming (e.g., WebSocket UI) where the turn
    has already been initiated and we want to stream events as they arrive.
    """
    if hasattr(backend, "stream_events"):
        async for event in backend.stream_events(session_id):
            yield event
    else:
        yield AgentEvent.stream_delta(content="", delta_type="noop")


def timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
