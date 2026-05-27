"""OpenCode turn lifecycle policy.

This module owns the boundary between transport acknowledgement and runtime
turn completion.  ``prompt_async`` completion is only an acceptance signal; a
turn reaches a terminal result through runtime observation or trusted message
snapshot recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Optional

from .protocol_payload import (
    extract_message_phase,
    extract_status_type,
    message_completion_is_turn_terminal,
    parse_message_response,
    status_is_idle,
)

OpenCodeTurnOutputSource = Literal[
    "event_stream",
    "messages_snapshot",
    "none",
]


class OpenCodeTurnLifecycleState(Enum):
    ACCEPTED = "accepted"
    OBSERVING = "observing"
    TERMINAL_OBSERVED = "terminal_observed"
    SNAPSHOT_RECOVERED = "snapshot_recovered"
    EMPTY_TERMINAL = "empty_terminal"
    FAILED = "failed"


@dataclass(frozen=True)
class OpenCodeTurnObservation:
    assistant_text: str
    error: Optional[str]
    output_source: OpenCodeTurnOutputSource
    terminal_signal: Optional[str]


@dataclass(frozen=True)
class OpenCodeTurnLifecycleResult:
    state: OpenCodeTurnLifecycleState
    assistant_text: str
    terminal_signal: Optional[str]
    output_source: OpenCodeTurnOutputSource
    command_completed: bool
    terminal_observed: bool
    snapshot_recovered: bool
    error: Optional[str]
    raw_events: list[dict[str, object]]
    evidence: dict[str, object] = field(default_factory=dict)


def unwrap_harness_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if isinstance(payload.get("message"), dict):
        message = payload["message"]
        method = message.get("method")
        params = message.get("params")
        if isinstance(method, str) and isinstance(params, dict):
            return method, params
    method = payload.get("method")
    params = payload.get("params")
    if isinstance(method, str) and isinstance(params, dict):
        return method, params
    return "", {}


def terminal_signal_from_payloads(payloads: list[dict[str, Any]]) -> Optional[str]:
    for payload in payloads:
        method, params = unwrap_harness_payload(payload)
        signal = terminal_signal_from_event(method, params)
        if signal is not None:
            return signal
    return None


def terminal_signal_from_event(method: str, params: dict[str, Any]) -> Optional[str]:
    if method == "turn/completed":
        return "turn/completed"
    if method == "session.idle":
        return "session.idle"
    if method == "session.status" and status_is_idle(extract_status_type(params)):
        return "session.status:idle"
    if method == "message.completed" and message_completion_is_turn_terminal(params):
        return "message.completed"
    if method == "item/completed":
        item = params.get("item")
        if (
            isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and extract_message_phase(params) != "commentary"
        ):
            return "item/completed:agentMessage"
    return None


def lifecycle_result_from_observation(
    observation: OpenCodeTurnObservation,
    *,
    raw_events: list[dict[str, Any]],
    command_completed: bool,
    command_accepted_before_terminal: bool,
    collector_completed: bool,
) -> OpenCodeTurnLifecycleResult:
    terminal_signal = observation.terminal_signal or terminal_signal_from_payloads(
        raw_events
    )
    terminal_observed = bool(collector_completed and observation.error is None)
    if terminal_signal is None and terminal_observed:
        terminal_signal = "collector_completed"
    snapshot_recovered = observation.output_source == "messages_snapshot" and bool(
        observation.assistant_text
    )
    if observation.error:
        state = OpenCodeTurnLifecycleState.FAILED
    elif snapshot_recovered:
        state = OpenCodeTurnLifecycleState.SNAPSHOT_RECOVERED
    elif observation.assistant_text and terminal_observed:
        state = OpenCodeTurnLifecycleState.TERMINAL_OBSERVED
    elif terminal_observed:
        state = OpenCodeTurnLifecycleState.EMPTY_TERMINAL
    elif command_completed:
        state = OpenCodeTurnLifecycleState.ACCEPTED
    else:
        state = OpenCodeTurnLifecycleState.OBSERVING

    return OpenCodeTurnLifecycleResult(
        state=state,
        assistant_text=observation.assistant_text,
        terminal_signal=terminal_signal,
        output_source=observation.output_source,
        command_completed=command_completed,
        terminal_observed=terminal_observed,
        snapshot_recovered=snapshot_recovered,
        error=observation.error,
        raw_events=[dict(event) for event in raw_events],
        evidence={
            "command_accepted_before_terminal": command_accepted_before_terminal,
            "collector_completed": collector_completed,
            "raw_event_count": len(raw_events),
        },
    )


def command_response_error(response: Any) -> Optional[str]:
    """Return command-response errors without treating response text as output."""

    if response is None:
        return None
    result = parse_message_response(response)
    return result.error or None


async def coordinate_turn_lifecycle(
    *,
    collect_task: asyncio.Task[OpenCodeTurnObservation],
    command_task: Optional[asyncio.Task[Any]],
    raw_events: Callable[[], list[dict[str, Any]]],
) -> OpenCodeTurnLifecycleResult:
    """Coordinate transport acceptance and runtime observation.

    The command task is deliberately unable to provide assistant output.  It can
    only mark the prompt as accepted or fail the lifecycle if transport rejects
    the command.
    """

    command_completed = False
    command_accepted_before_terminal = False
    collector_completed = False
    try:
        if command_task is None:
            observation = await collect_task
            collector_completed = True
        else:
            if command_task.done() and not collect_task.done():
                command_completed = True
                command_accepted_before_terminal = True
                command_exc = command_task.exception()
                if command_exc is not None:
                    collect_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await collect_task
                    raise command_exc
                error = command_response_error(command_task.result())
                if error is not None:
                    collect_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await collect_task
                    raise RuntimeError(error)
                observation = await collect_task
                collector_completed = True
            else:
                done, _pending = await asyncio.wait(
                    {collect_task, command_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if command_task in done:
                    command_completed = True
                    command_exc = command_task.exception()
                    if command_exc is not None:
                        collect_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await collect_task
                        raise command_exc
                    error = command_response_error(command_task.result())
                    if error is not None:
                        collect_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await collect_task
                        raise RuntimeError(error)
                    if collect_task not in done:
                        command_accepted_before_terminal = True
                        observation = await collect_task
                        collector_completed = True
                    else:
                        observation = await collect_task
                        collector_completed = True
                else:
                    observation = await collect_task
                    collector_completed = True
                    try:
                        response = await command_task
                    finally:
                        if command_task.done():
                            command_completed = True
                    error = command_response_error(response)
                    if error is not None:
                        raise RuntimeError(error)
    except Exception as exc:  # intentional: turn-level failure conversion
        return lifecycle_result_from_observation(
            OpenCodeTurnObservation(
                assistant_text="",
                error=str(exc),
                output_source="none",
                terminal_signal=None,
            ),
            raw_events=raw_events(),
            command_completed=command_completed,
            command_accepted_before_terminal=command_accepted_before_terminal,
            collector_completed=collector_completed,
        )

    return lifecycle_result_from_observation(
        observation,
        raw_events=raw_events(),
        command_completed=command_completed,
        command_accepted_before_terminal=command_accepted_before_terminal,
        collector_completed=collector_completed,
    )
