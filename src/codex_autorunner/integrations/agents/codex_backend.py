import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Optional, Union

from ...core.circuit_breaker import CircuitBreaker
from ...core.logging_utils import log_event
from ...core.ports.agent_backend import AgentBackend, AgentEvent, now_iso
from ...core.ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunEvent,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
)
from ...integrations.app_server.client import CodexAppServerClient, CodexAppServerError
from ...integrations.app_server.supervisor import WorkspaceAppServerSupervisor

_logger = logging.getLogger(__name__)

ApprovalDecision = Union[str, Dict[str, Any]]
NotificationHandler = Callable[[Dict[str, Any]], Awaitable[None]]


def _extract_output_delta(params: Dict[str, Any]) -> str:
    for key in ("delta", "text", "output"):
        value = params.get(key)
        if isinstance(value, str):
            return value
    return ""


def _output_delta_type_for_method(method: object) -> str:
    if not isinstance(method, str):
        return "assistant_stream"
    normalized = method.strip().lower()
    if normalized in {
        "item/commandexecution/outputdelta",
        "item/filechange/outputdelta",
    }:
        return "log_line"
    return "assistant_stream"


def _normalize_tool_name(params: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    item = params.get("item")
    item_dict = item if isinstance(item, dict) else {}
    item_type = item_dict.get("type")

    if item_type == "commandExecution":
        command = item_dict.get("command")
        if not command:
            command = params.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command).strip()
        if isinstance(command, str) and command:
            return command, {"command": command}
        return "commandExecution", {}

    if item_type == "fileChange":
        files = item_dict.get("files")
        if isinstance(files, list):
            paths = [str(entry) for entry in files if isinstance(entry, str)]
            if paths:
                return "fileChange", {"files": paths}
        return "fileChange", {}

    if item_type == "tool":
        name = item_dict.get("name") or item_dict.get("tool") or item_dict.get("id")
        if isinstance(name, str) and name:
            return name, {}
        return "tool", {}

    name = params.get("name")
    if isinstance(name, str) and name:
        return name, (
            params.get("input") if isinstance(params.get("input"), dict) else {}
        )
    return "", {}


