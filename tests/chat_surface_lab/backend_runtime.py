from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
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
_CONTROL_CANCELLED = object()


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


def fake_opencode_server_command(scenario: str = "smoke") -> list[str]:
    return [
        sys.executable,
        "-u",
        str(FAKE_OPENCODE_FIXTURE_PATH),
        "--scenario",
        scenario,
    ]


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
        self._pending_controls: dict[str, asyncio.Future[Any]] = {}

    async def start(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root

    async def create_conversation(self) -> str:
        raise NotImplementedError

    async def start_turn(self, conversation_id: str, text: str) -> str:
        raise NotImplementedError

    async def interrupt(self, conversation_id: str, turn_id: str) -> None:
        raise NotImplementedError

    async def respond_to_control(self, control_id: str, decision: Any) -> None:
        future = self._pending_controls.get(control_id)
        if future is None:
            raise RuntimeError(f"Unknown control id: {control_id}")
        if not future.done():
            future.set_result(decision)

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

    async def _wait_for_control(self, control_id: str) -> Any:
        future = asyncio.get_running_loop().create_future()
        self._pending_controls[control_id] = future
        try:
            return await future
        finally:
            self._pending_controls.pop(control_id, None)

    async def _cancel_pending_controls(self) -> None:
        for future in list(self._pending_controls.values()):
            if not future.done():
                future.set_result(_CONTROL_CANCELLED)


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
            question_handler=self._handle_question_request,
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
        decision = await self._wait_for_control(control_id)
        return _normalize_decision(decision if isinstance(decision, str) else "")

    async def _handle_question_request(self, message: dict[str, Any]) -> dict[str, Any]:
        params = message if isinstance(message, dict) else {}
        payload = (
            params.get("params") if isinstance(params.get("params"), dict) else params
        )
        control_id = str(message.get("id") or "").strip()
        if not control_id:
            return {"answers": {}}
        turn_id = str(payload.get("turnId") or "").strip() or None
        conversation_id = (
            str(payload.get("threadId") or "").strip() or None
        ) or self._turn_conversations.get(turn_id or "")
        questions_raw = payload.get("questions")
        questions = (
            [question for question in questions_raw if isinstance(question, dict)]
            if isinstance(questions_raw, list)
            else []
        )
        prompt = ""
        if questions:
            first_question = questions[0]
            prompt = str(
                first_question.get("question")
                or first_question.get("prompt")
                or first_question.get("header")
                or ""
            )
        await self._publish(
            backend=self.backend_name,
            kind="control.question_requested",
            conversation_id=conversation_id,
            turn_id=turn_id,
            control_id=control_id,
            text=prompt or "Question requested",
            payload={**payload, "questions": questions},
        )
        response = await self._wait_for_control(control_id)
        if response is _CONTROL_CANCELLED:
            return {"answers": {}}
        return _normalize_codex_question_response(questions, response)

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
        normalized = _normalize_decision(decision if isinstance(decision, str) else "")
        if normalized == "approve":
            return "allow"
        if normalized == "deny":
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

    def __init__(self, *, scenario: str = "smoke") -> None:
        super().__init__()
        self._scenario = scenario
        supports_turns = scenario == "question"
        self.capabilities = BackendRuntimeCapabilities(
            can_create_conversation=supports_turns,
            can_start_turn=supports_turns,
            can_interrupt=False,
            can_respond_to_control=supports_turns,
            streams_events=True,
        )
        self._supervisor: Optional[OpenCodeSupervisor] = None
        self._client: Optional[OpenCodeClient] = None
        self._openapi_spec: Optional[dict[str, Any]] = None
        self._turn_tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self, workspace_root: Path) -> None:
        await super().start(workspace_root)
        self._supervisor = OpenCodeSupervisor(
            fake_opencode_server_command(self._scenario),
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
        if not self.capabilities.can_create_conversation or self._client is None:
            raise RuntimeError("OpenCode fixture runtime does not create sessions")
        session = await self._client.create_session(directory=str(self._workspace_root))
        conversation_id = _extract_opencode_session_id(session)
        if not conversation_id:
            raise RuntimeError("OpenCode fixture did not return a session id")
        await self._publish(
            backend=self.backend_name,
            kind="conversation.started",
            conversation_id=conversation_id,
            payload=session if isinstance(session, dict) else {},
        )
        return conversation_id

    async def start_turn(self, conversation_id: str, text: str) -> str:
        if not self.capabilities.can_start_turn or self._client is None:
            raise RuntimeError("OpenCode fixture runtime does not start turns")
        turn_id = f"{conversation_id}:turn:{uuid.uuid4().hex[:8]}"
        task = asyncio.create_task(
            self._run_question_turn(
                conversation_id=conversation_id,
                turn_id=turn_id,
                text=text,
            )
        )
        self._turn_tasks[turn_id] = task
        task.add_done_callback(
            lambda _task, tid=turn_id: self._turn_tasks.pop(tid, None)
        )
        await self._publish(
            backend=self.backend_name,
            kind="turn.started",
            conversation_id=conversation_id,
            turn_id=turn_id,
            payload={"input": text},
        )
        return turn_id

    async def interrupt(self, conversation_id: str, turn_id: str) -> None:
        raise RuntimeError("OpenCode fixture smoke runtime does not interrupt turns")

    async def shutdown(self) -> None:
        await self._cancel_pending_controls()
        for task in list(self._turn_tasks.values()):
            task.cancel()
        if self._turn_tasks:
            await asyncio.gather(
                *tuple(self._turn_tasks.values()), return_exceptions=True
            )
        self._turn_tasks.clear()
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._supervisor is not None:
            await self._supervisor.close_all()
            self._supervisor = None
        await self._publish(backend=self.backend_name, kind="runtime.closed")

    async def _run_question_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        text: str,
    ) -> None:
        assert self._client is not None
        stream_ready = asyncio.Event()

        async def _stream_events() -> None:
            async for event in self._client.stream_events(
                directory=str(self._workspace_root),
                session_id=conversation_id,
            ):
                stream_ready.set()
                payload = {}
                try:
                    payload = json.loads(event.data) if event.data else {}
                except Exception:
                    payload = {}
                props = payload.get("properties") if isinstance(payload, dict) else None
                props = props if isinstance(props, dict) else {}
                event_turn_id = str(props.get("turnID") or "").strip()
                if event_turn_id and event_turn_id != turn_id:
                    continue
                if event.event == "question.asked":
                    request_id = str(props.get("id") or "").strip()
                    questions_raw = props.get("questions")
                    questions = (
                        [
                            question
                            for question in questions_raw
                            if isinstance(question, dict)
                        ]
                        if isinstance(questions_raw, list)
                        else []
                    )
                    prompt = ""
                    if questions:
                        first_question = questions[0]
                        prompt = str(
                            first_question.get("question")
                            or first_question.get("prompt")
                            or first_question.get("header")
                            or ""
                        )
                    await self._publish(
                        backend=self.backend_name,
                        kind="control.question_requested",
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        control_id=request_id,
                        text=prompt or "Question requested",
                        payload={**props, "questions": questions},
                    )
                    response = await self._wait_for_control(request_id)
                    if response is _CONTROL_CANCELLED:
                        await self._client.reject_question(request_id)
                        continue
                    answers = _normalize_opencode_question_answers(questions, response)
                    await self._client.reply_question(request_id, answers=answers)
                    continue
                if event.event in {"message.part.delta", "message.part.updated"}:
                    text_part = _extract_opencode_visible_text(event.event, payload)
                    if text_part:
                        await self._publish(
                            backend=self.backend_name,
                            kind="turn.output",
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            text=text_part,
                            payload=payload if isinstance(payload, dict) else {},
                        )
                if event.event in {
                    "session.idle",
                    "session.status",
                } and _opencode_event_is_idle(payload):
                    await self._publish(
                        backend=self.backend_name,
                        kind="turn.terminal",
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        status="completed",
                        payload=payload if isinstance(payload, dict) else {},
                    )
                    return

        stream_task = asyncio.create_task(_stream_events())
        try:
            await asyncio.wait_for(stream_ready.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        prompt_task = asyncio.create_task(
            self._client.prompt_async(
                conversation_id,
                message=text,
                variant=turn_id,
            )
        )
        try:
            await asyncio.gather(prompt_task, stream_task)
        finally:
            if not prompt_task.done():
                prompt_task.cancel()
                await asyncio.gather(prompt_task, return_exceptions=True)
            if not stream_task.done():
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)


