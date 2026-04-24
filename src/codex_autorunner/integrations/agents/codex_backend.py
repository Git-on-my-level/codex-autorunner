import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Optional, Union

from ...core.logging_utils import log_event
from ...core.orchestration.runtime_thread_events import (
    RuntimeEventDriver,
    RuntimeThreadRunEventState,
    _normalize_tool_name,
    normalize_runtime_thread_message_payload,
)
from ...core.ports.agent_backend import AgentBackend, AgentEvent, now_iso
from ...core.ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunEvent,
    Started,
    ToolCall,
    ToolResult,
)
from ...integrations.app_server.client import CodexAppServerClient, CodexAppServerError
from ...integrations.app_server.supervisor import WorkspaceAppServerSupervisor

_logger = logging.getLogger(__name__)

ApprovalDecision = Union[str, Dict[str, Any]]
NotificationHandler = Callable[[Dict[str, Any]], Awaitable[None]]
ApprovalHandler = Callable[[Dict[str, Any]], Awaitable[ApprovalDecision]]


def _agent_event_from_run_event(run_event: RunEvent) -> Optional[AgentEvent]:
    if isinstance(run_event, OutputDelta):
        return AgentEvent.stream_delta(
            content=run_event.content,
            delta_type=run_event.delta_type,
        )
    if isinstance(run_event, ToolCall):
        return AgentEvent.tool_call(
            tool_name=run_event.tool_name,
            tool_input=run_event.tool_input,
        )
    if isinstance(run_event, ToolResult):
        return AgentEvent.tool_result(
            tool_name=run_event.tool_name,
            result=run_event.result,
            error=run_event.error,
        )
    if isinstance(run_event, Failed):
        return AgentEvent.error(error_message=run_event.error_message)
    if isinstance(run_event, ApprovalRequested):
        return AgentEvent.approval_requested(
            request_id=run_event.request_id,
            description=run_event.description,
            context=run_event.context,
        )
    return None