class CodexAppServerBackend(AgentBackend):
    def __init__(
        self,
        *,
        supervisor: WorkspaceAppServerSupervisor,
        workspace_root: Path,
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
        output_policy: str = "final_only",
        notification_handler: Optional[NotificationHandler] = None,
        default_approval_decision: str = "accept",
        logger: Optional[logging.Logger] = None,
    ):
        self._supervisor = supervisor
        self._workspace_root = workspace_root
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
        self._output_policy = output_policy
        self._notification_handler = notification_handler
        self._default_approval_decision = (
            default_approval_decision.strip()
            if isinstance(default_approval_decision, str)
            and default_approval_decision.strip()
            else "accept"
        )
        self._logger = logger or _logger

        self._client: Optional[CodexAppServerClient] = None
        self._session_id: Optional[str] = None
        self._thread_id: Optional[str] = None
        self._turn_id: Optional[str] = None
        self._thread_info: Optional[Dict[str, Any]] = None
        self._reasoning_summary_buffers: dict[str, str] = {}

        self._circuit_breaker = CircuitBreaker("CodexAppServer", logger=_logger)
        self._event_queue: asyncio.Queue[RunEvent] = asyncio.Queue()

    async def _ensure_client(self) -> CodexAppServerClient:
        if self._client is None:
            self._client = await self._supervisor.get_client(self._workspace_root)
        self._client._approval_handler = self._handle_approval_request
        self._client._notification_handler = self._handle_notification
        self._client._default_approval_decision = self._default_approval_decision
        return self._client

    def configure(self, **options: Any) -> None:
        approval_policy = options.get("approval_policy")
        if approval_policy is None:
            approval_policy = options.get("approval_policy_default")

        sandbox_policy = options.get("sandbox_policy")
        if sandbox_policy is None:
            sandbox_policy = options.get("sandbox_policy_default")

        reasoning_effort = options.get("reasoning_effort")
        if reasoning_effort is None:
            reasoning_effort = options.get("reasoning")

        self._approval_policy = approval_policy
        self._sandbox_policy = sandbox_policy
        self._model = options.get("model")
        self._reasoning_effort = reasoning_effort
        self._turn_timeout_seconds = options.get("turn_timeout_seconds")
        self._notification_handler = options.get("notification_handler")
        default_approval_decision = options.get("default_approval_decision")
        if (
            isinstance(default_approval_decision, str)
            and default_approval_decision.strip()
        ):
            self._default_approval_decision = default_approval_decision.strip()
            if self._client is not None:
                self._client._default_approval_decision = (
                    self._default_approval_decision
                )
        if self._client is not None:
            self._client._approval_handler = self._handle_approval_request
            self._client._notification_handler = self._handle_notification

    async def start_session(self, target: dict, context: dict) -> str:
        client = await self._ensure_client()

        workspace_raw = context.get("workspace")
        repo_root = (
            Path(workspace_raw)
            if isinstance(workspace_raw, str) and workspace_raw.strip()
            else self._workspace_root
        )
        if repo_root != self._workspace_root:
            self._workspace_root = repo_root
            self._client = None
            client = await self._ensure_client()
            self._thread_id = None
            self._thread_info = None
        resume_session = context.get("session_id") or context.get("thread_id")
        # Ensure we don't reuse a stale turn id when a new session begins.
        self._turn_id = None
        self._reasoning_summary_buffers.clear()
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
            self._reasoning_summary_buffers.clear()

        if not self._thread_id:
            await self.start_session(target={}, context={})

        message_hash = hashlib.sha256(message.encode()).hexdigest()[:16]
        log_event(
            self._logger,
            logging.INFO,
            "agent.turn_started",
            thread_id=self._thread_id,
            message_length=len(message),
            message_hash=message_hash,
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
        final_text = str(getattr(result, "final_message", "") or "")
        if not final_text.strip():
            final_text = "\n\n".join(
                msg.strip()
                for msg in getattr(result, "agent_messages", [])
                if isinstance(msg, str) and msg.strip()
            )

        for event_data in result.raw_events:
            yield self._parse_raw_event(event_data)

        yield AgentEvent.message_complete(final_message=final_text)

    async def run_turn_events(
        self, session_id: str, message: str
    ) -> AsyncGenerator[RunEvent, None]:
        client = await self._ensure_client()

        if session_id:
            self._thread_id = session_id
            self._turn_id = None
            self._reasoning_summary_buffers.clear()

        if not self._thread_id:
            actual_session_id = await self.start_session(target={}, context={})
        else:
            actual_session_id = self._thread_id

        message_hash = hashlib.sha256(message.encode()).hexdigest()[:16]
        log_event(
            self._logger,
            logging.INFO,
            "agent.turn_events_started",
            thread_id=actual_session_id,
            turn_id=self._turn_id,
            message_length=len(message),
            message_hash=message_hash,
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
                    final_text = str(getattr(result, "final_message", "") or "")
                    if not final_text.strip():
                        final_text = "\n\n".join(
                            msg.strip()
                            for msg in getattr(result, "agent_messages", [])
                            if isinstance(msg, str) and msg.strip()
                        )
                    # raw_events already contain the same notifications we streamed
                    # through _event_queue; skipping here avoids double-emitting.
                    while not self._event_queue.empty():
                        extra = self._event_queue.get_nowait()
                        if extra:
                            yield extra
                    yield Completed(
                        timestamp=now_iso(),
                        final_message=final_text,
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

        decision = self._default_approval_decision.strip().lower()
        return {
            "approve": decision
            in {"accept", "approve", "approved", "allow", "yes", "true"}
        }

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
        params = event_data.get("params", {}) or {}
        method_lower = method.lower() if isinstance(method, str) else ""

        if method == "item/reasoning/summaryTextDelta":
            delta = params.get("delta")
            if isinstance(delta, str):
                message = self._accumulate_reasoning_delta(params, delta)
                if message.strip():
                    return RunNotice(
                        timestamp=now_iso(),
                        kind="thinking",
                        message=message,
                    )
            return None

        if method == "item/agentMessage/delta":
            content = _extract_output_delta(params)
            if not content:
                return None
            return OutputDelta(
                timestamp=now_iso(), content=content, delta_type="assistant_stream"
            )

        if method == "turn/streamDelta" or "outputdelta" in method_lower:
            content = _extract_output_delta(params)
            if not content:
                return None
            delta_type = _output_delta_type_for_method(method)
            return OutputDelta(
                timestamp=now_iso(), content=content, delta_type=delta_type
            )

        if method == "item/toolCall/start":
            tool_name, tool_input = _normalize_tool_name(params)
            return ToolCall(
                timestamp=now_iso(),
                tool_name=tool_name or "toolCall",
                tool_input=tool_input,
            )

        if method == "item/toolCall/end":
            return None

        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "reasoning":
                self._clear_reasoning_buffers_for_params(params)
                return None
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return OutputDelta(
                        timestamp=now_iso(),
                        content=text,
                        delta_type="assistant_stream",
                    )
                return None
            tool_name, tool_input = _normalize_tool_name(params)
            if tool_name:
                return ToolCall(
                    timestamp=now_iso(),
                    tool_name=tool_name,
                    tool_input=tool_input,
                )
            return None

        if method in {"turn/tokenUsage", "turn/usage", "thread/tokenUsage/updated"}:
            usage = params.get("usage")
            if not isinstance(usage, dict):
                usage = params.get("tokenUsage")
            if isinstance(usage, dict):
                return TokenUsage(timestamp=now_iso(), usage=usage)
            return None

        if method == "turn/error":
            self._clear_reasoning_buffers_for_params(params)
            error_message = params.get("message", "Unknown error")
            return Failed(timestamp=now_iso(), error_message=error_message)

        return None

    def _parse_raw_event(self, event_data: Dict[str, Any]) -> AgentEvent:
        method = event_data.get("method", "")
        params = event_data.get("params", {})
        method_lower = method.lower() if isinstance(method, str) else ""

        if method == "item/agentMessage/delta":
            content = _extract_output_delta(params)
            if not content:
                return AgentEvent.stream_delta(content="", delta_type="unknown_event")
            return AgentEvent.stream_delta(
                content=content, delta_type="assistant_stream"
            )

        if method == "turn/streamDelta" or "outputdelta" in method_lower:
            content = _extract_output_delta(params)
            if not content:
                return AgentEvent.stream_delta(content="", delta_type="unknown_event")
            delta_type = _output_delta_type_for_method(method)
            return AgentEvent.stream_delta(content=content, delta_type=delta_type)

        if method == "item/toolCall/start":
            tool_name, tool_input = _normalize_tool_name(params)
            return AgentEvent.tool_call(
                tool_name=tool_name or "toolCall",
                tool_input=tool_input,
            )

        if method == "item/toolCall/end":
            return AgentEvent.tool_result(
                tool_name=params.get("name", ""),
                result=params.get("result"),
                error=params.get("error"),
            )

        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return AgentEvent.stream_delta(
                        content=text, delta_type="assistant_stream"
                    )
                return AgentEvent.stream_delta(content="", delta_type="unknown_event")
            tool_name, tool_input = _normalize_tool_name(params)
            if tool_name:
                return AgentEvent.tool_call(tool_name=tool_name, tool_input=tool_input)
            return AgentEvent.stream_delta(content="", delta_type="unknown_event")

        if method == "turn/error":
            error_message = params.get("message", "Unknown error")
            return AgentEvent.error(error_message=error_message)

        return AgentEvent.stream_delta(content="", delta_type="unknown_event")

    def _reasoning_buffer_key(self, params: Dict[str, Any]) -> Optional[str]:
        for key in ("itemId", "item_id", "turnId", "turn_id"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        if isinstance(self._turn_id, str) and self._turn_id:
            return self._turn_id
        return None

    def _accumulate_reasoning_delta(self, params: Dict[str, Any], delta: str) -> str:
        key = self._reasoning_buffer_key(params)
        if not key:
            return delta
        combined = f"{self._reasoning_summary_buffers.get(key, '')}{delta}"
        self._reasoning_summary_buffers[key] = combined
        return combined

    def _clear_reasoning_buffers_for_params(self, params: Dict[str, Any]) -> None:
        key = self._reasoning_buffer_key(params)
        if key is not None:
            self._reasoning_summary_buffers.pop(key, None)

    @property
    def last_turn_id(self) -> Optional[str]:
        return self._turn_id

    @property
    def last_thread_info(self) -> Optional[Dict[str, Any]]:
        return self._thread_info
