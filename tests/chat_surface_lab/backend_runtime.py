from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from codex_autorunner.agents.acp.client import ACPClient
from codex_autorunner.agents.acp.events import (
    ACPEvent,
    ACPMessageEvent,
    ACPOutputDeltaEvent,
    ACPPermissionRequestEvent,
    ACPProgressEvent,
    ACPTurnTerminalEvent,
)
from codex_autorunner.agents.hermes.harness import HERMES_CAPABILITIES, HermesHarness
from codex_autorunner.agents.hermes.supervisor import HermesSupervisor
from codex_autorunner.agents.opencode.client import OpenCodeClient
from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisor
from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.integrations.app_server.client import CodexAppServerClient
from codex_autorunner.integrations.app_server.protocol_helpers import (
    normalize_approval_request,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
APP_SERVER_FIXTURE_PATH = FIXTURES_DIR / "app_server_fixture.py"
FAKE_ACP_FIXTURE_PATH = FIXTURES_DIR / "fake_acp_server.py"
FAKE_OPENCODE_FIXTURE_PATH = FIXTURES_DIR / "fake_opencode_server.py"


def app_server_fixture_command(scenario: str = "basic") -> list[str]:
    return [
        sys.executable,
        "-u",
        str(APP_SERVER_FIXTURE_PATH),
        "--scenario",
        scenario,
    ]


def fake_acp_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FAKE_ACP_FIXTURE_PATH), "--scenario", scenario]


def fake_opencode_server_command() -> list[str]:
    return [sys.executable, "-u", str(FAKE_OPENCODE_FIXTURE_PATH)]


@dataclass
class HermesFixtureRuntime:
    scenario: str
    logger_name: str = "test.chat_surface_integration.hermes"
    _descriptor: Optional[AgentDescriptor] = field(default=None, init=False)
    _supervisor: Optional[HermesSupervisor] = field(default=None, init=False)

    @property
    def supervisor(self) -> HermesSupervisor:
        if self._supervisor is None:
            self._supervisor = HermesSupervisor(
                fake_acp_command(self.scenario),
                logger=logging.getLogger(self.logger_name),
            )
        return self._supervisor

    def descriptor(self) -> AgentDescriptor:
        if self._descriptor is None:
            self._descriptor = AgentDescriptor(
                id="hermes",
                name="Hermes",
                capabilities=HERMES_CAPABILITIES,
                runtime_kind="hermes",
                make_harness=lambda _ctx: HermesHarness(self.supervisor),
            )
        return self._descriptor

    def registered_agents(self) -> dict[str, AgentDescriptor]:
        return {"hermes": self.descriptor()}

    async def close(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.close_all()
            self._supervisor = None


@dataclass(frozen=True)
class BackendRuntimeCapabilities:
    can_create_conversation: bool = True
    can_start_turn: bool = True
    can_interrupt: bool = True
    can_respond_to_control: bool = True
    streams_events: bool = True


@dataclass(frozen=True)
class BackendRuntimeEvent:
    backend: str
    kind: str
    conversation_id: Optional[str] = None
    turn_id: Optional[str] = None
    control_id: Optional[str] = None
    status: Optional[str] = None
    text: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)


def _normalize_decision(decision: str) -> str:
    value = str(decision or "").strip().lower()
    if value in {"approve", "approved", "accept", "accepted", "allow", "allowed"}:
        return "approve"
    if value in {"deny", "denied", "decline", "declined", "reject", "rejected"}:
        return "deny"
    return "cancel"


def _normalize_terminal_status(status: Any) -> str:
    value = str(status or "").strip().lower()
    if value in {"completed", "complete", "ok", "done", "end_turn"}:
        return "completed"
    if value in {"interrupted", "cancelled", "canceled", "aborted", "stopped"}:
        return "interrupted"
    if value in {"failed", "error"}:
        return "failed"
    return value or "unknown"


def _extract_opencode_session_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "sessionId", "sessionID", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    session = payload.get("session")
    if isinstance(session, dict):
        return _extract_opencode_session_id(session)
    return None


