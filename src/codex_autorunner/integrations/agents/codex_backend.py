import logging
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional, Union

from ...core.circuit_breaker import CircuitBreaker
from ...integrations.app_server.client import CodexAppServerClient
from .agent_backend import AgentBackend, AgentEvent

_logger = logging.getLogger(__name__)

ApprovalDecision = Union[str, Dict[str, Any]]


class CodexAppServerBackend(AgentBackend):
    def __init__(
        self,
        command: list[str],
        *,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        approval_policy: Optional[str] = None,
        sandbox_policy: Optional[str] = None,
    ):
        self._command = command
        self._cwd = cwd
        self._env = env
        self._approval_policy = approval_policy
        self._sandbox_policy = sandbox_policy

        self._client: Optional[CodexAppServerClient] = None
        self._session_id: Optional[str] = None
        self._thread_id: Optional[str] = None

        self._circuit_breaker = CircuitBreaker("CodexAppServer", logger=_logger)

    async def _ensure_client(self) -> CodexAppServerClient:
        if self._client is None:
            self._client = CodexAppServerClient(
                self._command,
                cwd=self._cwd,
                env=self._env,
                approval_handler=self._handle_approval_request,
                notification_handler=self._handle_notification,
            )
            await self._client.start()
        return self._client

    async def start_session(self, target: dict, context: dict) -> str:
        client = await self._ensure_client()

        repo_root = Path(context.get("workspace") or self._cwd or Path.cwd())

        result = await client.thread_start(str(repo_root))
        self._thread_id = result.get("id")

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

        if not self._thread_id:
            await self.start_session(target={}, context={})

        _logger.info(
            "Running turn on thread %s with message: %s",
            self._thread_id or "unknown",
            message[:100],
        )

        handle = await client.turn_start(
            self._thread_id if self._thread_id else "default",
            text=message,
            approval_policy=self._approval_policy,
            sandbox_policy=self._sandbox_policy,
        )

        yield AgentEvent.stream_delta(content=message, delta_type="user_message")

        result = await handle.wait(timeout=600.0)

        for msg in result.agent_messages:
            yield AgentEvent.stream_delta(content=msg, delta_type="assistant_message")

        for event_data in result.raw_events:
            yield self._parse_raw_event(event_data)

        yield AgentEvent.message_complete(
            final_message="\n".join(result.agent_messages)
        )

    async def stream_events(self, session_id: str) -> AsyncGenerator[AgentEvent, None]:
        if False:
            yield AgentEvent.stream_delta(content="", delta_type="noop")

    async def interrupt(self, session_id: str) -> None:
        target_thread = session_id or self._thread_id
        if self._client and target_thread:
            try:
                await self._client.turn_interrupt(target_thread)
                _logger.info("Interrupted turn on thread %s", target_thread)
            except Exception as e:
                _logger.warning("Failed to interrupt turn: %s", e)

    async def final_messages(self, session_id: str) -> list[str]:
        return []

    async def request_approval(
        self, description: str, context: Optional[Dict[str, Any]] = None
    ) -> bool:
        raise NotImplementedError(
            "Approvals are handled via approval_handler in CodexAppServerBackend"
        )

    async def _handle_approval_request(
        self, request: Dict[str, Any]
    ) -> ApprovalDecision:
        method = request.get("method", "")
        item_type = request.get("params", {}).get("type", "")

        _logger.info("Received approval request: %s (type=%s)", method, item_type)

        return {"approve": True}

    async def _handle_notification(self, notification: Dict[str, Any]) -> None:
        method = notification.get("method", "")
        _logger.debug("Received notification: %s", method)

        if method == "turn/streamDelta":
            content = notification.get("params", {}).get("delta", "")
            _logger.info("Stream delta: %s", content[:100])

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
