"""Shared OpenCode turn runtime orchestration.

This module owns the protocol boundary where command acceptance, pre-connected
SSE streams, and terminal runtime observation meet.  Command responses are
treated as acceptance only; terminal results still come from runtime observation
or trusted snapshot recovery through ``turn_lifecycle``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

import httpx

from ...core.sse import SSEEvent
from .progress_synthesis import synthetic_command_result_events
from .runtime import (
    OpenCodeTurnOutput,
    opencode_event_is_progress_signal,
)
from .turn_lifecycle import (
    OpenCodeTurnLifecycleResult,
    OpenCodeTurnObservation,
    coordinate_turn_lifecycle,
)

_logger = logging.getLogger(__name__)

OpenCodeTurnCommandKind = Literal["prompt", "review"]
OpenCodeCommandExecutor = Callable[[], Awaitable[Any]]
OpenCodeAcceptanceCallback = Callable[[Any], Awaitable[None]]


@dataclass(frozen=True)
class OpenCodePreconnectedStream:
    event_queue: asyncio.Queue[Any]
    stream_task: asyncio.Task[None]
    event_seen: asyncio.Event


@dataclass(frozen=True)
class OpenCodeTurnCommand:
    kind: OpenCodeTurnCommandKind
    conversation_id: str
    executor: OpenCodeCommandExecutor
    on_acceptance: Optional[OpenCodeAcceptanceCallback] = None


@dataclass
class OpenCodeStartedCommand:
    command: OpenCodeTurnCommand
    task: asyncio.Task[Any]
    synthetic_raw_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class OpenCodeRuntimeObservation:
    output: OpenCodeTurnOutput
    lifecycle: OpenCodeTurnLifecycleResult


async def pre_connect_event_stream(
    client: Any,
    workspace_root: Path,
    conversation_id: str,
    *,
    event_seen: Optional[asyncio.Event] = None,
) -> OpenCodePreconnectedStream:
    """Start an OpenCode SSE stream before the command is sent."""

    queue: asyncio.Queue[Any] = asyncio.Queue()
    ready_event = asyncio.Event()
    seen = event_seen if event_seen is not None else asyncio.Event()

    async def _stream_to_queue() -> None:
        try:
            async for event in client.stream_events(
                directory=str(workspace_root),
                session_id=conversation_id,
                ready_event=ready_event,
            ):
                if _preconnected_event_counts_as_progress(
                    event,
                    conversation_id=conversation_id,
                ):
                    seen.set()
                await queue.put(event)
        except (
            RuntimeError,
            OSError,
            ProcessLookupError,
            BrokenPipeError,
            httpx.HTTPError,
        ):  # intentional: background SSE consumer must not crash
            _logger.debug("Pre-connected SSE stream error", exc_info=True)
        finally:
            await queue.put(None)

    task = asyncio.create_task(_stream_to_queue())
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        _logger.debug("SSE pre-connect timed out after 2s, continuing anyway")
    return OpenCodePreconnectedStream(
        event_queue=queue,
        stream_task=task,
        event_seen=seen,
    )


def _preconnected_event_counts_as_progress(
    event: SSEEvent,
    *,
    conversation_id: str,
) -> bool:
    return opencode_event_is_progress_signal(event, session_id=conversation_id)


def start_command(command: OpenCodeTurnCommand) -> OpenCodeStartedCommand:
    """Start a prompt/review command task with shared acceptance handling."""

    async def _run() -> Any:
        result = await command.executor()
        if command.on_acceptance is not None:
            await command.on_acceptance(result)
        return result

    task = asyncio.create_task(_run())
    _observe_background_task(task)
    return OpenCodeStartedCommand(command=command, task=task)


async def append_synthetic_acceptance_events(
    *,
    conversation_id: str,
    result: Any,
    raw_events: list[dict[str, Any]],
    event_buffer: Any,
) -> None:
    """Publish synthetic acceptance events when the real stream stayed silent."""

    for raw_event in synthetic_command_result_events(conversation_id, result):
        raw_events.append(raw_event)
        await event_buffer.append(raw_event)


async def cancel_and_wait(task: Optional[asyncio.Task[Any]]) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def observe_turn_runtime(
    *,
    collect_task: asyncio.Task[OpenCodeTurnOutput],
    command_task: Optional[asyncio.Task[Any]],
    raw_events: Callable[[], list[dict[str, Any]]],
) -> OpenCodeRuntimeObservation:
    """Coordinate collector and command task using shared lifecycle policy."""

    async def _collect_observation() -> OpenCodeTurnObservation:
        output = await collect_task
        return OpenCodeTurnObservation(
            assistant_text=output.text,
            error=output.error,
            output_source=output.output_source,
            terminal_signal=output.terminal_signal,
        )

    observation_task = asyncio.create_task(_collect_observation())
    lifecycle = await coordinate_turn_lifecycle(
        collect_task=observation_task,
        command_task=command_task,
        raw_events=raw_events,
    )
    usage: Optional[dict[str, Any]] = None
    if collect_task.done() and not collect_task.cancelled():
        with contextlib.suppress(Exception):
            usage = collect_task.result().usage
    output = OpenCodeTurnOutput(
        text=lifecycle.assistant_text,
        error=lifecycle.error,
        usage=usage,
        output_source=lifecycle.output_source,
        terminal_signal=lifecycle.terminal_signal,
    )
    return OpenCodeRuntimeObservation(output=output, lifecycle=lifecycle)


def _observe_background_task(task: asyncio.Task[Any]) -> None:
    def _consume_result(done: asyncio.Task[Any]) -> None:
        if done.cancelled():
            return
        with contextlib.suppress(Exception):
            done.result()

    task.add_done_callback(_consume_result)


__all__ = [
    "OpenCodePreconnectedStream",
    "OpenCodeRuntimeObservation",
    "OpenCodeStartedCommand",
    "OpenCodeTurnCommand",
    "append_synthetic_acceptance_events",
    "cancel_and_wait",
    "observe_turn_runtime",
    "pre_connect_event_stream",
    "start_command",
]