class BaseBackendFixtureRuntime:
    backend_name = "unknown"

    def __init__(self) -> None:
        self.capabilities = BackendRuntimeCapabilities()
        self._event_queue: asyncio.Queue[BackendRuntimeEvent] = asyncio.Queue()
        self._event_backlog: list[BackendRuntimeEvent] = []
        self._workspace_root: Optional[Path] = None
        self._pending_controls: dict[str, asyncio.Future[str]] = {}

    async def start(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root

    async def create_conversation(self) -> str:
        raise NotImplementedError

    async def start_turn(self, conversation_id: str, text: str) -> str:
        raise NotImplementedError

    async def interrupt(self, conversation_id: str, turn_id: str) -> None:
        raise NotImplementedError

    async def respond_to_control(self, control_id: str, decision: str) -> None:
        future = self._pending_controls.get(control_id)
        if future is None:
            raise RuntimeError(f"Unknown control id: {control_id}")
        if not future.done():
            future.set_result(_normalize_decision(decision))

    async def next_event(self, *, timeout: float = 2.0) -> BackendRuntimeEvent:
        if self._event_backlog:
            return self._event_backlog.pop(0)
        return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)

    async def wait_for_event(
        self,
        predicate: Callable[[BackendRuntimeEvent], bool],
        *,
        timeout: float = 2.0,
    ) -> BackendRuntimeEvent:
        deadline = asyncio.get_running_loop().time() + max(timeout, 0.0)
        while True:
            for index, event in enumerate(self._event_backlog):
                if predicate(event):
                    return self._event_backlog.pop(index)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for backend runtime event")
            event = await asyncio.wait_for(self._event_queue.get(), timeout=remaining)
            if predicate(event):
                return event
            self._event_backlog.append(event)

    async def events(self) -> AsyncIterator[BackendRuntimeEvent]:
        while True:
            event = await self.next_event()
            yield event
            if event.kind == "runtime.closed":
                return

    async def shutdown(self) -> None:
        raise NotImplementedError

    async def _publish(self, **kwargs: Any) -> None:
        await self._event_queue.put(BackendRuntimeEvent(**kwargs))

    async def _wait_for_control(self, control_id: str) -> str:
        future = asyncio.get_running_loop().create_future()
        self._pending_controls[control_id] = future
        try:
            return await future
        finally:
            self._pending_controls.pop(control_id, None)

    async def _cancel_pending_controls(self) -> None:
        for future in list(self._pending_controls.values()):
            if not future.done():
                future.set_result("cancel")


class CodexAppServerFixtureRuntime(BaseBackendFixtureRuntime):
    backend_name = "codex_app_server"

    def __init__(self, *, scenario: str = "basic") -> None:
        super().__init__()
        self._scenario = scenario
        self._client: Optional[CodexAppServerClient] = None
        self._turn_conversations: dict[str, str] = {}

    async def start(self, workspace_root: Path) -> None:
        await super().start(workspace_root)
        self._client = CodexAppServerClient(
            app_server_fixture_command(self._scenario),
            cwd=workspace_root,
            approval_handler=self._handle_approval_request,
            notification_handler=self._handle_notification,
            auto_restart=False,
            request_timeout=2.0,
        )
        await self._client.start()
        await self._publish(
            backend=self.backend_name,
            kind="runtime.ready",
            payload={"scenario": self._scenario},
        )

    async def create_conversation(self) -> str:
        if self._client is None or self._workspace_root is None:
            raise RuntimeError("Runtime not started")
        result = await self._client.thread_start(cwd=str(self._workspace_root))
        conversation_id = str(result.get("id") or "").strip()
        if not conversation_id:
            raise RuntimeError("thread/start did not return a conversation id")
        await self._publish(
            backend=self.backend_name,
            kind="conversation.started",
            conversation_id=conversation_id,
            payload=result,
        )
        return conversation_id

    async def start_turn(self, conversation_id: str, text: str) -> str:
        if self._client is None:
            raise RuntimeError("Runtime not started")
        handle = await self._client.turn_start(conversation_id, text)
        self._turn_conversations[handle.turn_id] = conversation_id
        await self._publish(
            backend=self.backend_name,
            kind="turn.started",
            conversation_id=conversation_id,
            turn_id=handle.turn_id,
            payload={"input": text},
        )
        return handle.turn_id

    async def interrupt(self, conversation_id: str, turn_id: str) -> None:
        if self._client is None:
            raise RuntimeError("Runtime not started")
        await self._client.turn_interrupt(turn_id, thread_id=conversation_id)

    async def shutdown(self) -> None:
        await self._cancel_pending_controls()
        if self._client is not None:
            await self._client.close()
            self._client = None
        await self._publish(backend=self.backend_name, kind="runtime.closed")

    async def _handle_approval_request(self, message: dict[str, Any]) -> str:
        approval = normalize_approval_request(message)
        if approval is None:
            return "cancel"
        control_id = str(approval.request_id)
        turn_id = str(approval.params.get("turnId") or "").strip() or None
        await self._publish(
            backend=self.backend_name,
            kind="control.approval_requested",
            conversation_id=(str(approval.params.get("threadId") or "").strip() or None)
            or (self._turn_conversations.get(turn_id or "")),
            turn_id=turn_id,
            control_id=control_id,
            text=str(approval.params.get("reason") or approval.method),
            payload=approval.params,
        )
        return await self._wait_for_control(control_id)

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "").strip()
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        turn_id = str(params.get("turnId") or "").strip() or None
        conversation_id = (
            str(params.get("threadId") or "").strip() or None
        ) or self._turn_conversations.get(turn_id or "")

        if method == "item/agentMessage/delta":
            await self._publish(
                backend=self.backend_name,
                kind="turn.output",
                conversation_id=conversation_id,
                turn_id=turn_id,
                text=str(params.get("delta") or ""),
                payload=params,
            )
            return

        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict):
                text = str(
                    item.get("text") or item.get("review") or item.get("content") or ""
                )
                if text:
                    await self._publish(
                        backend=self.backend_name,
                        kind="turn.output",
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        text=text,
                        payload=params,
                    )
            return

        if method == "turn/completed":
            await self._publish(
                backend=self.backend_name,
                kind="turn.terminal",
                conversation_id=conversation_id,
                turn_id=turn_id,
                status=_normalize_terminal_status(params.get("status")),
                text=str(params.get("error") or ""),
                payload=params,
            )
            return

        if method == "error":
            error_payload = params.get("error")
            error_message = ""
            if isinstance(error_payload, dict):
                error_message = str(error_payload.get("message") or "")
            await self._publish(
                backend=self.backend_name,
                kind="turn.terminal",
                conversation_id=conversation_id,
                turn_id=turn_id,
                status="failed",
                text=error_message,
                payload=params,
            )