class CodexAppServerBackend(AgentBackend):
    """Adapts Codex app-server JSON-RPC protocol to the AgentBackend interface.

    Ownership (TICKET-1170):
    - Owns protocol-specific session/thread state: _session_id, _thread_id,
      _turn_id, _thread_info.
    - Delegates process lifecycle and client caching to
      WorkspaceAppServerSupervisor.
    - Does NOT participate in active-turn counting (the Codex supervisor has
      no turn-counting concept; idle eviction uses time only).
    """

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
        approval_handler: Optional[ApprovalHandler] = None,
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
        self._approval_handler = approval_handler
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
        self._event_queue: asyncio.Queue[RunEvent] = asyncio.Queue()
        self._active_event_driver: Optional[RuntimeEventDriver] = None
        self._notification_parser_state = RuntimeThreadRunEventState()

    def reset_session_state(self) -> None:
        """Clear cached session/thread ids so the next turn starts fresh."""
        self._session_id = None
        self._thread_id = None
        self._turn_id = None
        self._thread_info = None
        self._active_event_driver = None
        self._notification_parser_state = RuntimeThreadRunEventState()

    async def _ensure_client(self) -> CodexAppServerClient:
        if self._client is None:
            self._client = await self._supervisor.get_client(self._workspace_root)
        self._client.configure_runtime_callbacks(
            approval_handler=self._handle_approval_request,
            notification_handler=self._handle_notification,
            default_approval_decision=self._default_approval_decision,
        )
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
        self._approval_handler = options.get("approval_handler")
        default_approval_decision = options.get("default_approval_decision")
        if (
            isinstance(default_approval_decision, str)
            and default_approval_decision.strip()
        ):
            self._default_approval_decision = default_approval_decision.strip()
        if self._client is not None:
            self._client.configure_runtime_callbacks(
                approval_handler=self._handle_approval_request,
                notification_handler=self._handle_notification,
                default_approval_decision=self._default_approval_decision,
            )

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
        self._active_event_driver = None
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
        self,
        session_id: str,
        message: str,
        *,
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        client = await self._ensure_client()

        if session_id:
            self._thread_id = session_id
            # Reset last turn to avoid interrupting the wrong turn when reusing backends.
            self._turn_id = None
            self._active_event_driver = None

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
            input_items=input_items,
            approval_policy=self._approval_policy,
            sandbox_policy=self._sandbox_policy,
            **turn_kwargs,
        )
        self._turn_id = handle.turn_id

        yield AgentEvent.stream_delta(content=message, delta_type="user_message")

        result = await handle.wait(timeout=self._turn_timeout_seconds)
        runtime_driver = RuntimeEventDriver()
        run_events = await runtime_driver.consume_raw_events(
            getattr(result, "raw_events", ()) or (),
            store_raw_event=False,
        )

        for run_event in run_events:
            agent_event = _agent_event_from_run_event(run_event)
            if agent_event is not None:
                yield agent_event

        final_text = self._final_text_from_result(result, driver=runtime_driver)
        yield AgentEvent.message_complete(final_message=final_text)

    async def run_turn_events(
        self,
        session_id: str,
        message: str,
        *,
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncGenerator[RunEvent, None]:
        client = await self._ensure_client()

        if session_id:
            self._thread_id = session_id
            self._turn_id = None
            self._active_event_driver = None

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
        runtime_driver = RuntimeEventDriver()
        self._active_event_driver = runtime_driver

        turn_kwargs: dict[str, Any] = {}
        if self._model:
            turn_kwargs["model"] = self._model
        if self._reasoning_effort:
            turn_kwargs["effort"] = self._reasoning_effort
        handle = await client.turn_start(
            actual_session_id if actual_session_id else "default",
            text=message,
            input_items=input_items,
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
                    completion_event: Optional[RunEvent] = None
                    if get_task in done_set:
                        completion_event = get_task.result()
                    elif get_task in pending_set:
                        get_task.cancel()
                    result = wait_task.result()
                    if not runtime_driver.run_events and result.raw_events:
                        replayed_events = await runtime_driver.consume_raw_events(
                            result.raw_events,
                            store_raw_event=False,
                        )
                        for replayed_event in replayed_events:
                            yield replayed_event
                    # raw_events already contain the same notifications we streamed
                    # through _event_queue; skipping here avoids double-emitting.
                    if completion_event:
                        yield completion_event
                    while not self._event_queue.empty():
                        extra = self._event_queue.get_nowait()
                        if extra:
                            yield extra
                    final_text = self._final_text_from_result(
                        result,
                        driver=runtime_driver,
                    )
                    yield Completed(
                        timestamp=now_iso(),
                        final_message=final_text,
                    )
                    break

                if get_task in done_set:
                    run_event = get_task.result()
                    if run_event:
                        yield run_event
                    continue
        except Exception as e:  # intentional: top-level turn execution error handler
            _logger.error("Error during turn execution: %s", e)
            if not wait_task.done():
                wait_task.cancel()
            yield Failed(timestamp=now_iso(), error_message=str(e))
        finally:
            if self._active_event_driver is runtime_driver:
                self._active_event_driver = None

    def _final_text_from_result(
        self,
        result: Any,
        *,
        driver: Optional[RuntimeEventDriver] = None,
    ) -> str:
        final_text = str(getattr(result, "final_message", "") or "")
        if final_text.strip():
            return final_text
        aggregated_messages = "\n\n".join(
            msg.strip()
            for msg in getattr(result, "agent_messages", [])
            if isinstance(msg, str) and msg.strip()
        )
        if aggregated_messages.strip():
            return aggregated_messages
        if driver is not None:
            assistant_text = driver.best_assistant_text()
            if assistant_text.strip():
                return assistant_text
        return ""

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
            except (
                Exception
            ) as e:  # intentional: best-effort interrupt, must not propagate
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
        self._active_event_driver = None
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

        if self._approval_handler is not None:
            external_decision = await self._approval_handler(request)
            if isinstance(external_decision, dict):
                return external_decision
            if isinstance(external_decision, str) and external_decision.strip():
                return external_decision.strip()

        decision = self._default_approval_decision.strip().lower()
        return {
            "approve": decision
            in {"accept", "approve", "approved", "allow", "yes", "true"}
        }

    async def _handle_notification(self, notification: Dict[str, Any]) -> None:
        if self._notification_handler is not None:
            try:
                await self._notification_handler(notification)
            except Exception as exc:  # intentional: external notification handler error
                self._logger.debug("Notification handler failed: %s", exc)
        params = notification.get("params", {}) or {}
        thread_id = params.get("threadId") or params.get("thread_id")
        if self._thread_id and thread_id and thread_id != self._thread_id:
            return
        runtime_driver = self._active_event_driver
        if runtime_driver is None:
            return
        _logger.debug("Received notification: %s", notification.get("method", ""))
        for run_event in await runtime_driver.consume_raw_event(
            notification,
            store_raw_event=False,
        ):
            await self._event_queue.put(run_event)

    def _map_to_run_event(self, event_data: Dict[str, Any]) -> Optional[RunEvent]:
        method = str(event_data.get("method") or "").strip()
        params = event_data.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") not in {
                "agentMessage",
                "reasoning",
            }:
                tool_name, tool_input = _normalize_tool_name(params, item=item)
                if tool_name:
                    return ToolCall(
                        timestamp=now_iso(),
                        tool_name=tool_name,
                        tool_input=tool_input,
                    )
        events = normalize_runtime_thread_message_payload(
            event_data,
            self._notification_parser_state,
            timestamp=now_iso(),
        )
        if not events:
            return None
        return events[-1]

    @property
    def last_turn_id(self) -> Optional[str]:
        return self._turn_id

    @property
    def last_thread_info(self) -> Optional[Dict[str, Any]]:
        return self._thread_info