def _normalize_codex_question_response(
    questions: list[dict[str, Any]], response: Any
) -> dict[str, Any]:
    if isinstance(response, dict):
        answers_raw = response.get("answers")
        if isinstance(answers_raw, dict):
            normalized: dict[str, dict[str, list[str]]] = {}
            for question_id, answer_payload in answers_raw.items():
                if not isinstance(question_id, str) or not question_id.strip():
                    continue
                if isinstance(answer_payload, dict):
                    values = answer_payload.get("answers")
                else:
                    values = answer_payload
                normalized[question_id.strip()] = {
                    "answers": _normalize_answer_list(values)
                }
            return {"answers": normalized}
    if isinstance(response, list):
        normalized = {}
        for index, question in enumerate(questions):
            question_id = question.get("id")
            if not isinstance(question_id, str) or not question_id.strip():
                continue
            values = response[index] if index < len(response) else []
            normalized[question_id.strip()] = {
                "answers": _normalize_answer_list(values)
            }
        return {"answers": normalized}
    if isinstance(response, str):
        for question in questions:
            question_id = question.get("id")
            if isinstance(question_id, str) and question_id.strip():
                return {"answers": {question_id.strip(): {"answers": [response]}}}
    return {
        "answers": {
            str(question.get("id")).strip(): {"answers": []}
            for question in questions
            if isinstance(question.get("id"), str) and str(question.get("id")).strip()
        }
    }