class ACPFixtureRuntime(BaseBackendFixtureRuntime):
    backend_name = "hermes_acp"

    def __init__(self, *, scenario: str = "official") -> None:
        super().__init__()
        self._scenario = scenario
        self._client: Optional[ACPClient] = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._terminal_turns: set[str] = set()

    async def start(self, workspace_root: Path) -> None:
        await super().start(workspace_root)
        self._client = ACPClient(
            fake_acp_command(self._scenario),
            cwd=workspace_root,
            notification_handler=self._handle_notification,
            permission_handler=self._handle_permission_request,
            request_timeout=2.0,
        )
        await self._client.start()
        await self._publish(
            backend=self.backend_name,
            kind="runtime.ready",
            payload={"scenario": self._scenario},
        )

    async def create_conversation(self) -> str:
        if self._client is None or self._workspace_root is None:
            raise RuntimeError("Runtime not started")
        session = await self._client.create_session(cwd=str(self._workspace_root))
        conversation_id = session.session_id
        await self._publish(
            backend=self.backend_name,
            kind="conversation.started",
            conversation_id=conversation_id,
            payload=dict(session.raw),
        )
        return conversation_id

    async def start_turn(self, conversation_id: str, text: str) -> str:
        if self._client is None:
            raise RuntimeError("Runtime not started")
        handle = await self._client.start_prompt(conversation_id, text)
        task = asyncio.create_task(
            self._publish_terminal_from_prompt_handle(
                conversation_id=conversation_id,
                turn_id=handle.turn_id,
                handle=handle,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        await self._publish(
            backend=self.backend_name,
            kind="turn.started",
            conversation_id=conversation_id,
            turn_id=handle.turn_id,
            payload={"input": text},
        )
        return handle.turn_id

    async def interrupt(self, conversation_id: str, turn_id: str) -> None:
        if self._client is None:
            raise RuntimeError("Runtime not started")
        await self._client.cancel_prompt(conversation_id, turn_id)

    async def shutdown(self) -> None:
        await self._cancel_pending_controls()
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*tuple(self._background_tasks), return_exceptions=True)
        self._background_tasks.clear()
        if self._client is not None:
            await self._client.close()
            self._client = None
        await self._publish(backend=self.backend_name, kind="runtime.closed")

    async def _handle_permission_request(self, event: ACPPermissionRequestEvent) -> str:
        control_id = str(event.request_id)
        await self._publish(
            backend=self.backend_name,
            kind="control.approval_requested",
            conversation_id=event.session_id,
            turn_id=event.turn_id,
            control_id=control_id,
            text=event.description,
            payload=event.payload,
        )
        decision = await self._wait_for_control(control_id)
        if decision == "approve":
            return "allow"
        if decision == "deny":
            return "deny"
        return "cancel"

    async def _handle_notification(self, event: ACPEvent) -> None:
        if isinstance(event, ACPOutputDeltaEvent):
            await self._publish(
                backend=self.backend_name,
                kind="turn.output",
                conversation_id=event.session_id,
                turn_id=event.turn_id,
                text=event.delta,
                payload=event.payload,
            )
            return

        if isinstance(event, (ACPMessageEvent, ACPProgressEvent)):
            text = getattr(event, "message", "")
            if text:
                await self._publish(
                    backend=self.backend_name,
                    kind="turn.output",
                    conversation_id=event.session_id,
                    turn_id=event.turn_id,
                    text=text,
                    payload=event.payload,
                )
            return

        if isinstance(event, ACPTurnTerminalEvent):
            self._terminal_turns.add(event.turn_id or "")
            await self._publish(
                backend=self.backend_name,
                kind="turn.terminal",
                conversation_id=event.session_id,
                turn_id=event.turn_id,
                status=_normalize_terminal_status(event.status),
                text=event.final_output or event.error_message or "",
                payload=event.payload,
            )

    async def _publish_terminal_from_prompt_handle(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        handle: Any,
    ) -> None:
        try:
            result = await handle.wait(timeout=4.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        if turn_id in self._terminal_turns:
            return
        self._terminal_turns.add(turn_id)
        await self._publish(
            backend=self.backend_name,
            kind="turn.terminal",
            conversation_id=conversation_id,
            turn_id=turn_id,
            status=_normalize_terminal_status(getattr(result, "status", None)),
            text=(
                getattr(result, "final_output", None)
                or getattr(result, "error_message", None)
                or ""
            ),
            payload={
                "status": getattr(result, "status", None),
                "finalOutput": getattr(result, "final_output", None),
                "error": getattr(result, "error_message", None),
            },
        )


class OpenCodeFixtureRuntime(BaseBackendFixtureRuntime):
    backend_name = "opencode"

    def __init__(self) -> None:
        super().__init__()
        self.capabilities = BackendRuntimeCapabilities(
            can_create_conversation=False,
            can_start_turn=False,
            can_interrupt=False,
            can_respond_to_control=False,
            streams_events=True,
        )
        self._supervisor: Optional[OpenCodeSupervisor] = None
        self._client: Optional[OpenCodeClient] = None
        self._openapi_spec: Optional[dict[str, Any]] = None

    async def start(self, workspace_root: Path) -> None:
        await super().start(workspace_root)
        self._supervisor = OpenCodeSupervisor(
            fake_opencode_server_command(),
            request_timeout=5.0,
        )
        self._client = await self._supervisor.get_client(workspace_root)
        self._openapi_spec = await self._client.fetch_openapi_spec()
        health = await self._client.health()
        await self._publish(
            backend=self.backend_name,
            kind="runtime.ready",
            payload={
                "health": health,
                "supports_global_endpoints": self._client.has_endpoint(
                    self._openapi_spec,
                    "get",
                    "/global/health",
                ),
            },
        )

    async def create_conversation(self) -> str:
        raise RuntimeError("OpenCode fixture smoke runtime does not create sessions")

    async def start_turn(self, conversation_id: str, text: str) -> str:
        raise RuntimeError("OpenCode fixture smoke runtime does not start turns")

    async def interrupt(self, conversation_id: str, turn_id: str) -> None:
        raise RuntimeError("OpenCode fixture smoke runtime does not interrupt turns")

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._supervisor is not None:
            await self._supervisor.close_all()
            self._supervisor = None
        await self._publish(backend=self.backend_name, kind="runtime.closed")


__all__ = [
    "ACPFixtureRuntime",
    "APP_SERVER_FIXTURE_PATH",
    "BackendRuntimeCapabilities",
    "BackendRuntimeEvent",
    "BaseBackendFixtureRuntime",
    "CodexAppServerFixtureRuntime",
    "FAKE_ACP_FIXTURE_PATH",
    "FAKE_OPENCODE_FIXTURE_PATH",
    "FIXTURES_DIR",
    "HermesFixtureRuntime",
    "OpenCodeFixtureRuntime",
    "app_server_fixture_command",
    "fake_acp_command",
    "fake_opencode_server_command",
]
