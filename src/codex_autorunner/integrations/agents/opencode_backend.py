import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional

from ...agents.opencode.client import OpenCodeClient
from ...agents.opencode.events import SSEEvent
from ...agents.opencode.logging import OpenCodeEventFormatter
from ...agents.opencode.runtime import (
    OpenCodeTurnOutput,
    build_turn_id,
    collect_opencode_output,
    extract_session_id,
    map_approval_policy_to_permission,
    opencode_missing_env,
    parse_message_response,
    split_model_id,
)
from ...agents.opencode.supervisor import OpenCodeSupervisor
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
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
)

_logger = logging.getLogger(__name__)


class OpenCodeBackend(AgentBackend):
    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        supervisor: Optional[OpenCodeSupervisor] = None,
        workspace_root: Optional[Path] = None,
        auth: Optional[tuple[str, str]] = None,
        timeout: Optional[float] = None,
        agent: Optional[str] = None,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_policy: Optional[str] = None,
        session_stall_timeout_seconds: Optional[float] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self._supervisor = supervisor
        self._workspace_root = Path(workspace_root) if workspace_root else None
        self._client: Optional[OpenCodeClient]
        if base_url:
            self._client = OpenCodeClient(
                base_url=base_url,
                auth=auth,
                timeout=timeout,
                logger=logger,
            )
        else:
            self._client = None
        self._agent = agent
        self._model = model
        self._reasoning = reasoning
        self._approval_policy = approval_policy
        self._session_stall_timeout_seconds = session_stall_timeout_seconds
        self._logger = logger or _logger

        self._session_id: Optional[str] = None
        self._message_count: int = 0
        self._final_messages: list[str] = []
        self._last_turn_id: Optional[str] = None
        self._last_token_total: Optional[dict[str, Any]] = None
        self._event_formatter = OpenCodeEventFormatter()

    def configure(
        self,
        *,
        model: Optional[str],
        reasoning: Optional[str],
        approval_policy: Optional[str],
    ) -> None:
        self._model = model
        self._reasoning = reasoning
        self._approval_policy = approval_policy

    async def start_session(self, target: dict, context: dict) -> str:
        client = await self._ensure_client()
        workspace_root = self._workspace_root or Path(context.get("workspace") or ".")
        resume_session = context.get("session_id") or context.get("thread_id")
        if isinstance(resume_session, str) and resume_session:
            try:
                await client.get_session(resume_session)
                self._session_id = resume_session
            except Exception:
                self._session_id = None

        if not self._session_id:
            result = await client.create_session(
                title=f"Flow session {self._message_count}",
                directory=str(workspace_root),
            )
            self._session_id = extract_session_id(result, allow_fallback_id=True)

        if not self._session_id:
            raise RuntimeError("Failed to create OpenCode session: missing session ID")

        _logger.info("Started OpenCode session: %s", self._session_id)

        return self._session_id

    async def run_turn(
        self, session_id: str, message: str
    ) -> AsyncGenerator[AgentEvent, None]:
        client = await self._ensure_client()
        if session_id:
            self._session_id = session_id
        if not self._session_id:
            self._session_id = await self.start_session(target={}, context={})

        _logger.info("Sending message to session %s", self._session_id)

        yield AgentEvent.stream_delta(content=message, delta_type="user_message")

        await client.send_message(
            self._session_id,
            message=message,
            agent=self._agent,
            model=split_model_id(self._model) if self._model else None,
        )

        self._message_count += 1
        async for event in self._yield_events_until_completion():
            yield event

    async def run_turn_events(
        self, session_id: str, message: str
    ) -> AsyncGenerator[RunEvent, None]:
        client = await self._ensure_client()
        workspace_root = self._workspace_root or Path(".")

        if session_id:
            self._session_id = session_id
        if not self._session_id:
            self._session_id = await self.start_session(
                target={},
                context={"workspace": str(workspace_root)},
            )

        _logger.info("Running turn events on session %s", self._session_id)

        self._last_turn_id = build_turn_id(self._session_id)

        yield Started(timestamp=now_iso(), session_id=self._session_id)

        yield OutputDelta(
            timestamp=now_iso(), content=message, delta_type="user_message"
        )

        model_payload = split_model_id(self._model) if self._model else None
        missing_env = await opencode_missing_env(
            client, str(workspace_root), model_payload
        )
        if missing_env:
            provider_id = model_payload.get("providerID") if model_payload else None
            missing_label = ", ".join(missing_env)
            yield Failed(
                timestamp=now_iso(),
                error_message=(
                    "OpenCode provider "
                    f"{provider_id or 'selected'} requires env vars: {missing_label}"
                ),
            )
            return

        permission_policy = map_approval_policy_to_permission(
            self._approval_policy, default="allow"
        )

        event_queue: asyncio.Queue[RunEvent] = asyncio.Queue()
        self._event_formatter.reset()

        async def _enqueue_lines(lines: list[str]) -> None:
            for line in lines:
                await event_queue.put(
                    OutputDelta(
                        timestamp=now_iso(), content=line, delta_type="log_line"
                    )
                )

        async def _part_handler(
            part_type: str, part: dict[str, Any], delta_text: Optional[str]
        ) -> None:
            if part_type == "usage" and isinstance(part, dict):
                self._last_token_total = _usage_to_token_total(part)
                await event_queue.put(TokenUsage(timestamp=now_iso(), usage=dict(part)))
                await _enqueue_lines(self._event_formatter.format_usage(part))
            else:
                await _enqueue_lines(
                    self._event_formatter.format_part(part_type, part, delta_text)
                )
            if part_type == "text" and isinstance(delta_text, str) and delta_text:
                await event_queue.put(
                    OutputDelta(
                        timestamp=now_iso(),
                        content=delta_text,
                        delta_type="assistant_stream",
                    )
                )

        ready_event = asyncio.Event()
        output_task = asyncio.create_task(
            collect_opencode_output(
                client,
                session_id=self._session_id,
                workspace_path=str(workspace_root),
                model_payload=model_payload,
                permission_policy=permission_policy,
                part_handler=_part_handler,
                ready_event=ready_event,
                stall_timeout_seconds=self._session_stall_timeout_seconds,
            )
        )
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            await event_queue.put(
                RunNotice(
                    timestamp=now_iso(),
                    kind="ready_timeout",
                    message="OpenCode stream readiness wait timed out",
                    data={"timeout_seconds": 2.0},
                )
            )

        prompt_response: Any = None
        prompt_task: Optional[asyncio.Task[Any]] = asyncio.create_task(
            client.prompt_async(
                self._session_id,
                message=message,
                agent=self._agent,
                model=model_payload,
                variant=self._reasoning,
            )
        )

        output_result = None
        try:
            while True:
                queue_task = asyncio.create_task(event_queue.get())
                tasks = {output_task, queue_task}
                if prompt_task is not None:
                    tasks.add(prompt_task)
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )

                if queue_task in done:
                    yield queue_task.result()
                else:
                    queue_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await queue_task

                if prompt_task is not None and prompt_task in done:
                    try:
                        prompt_response = prompt_task.result()
                    except Exception as exc:
                        output_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await output_task
                        yield Failed(timestamp=now_iso(), error_message=str(exc))
                        return
                    prompt_task = None

                if output_task in done:
                    output_result = await output_task
                    break

        finally:
            if prompt_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await prompt_task
            for line in self._event_formatter.flush_all_reasoning():
                await event_queue.put(
                    OutputDelta(
                        timestamp=now_iso(), content=line, delta_type="log_line"
                    )
                )

        while not event_queue.empty():
            yield event_queue.get_nowait()

        if output_result is None:
            yield Failed(timestamp=now_iso(), error_message="OpenCode output failed")
            return

        if prompt_response is not None and not output_result.text:
            fallback = parse_message_response(prompt_response)
            if fallback.text:
                output_result = OpenCodeTurnOutput(
                    text=fallback.text, error=output_result.error
                )
            if fallback.error and not output_result.error:
                output_result = OpenCodeTurnOutput(
                    text=output_result.text, error=fallback.error
                )

        if output_result.text:
            yield Completed(timestamp=now_iso(), final_message=output_result.text)
        elif output_result.error:
            yield Failed(timestamp=now_iso(), error_message=output_result.error)
        else:
            yield Completed(timestamp=now_iso(), final_message="")

    async def stream_events(self, session_id: str) -> AsyncGenerator[AgentEvent, None]:
        if session_id:
            self._session_id = session_id
        if not self._session_id:
            raise RuntimeError("Session not started. Call start_session() first.")

        client = await self._ensure_client()
        async for sse in client.stream_events(directory=None):
            for agent_event in self._convert_sse_to_agent_event(sse):
                yield agent_event

    async def interrupt(self, session_id: str) -> None:
        target_session = session_id or self._session_id
        if target_session:
            client = await self._ensure_client()
            try:
                await client.abort(target_session)
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
            client = await self._ensure_client()
            async for sse in client.stream_events(
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
            client = await self._ensure_client()
            async for sse in client.stream_events(
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

    async def _ensure_client(self) -> OpenCodeClient:
        if self._client is not None:
            return self._client
        if self._supervisor is None or self._workspace_root is None:
            raise RuntimeError("OpenCode client unavailable: supervisor not configured")
        client = await self._supervisor.get_client(self._workspace_root)
        self._client = client
        return client

    @property
    def last_turn_id(self) -> Optional[str]:
        return self._last_turn_id

    @property
    def last_token_total(self) -> Optional[dict[str, Any]]:
        return self._last_token_total


def _usage_to_token_total(usage: dict[str, Any]) -> Optional[dict[str, int]]:
    if not isinstance(usage, dict):
        return None

    def _int(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    total = usage.get("totalTokens")
    total_tokens = int(total) if isinstance(total, (int, float)) else None
    input_tokens = _int("inputTokens")
    cached_tokens = _int("cachedInputTokens")
    output_tokens = _int("outputTokens")
    reasoning_tokens = _int("reasoningTokens")
    if total_tokens is None:
        total_tokens = input_tokens + cached_tokens + output_tokens + reasoning_tokens
    return {
        "total": total_tokens,
        "input_tokens": input_tokens,
        "prompt_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "completion_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "reasoning_output_tokens": reasoning_tokens,
    }