def _normalize_opencode_question_answers(
    questions: list[dict[str, Any]], response: Any
) -> list[list[str]]:
    if isinstance(response, dict):
        answers_raw = response.get("answers")
        if isinstance(answers_raw, dict):
            normalized: list[list[str]] = []
            for question in questions:
                question_id = question.get("id")
                values = (
                    answers_raw.get(question_id) if isinstance(question_id, str) else []
                )
                if isinstance(values, dict):
                    values = values.get("answers")
                normalized.append(_normalize_answer_list(values))
            return normalized
    if isinstance(response, list):
        return [_normalize_answer_list(values) for values in response]
    if isinstance(response, str):
        return [[response]]
    return [[] for _ in questions]


def _normalize_answer_list(values: Any) -> list[str]:
    if isinstance(values, list):
        return [str(value) for value in values if isinstance(value, str) and value]
    if isinstance(values, str) and values:
        return [values]
    return []


def _extract_opencode_visible_text(event_name: str, payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    props = payload.get("properties")
    if not isinstance(props, dict):
        return ""
    if event_name == "message.part.delta":
        delta = props.get("delta")
        return delta if isinstance(delta, str) else ""
    part = props.get("part")
    if not isinstance(part, dict):
        return ""
    if part.get("type") != "text":
        return ""
    text = part.get("text")
    return text if isinstance(text, str) else ""


def _opencode_event_is_idle(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("type") == "session.idle":
        return True
    props = payload.get("properties")
    if not isinstance(props, dict):
        return False
    status = props.get("status")
    if isinstance(status, dict):
        status = status.get("type")
    return isinstance(status, str) and status.strip().lower() == "idle"


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
