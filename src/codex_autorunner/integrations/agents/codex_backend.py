import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Optional, Union

from ...core.circuit_breaker import CircuitBreaker
from ...core.ports.agent_backend import AgentBackend, AgentEvent, now_iso
from ...core.ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunEvent,
    Started,
    ToolCall,
)
from ...integrations.app_server.client import CodexAppServerClient, CodexAppServerError

_logger = logging.getLogger(__name__)

ApprovalDecision = Union[str, Dict[str, Any]]
NotificationHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class CodexAppServerBackend(AgentBackend):
    def __init__(
        self,
        command: list[str],
        *,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        approval_policy: Optional[str] = None,
        sandbox_policy: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        turn_timeout_seconds: Optional[float] = None,
        auto_restart: Optional[bool] = None,
        request_timeout: Optional[float] = None,
        turn_stall_timeout_seconds: Optional[float] = None,
        turn_stall_poll_interval_seconds: Optional[float] = None,
        turn_stall_recovery_min_interval_seconds: Optional[float] = None,
        max_message_bytes: Optional[int] = None,
        oversize_preview_bytes: Optional[int] = None,
        max_oversize_drain_bytes: Optional[int] = None,
        restart_backoff_initial_seconds: Optional[float] = None,
        restart_backoff_max_seconds: Optional[float] = None,
        restart_backoff_jitter_ratio: Optional[float] = None,
        notification_handler: Optional[NotificationHandler] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self._command = command
        self._cwd = cwd
        self._env = env
        self._approval_policy = approval_policy
        self._sandbox_policy = sandbox_policy
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._turn_timeout_seconds = turn_timeout_seconds
        self._auto_restart = auto_restart
        self._request_timeout = request_timeout
        self._turn_stall_timeout_seconds = turn_stall_timeout_seconds
        self._turn_stall_poll_interval_seconds = turn_stall_poll_interval_seconds
        self._turn_stall_recovery_min_interval_seconds = (
            turn_stall_recovery_min_interval_seconds
        )
        self._max_message_bytes = max_message_bytes
        self._oversize_preview_bytes = oversize_preview_bytes
        self._max_oversize_drain_bytes = max_oversize_drain_bytes
        self._restart_backoff_initial_seconds = restart_backoff_initial_seconds
        self._restart_backoff_max_seconds = restart_backoff_max_seconds
        self._restart_backoff_jitter_ratio = restart_backoff_jitter_ratio
        self._notification_handler = notification_handler
        self._logger = logger or _logger

        self._client: Optional[CodexAppServerClient] = None
        self._session_id: Optional[str] = None
        self._thread_id: Optional[str] = None
        self._turn_id: Optional[str] = None
        self._thread_info: Optional[Dict[str, Any]] = None

        self._circuit_breaker = CircuitBreaker("CodexAppServer", logger=_logger)
        self._event_queue: asyncio.Queue[RunEvent] = asyncio.Queue()

    async def _ensure_client(self) -> CodexAppServerClient:
        if self._client is None:
            self._client = CodexAppServerClient(
                self._command,
                cwd=self._cwd,
                env=self._env,
                approval_handler=self._handle_approval_request,
                notification_handler=self._handle_notification,
                auto_restart=self._auto_restart,
                request_timeout=self._request_timeout,
                turn_stall_timeout_seconds=self._turn_stall_timeout_seconds,
                turn_stall_poll_interval_seconds=self._turn_stall_poll_interval_seconds,
                turn_stall_recovery_min_interval_seconds=self._turn_stall_recovery_min_interval_seconds,
                max_message_bytes=self._max_message_bytes,
                oversize_preview_bytes=self._oversize_preview_bytes,
                max_oversize_drain_bytes=self._max_oversize_drain_bytes,
                restart_backoff_initial_seconds=self._restart_backoff_initial_seconds,
                restart_backoff_max_seconds=self._restart_backoff_max_seconds,
                restart_backoff_jitter_ratio=self._restart_backoff_jitter_ratio,
                logger=self._logger,
            )
            await self._client.start()
        return self._client

    def configure(
        self,
        *,
        approval_policy: Optional[str],
        sandbox_policy: Optional[str],
        model: Optional[str],
        reasoning_effort: Optional[str],
        turn_timeout_seconds: Optional[float],
        notification_handler: Optional[NotificationHandler],
    ) -> None:
        self._approval_policy = approval_policy
        self._sandbox_policy = sandbox_policy
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._turn_timeout_seconds = turn_timeout_seconds
        self._notification_handler = notification_handler

    async def start_session(self, target: dict, context: dict) -> str:
        client = await self._ensure_client()

        repo_root = Path(context.get("workspace") or self._cwd or Path.cwd())
        resume_session = context.get("session_id") or context.get("thread_id")
        # Ensure we don't reuse a stale turn id when a new session begins.
        self._turn_id = None
        if isinstance(resume_session, str) and resume_session:
            try:
                resume_result = await client.thread_resume(resume_session)
                if isinstance(resume_result, dict):
                    self._thread_info = resume_result
                resumed_id = (
                    resume_result.get("id")
                    if isinstance(resume_result, dict)
                    else resume_session
                )
                self._thread_id = (
                    resumed_id if isinstance(resumed_id, str) else resume_session
                )
            except CodexAppServerError:
                self._thread_id = None
                self._thread_info = None

        if not self._thread_id:
            result = await client.thread_start(str(repo_root))
            self._thread_info = result if isinstance(result, dict) else None
            self._thread_id = result.get("id") if isinstance(result, dict) else None

        if not self._thread_id:
            raise RuntimeError("Failed to start thread: missing thread ID")

        self._session_id = self._thread_id
        _logger.info("Started Codex app-server session: %s", self._session_id)

        return self._session_id

    async def run_turn(
        self, session_id: str, message: str
    ) -> AsyncGenerator[AgentEvent, None]:
        client = await self._ensure_client()

        if session_id:
            self._thread_id = session_id
            # Reset last turn to avoid interrupting the wrong turn when reusing backends.
            self._turn_id = None

        if not self._thread_id:
            await self.start_session(target={}, context={})

        _logger.info(
            "Running turn on thread %s with message: %s",
            self._thread_id or "unknown",
            message[:100],
        )

        turn_kwargs: Dict[str, Any] = {}
        if self._model:
            turn_kwargs["model"] = self._model
        if self._reasoning_effort:
            turn_kwargs["effort"] = self._reasoning_effort
        handle = await client.turn_start(
            self._thread_id if self._thread_id else "default",
            text=message,
            approval_policy=self._approval_policy,
            sandbox_policy=self._sandbox_policy,
            **turn_kwargs,
        )
        self._turn_id = handle.turn_id

        yield AgentEvent.stream_delta(content=message, delta_type="user_message")

        result = await handle.wait(timeout=self._turn_timeout_seconds)

        for msg in result.agent_messages:
            yield AgentEvent.stream_delta(content=msg, delta_type="assistant_message")

        for event_data in result.raw_events:
            yield self._parse_raw_event(event_data)

        yield AgentEvent.message_complete(
            final_message="\n".join(result.agent_messages)
        )

    async def run_turn_events(
        self, session_id: str, message: str
    ) -> AsyncGenerator[RunEvent, None]:
        client = await self._ensure_client()

        if session_id:
            self._thread_id = session_id
            self._turn_id = None

        if not self._thread_id:
            actual_session_id = await self.start_session(target={}, context={})
        else:
            actual_session_id = self._thread_id

        _logger.info(
            "Running turn events on thread %s with message: %s",
            actual_session_id or "unknown",
            message[:100],
        )

        yield Started(
            timestamp=now_iso(),
            session_id=actual_session_id,
            thread_id=self._thread_id,
            turn_id=self._turn_id,
        )

        yield OutputDelta(
            timestamp=now_iso(), content=message, delta_type="user_message"
        )

        self._event_queue = asyncio.Queue()

        turn_kwargs: dict[str, Any] = {}
        if self._model:
            turn_kwargs["model"] = self._model
        if self._reasoning_effort:
            turn_kwargs["effort"] = self._reasoning_effort
        handle = await client.turn_start(
            actual_session_id if actual_session_id else "default",
            text=message,
            approval_policy=self._approval_policy,
            sandbox_policy=self._sandbox_policy,
            **turn_kwargs,
        )
        self._turn_id = handle.turn_id

        wait_task = asyncio.create_task(handle.wait(timeout=self._turn_timeout_seconds))

        try:
            while True:
                if not self._event_queue.empty():
                    run_event = self._event_queue.get_nowait()
                    if run_event:
                        yield run_event
                    continue

                get_task = asyncio.create_task(self._event_queue.get())
                done_set, pending_set = await asyncio.wait(
                    {wait_task, get_task}, return_when=asyncio.FIRST_COMPLETED
                )

                if wait_task in done_set:
                    if get_task in pending_set:
                        get_task.cancel()
                    result = wait_task.result()
                    for msg in result.agent_messages:
                        yield OutputDelta(
                            timestamp=now_iso(),
                            content=msg,
                            delta_type="assistant_message",
                        )
                    # raw_events already contain the same notifications we streamed
                    # through _event_queue; skipping here avoids double-emitting.
                    while not self._event_queue.empty():
                        extra = self._event_queue.get_nowait()
                        if extra:
                            yield extra
                    yield Completed(
                        timestamp=now_iso(),
                        final_message="\n".join(result.agent_messages),
                    )
                    break

                if get_task in done_set:
                    run_event = get_task.result()
                    if run_event:
                        yield run_event
                for task in pending_set:
                    task.cancel()
        except Exception as e:
            _logger.error("Error during turn execution: %s", e)
            if not wait_task.done():
                wait_task.cancel()
            yield Failed(timestamp=now_iso(), error_message=str(e))

    async def stream_events(self, session_id: str) -> AsyncGenerator[AgentEvent, None]:
        if False:
            yield AgentEvent.stream_delta(content="", delta_type="noop")

    async def interrupt(self, session_id: str) -> None:
        target_thread = session_id or self._thread_id
        target_turn = self._turn_id
        if self._client and target_turn:
            try:
                await self._client.turn_interrupt(target_turn, thread_id=target_thread)
                _logger.info(
                    "Interrupted turn %s on thread %s",
                    target_turn,
                    target_thread or "unknown",
                )
                return
            except Exception as e:
                _logger.warning("Failed to interrupt turn: %s", e)
                return
        if self._client and target_thread:
            _logger.warning(
                "Cannot interrupt turn for thread %s: missing turn id",
                target_thread,
            )

    async def final_messages(self, session_id: str) -> list[str]:
        return []

    async def request_approval(
        self, description: str, context: Optional[Dict[str, Any]] = None
    ) -> bool:
        raise NotImplementedError(
            "Approvals are handled via approval_handler in CodexAppServerBackend"
        )

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.close()
        finally:
            self._client = None

    async def _handle_approval_request(
        self, request: Dict[str, Any]
    ) -> ApprovalDecision:
        method = request.get("method", "")
        item_type = request.get("params", {}).get("type", "")

        _logger.info("Received approval request: %s (type=%s)", method, item_type)
        request_id = str(request.get("id") or "")
        # Surface the approval request to consumers (e.g., Telegram) while defaulting to approve
        await self._event_queue.put(
            ApprovalRequested(
                timestamp=now_iso(),
                request_id=request_id,
                description=method or "approval requested",
                context=request.get("params", {}),
            )
        )

        return {"approve": True}

    async def _handle_notification(self, notification: Dict[str, Any]) -> None:
        if self._notification_handler is not None:
            try:
                await self._notification_handler(notification)
            except Exception as exc:
                self._logger.debug("Notification handler failed: %s", exc)
        method = notification.get("method", "")
        params = notification.get("params", {}) or {}
        thread_id = params.get("threadId") or params.get("thread_id")
        if self._thread_id and thread_id and thread_id != self._thread_id:
            return
        _logger.debug("Received notification: %s", method)
        run_event = self._map_to_run_event(notification)
        if run_event:
            await self._event_queue.put(run_event)

    def _map_to_run_event(self, event_data: Dict[str, Any]) -> Optional[RunEvent]:
        method = event_data.get("method", "")

        if method == "turn/streamDelta":
            content = event_data.get("params", {}).get("delta", "")
            return OutputDelta(
                timestamp=now_iso(), content=content, delta_type="assistant_stream"
            )

        if method == "item/toolCall/start":
            params = event_data.get("params", {})
            return ToolCall(
                timestamp=now_iso(),
                tool_name=params.get("name", ""),
                tool_input=params.get("input", {}),
            )

        if method == "item/toolCall/end":
            return None

        if method == "turn/error":
            params = event_data.get("params", {})
            error_message = params.get("message", "Unknown error")
            return Failed(timestamp=now_iso(), error_message=error_message)

        return None

    def _parse_raw_event(self, event_data: Dict[str, Any]) -> AgentEvent:
        method = event_data.get("method", "")

        if method == "turn/streamDelta":
            content = event_data.get("params", {}).get("delta", "")
            return AgentEvent.stream_delta(
                content=content, delta_type="assistant_stream"
            )

        if method == "item/toolCall/start":
            params = event_data.get("params", {})
            return AgentEvent.tool_call(
                tool_name=params.get("name", ""),
                tool_input=params.get("input", {}),
            )

        if method == "item/toolCall/end":
            params = event_data.get("params", {})
            return AgentEvent.tool_result(
                tool_name=params.get("name", ""),
                result=params.get("result"),
                error=params.get("error"),
            )

        if method == "turn/error":
            params = event_data.get("params", {})
            error_message = params.get("message", "Unknown error")
            return AgentEvent.error(error_message=error_message)

        return AgentEvent.stream_delta(content="", delta_type="unknown_event")

    @property
    def last_turn_id(self) -> Optional[str]:
        return self._turn_id

    @property
    def last_thread_info(self) -> Optional[Dict[str, Any]]:
        return self._thread_info
