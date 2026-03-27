from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Sequence

from .errors import (
    ACPError,
    ACPInitializationError,
    ACPMethodNotFoundError,
    ACPProcessCrashedError,
    ACPProtocolError,
    ACPResponseError,
    ACPTransportError,
)
from .events import (
    ACPEvent,
    ACPMessageEvent,
    ACPOutputDeltaEvent,
    ACPPermissionRequestEvent,
    ACPTurnTerminalEvent,
    normalize_notification,
)
from .protocol import (
    ACPInitializeResult,
    ACPPromptDescriptor,
    ACPSessionDescriptor,
    coerce_session_list,
)

NotificationHandler = Callable[[ACPEvent], Awaitable[None]]
PermissionHandler = Callable[[ACPPermissionRequestEvent], Awaitable[Any]]
_QUEUE_SENTINEL = object()

_APPROVAL_ALLOW_DECISIONS = frozenset(
    {"accept", "accepted", "allow", "allowed", "approve", "approved", "yes", "true"}
)
_APPROVAL_DENY_DECISIONS = frozenset(
    {"decline", "declined", "deny", "denied", "reject", "rejected", "no", "false"}
)
_APPROVAL_CANCEL_DECISIONS = frozenset(
    {"cancel", "cancelled", "canceled", "timeout", "timed_out"}
)
_PERMISSION_NOTIFICATION_METHODS = (
    "permission/respond",
    "permission/reply",
    "permission/resolve",
)


@dataclass(frozen=True)
class ACPPromptResult:
    session_id: str
    turn_id: str
    status: str
    final_output: str
    error_message: Optional[str] = None
    events: tuple[ACPEvent, ...] = ()


@dataclass
class _PromptState:
    session_id: str
    turn_id: str
    queue: asyncio.Queue[object] = field(default_factory=asyncio.Queue)
    future: asyncio.Future[ACPPromptResult] = field(default_factory=asyncio.Future)
    events: list[ACPEvent] = field(default_factory=list)
    final_output: str = ""
    closed: bool = False


def _normalize_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _stringify(value: Any) -> str:
    return str(value or "").strip().lower()


def _decision_label(value: Any) -> str:
    normalized = _stringify(value)
    if normalized in _APPROVAL_ALLOW_DECISIONS:
        return "allow"
    if normalized in _APPROVAL_DENY_DECISIONS:
        return "deny"
    if normalized in _APPROVAL_CANCEL_DECISIONS:
        return "cancel"
    return ""


def _select_permission_option_id(options: Any, decision: str) -> Optional[str]:
    if not isinstance(options, list):
        return None
    matched_ids: list[str] = []
    fallback_ids: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        option_id = _normalize_optional_text(
            option.get("optionId")
            or option.get("option_id")
            or option.get("id")
            or option.get("value")
        )
        if not option_id:
            continue
        fallback_ids.append(option_id)
        label = " ".join(
            part
            for part in (
                _normalize_optional_text(option.get("optionId"))
                or _normalize_optional_text(option.get("option_id"))
                or _normalize_optional_text(option.get("id")),
                _normalize_optional_text(option.get("label")),
                _normalize_optional_text(option.get("title")),
                _normalize_optional_text(option.get("description")),
            )
            if part
        ).lower()
        if decision == "allow" and any(
            token in label for token in ("allow", "approve", "accept", "yes")
        ):
            matched_ids.append(option_id)
        elif decision == "deny" and any(
            token in label for token in ("deny", "decline", "reject", "no")
        ):
            matched_ids.append(option_id)
    if matched_ids:
        return matched_ids[0]
    if len(fallback_ids) == 2 and decision in {"allow", "deny"}:
        return fallback_ids[0] if decision == "allow" else fallback_ids[1]
    return None


def _permission_outcome_payload(
    event: ACPPermissionRequestEvent, decision: Any
) -> dict[str, Any]:
    normalized = _decision_label(decision)
    if normalized == "cancel":
        return {"outcome": {"outcome": "cancelled"}}
    option_id = _select_permission_option_id(event.payload.get("options"), normalized)
    if option_id:
        return {"outcome": {"outcome": "selected", "optionId": option_id}}
    if normalized == "allow":
        return {"outcome": {"outcome": "selected", "optionId": "allow"}}
    if normalized == "deny":
        return {"outcome": {"outcome": "selected", "optionId": "deny"}}
    return {"outcome": {"outcome": "cancelled"}}


def _build_transport_error_message(
    *,
    returncode: Optional[int],
    stderr_tail: deque[str],
) -> str:
    message = "ACP subprocess disconnected"
    if returncode is not None:
        message = f"ACP subprocess exited with code {returncode}"
    if stderr_tail:
        message = f"{message}: {' | '.join(stderr_tail)}"
    return message


class ACPPromptHandle:
    def __init__(self, client: "ACPClient", turn_id: str) -> None:
        self._client = client
        self.turn_id = turn_id

    async def wait(self, *, timeout: Optional[float] = None) -> ACPPromptResult:
        return await self._client.wait_for_prompt(self.turn_id, timeout=timeout)

    async def events(self) -> AsyncIterator[ACPEvent]:
        async for event in self._client.iter_prompt_events(self.turn_id):
            yield event


class ACPClient:
    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[Path] = None,
        env: Optional[dict[str, str]] = None,
        initialize_params: Optional[dict[str, Any]] = None,
        request_timeout: Optional[float] = None,
        notification_handler: Optional[NotificationHandler] = None,
        permission_handler: Optional[PermissionHandler] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if not command:
            raise ValueError("ACP command must not be empty")
        self._command = [str(part) for part in command]
        self._cwd = str(cwd) if cwd is not None else None
        self._env = dict(env) if env is not None else None
        self._initialize_params = dict(initialize_params or {})
        self._request_timeout = request_timeout
        self._notification_handler = notification_handler
        self._permission_handler = permission_handler
        self._logger = logger or logging.getLogger(__name__)

        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._wait_task: Optional[asyncio.Task[None]] = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._pending_methods: dict[str, str] = {}
        self._next_id = 0
        self._initialized = False
        self._closed = False
        self._notifications: asyncio.Queue[object] = asyncio.Queue()
        self._prompts: dict[str, _PromptState] = {}
        self._orphan_events: dict[str, list[ACPEvent]] = defaultdict(list)
        self._stderr_tail: deque[str] = deque(maxlen=5)
        self._initialize_result: Optional[ACPInitializeResult] = None
        self._disconnect_error: Optional[ACPError] = None
        self._closing = False

    @property
    def initialize_result(self) -> Optional[ACPInitializeResult]:
        return self._initialize_result

    async def start(self) -> ACPInitializeResult:
        async with self._start_lock:
            if self._closed:
                raise ACPTransportError("ACP client is already closed")
            if self._process is None:
                process = await asyncio.create_subprocess_exec(
                    *self._command,
                    cwd=self._cwd,
                    env=self._env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                if (
                    process.stdin is None
                    or process.stdout is None
                    or process.stderr is None
                ):
                    raise ACPTransportError(
                        "ACP subprocess did not expose stdin/stdout/stderr pipes"
                    )
                self._process = process
                self._reader_task = asyncio.create_task(self._read_stdout_loop())
                self._stderr_task = asyncio.create_task(self._read_stderr_loop())
                self._wait_task = asyncio.create_task(self._wait_for_process_exit())
            if not self._initialized:
                try:
                    initialize_payload = await self._request_after_start(
                        "initialize",
                        self._initialize_params,
                    )
                except ACPResponseError as exc:
                    raise ACPInitializationError(str(exc)) from exc
                except ACPTransportError as exc:
                    raise ACPInitializationError(str(exc)) from exc
                self._initialize_result = ACPInitializeResult.from_result(
                    initialize_payload
                )
                try:
                    await self._write_message({"method": "initialized", "params": {}})
                except ACPTransportError:
                    raise
                self._initialized = True
            if self._initialize_result is None:
                raise ACPInitializationError("ACP initialize result is missing")
            return self._initialize_result

    async def request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        await self.start()
        return await self._request_after_start(method, params, timeout=timeout)

    async def _request_after_start(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        if self._disconnect_error is not None:
            raise self._disconnect_error
        self._next_id += 1
        request_id = str(self._next_id)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._pending_methods[request_id] = method
        try:
            await self._write_message(
                {
                    "id": request_id,
                    "method": method,
                    "params": dict(params or {}),
                }
            )
        except Exception:
            self._pending.pop(request_id, None)
            self._pending_methods.pop(request_id, None)
            raise

        wait_timeout = timeout if timeout is not None else self._request_timeout
        try:
            if wait_timeout is None:
                return await future
            return await asyncio.wait_for(asyncio.shield(future), timeout=wait_timeout)
        finally:
            self._pending.pop(request_id, None)
            self._pending_methods.pop(request_id, None)

    async def notify(
        self, method: str, params: Optional[dict[str, Any]] = None
    ) -> None:
        await self._ensure_transport_ready()
        await self._write_message({"method": method, "params": dict(params or {})})

    async def call_optional(
        self, method: str, params: Optional[dict[str, Any]] = None
    ) -> Any:
        try:
            return await self.request(method, params)
        except ACPMethodNotFoundError:
            return None

    async def create_session(
        self,
        *,
        cwd: Optional[str] = None,
        title: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ACPSessionDescriptor:
        params: dict[str, Any] = {}
        if cwd:
            params["cwd"] = cwd
        if title:
            params["title"] = title
        if metadata:
            params["metadata"] = dict(metadata)
        result = await self.request("session/create", params)
        return ACPSessionDescriptor.from_result(result)

    async def load_session(self, session_id: str) -> ACPSessionDescriptor:
        result = await self.request("session/load", {"sessionId": session_id})
        return ACPSessionDescriptor.from_result(result)

    async def list_sessions(self) -> list[ACPSessionDescriptor]:
        result = await self.request("session/list", {})
        return coerce_session_list(result)

    async def start_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ACPPromptHandle:
        params: dict[str, Any] = {"sessionId": session_id, "prompt": prompt}
        if model:
            params["model"] = model
        if metadata:
            params["metadata"] = dict(metadata)
        result = await self.request("prompt/start", params)
        prompt_info = ACPPromptDescriptor.from_result(result, session_id=session_id)
        self._ensure_prompt_state(prompt_info.session_id, prompt_info.turn_id)
        return ACPPromptHandle(self, prompt_info.turn_id)

    async def cancel_prompt(self, session_id: str, turn_id: str) -> Any:
        return await self.request(
            "prompt/cancel",
            {"sessionId": session_id, "turnId": turn_id},
        )

    async def wait_for_prompt(
        self, turn_id: str, *, timeout: Optional[float] = None
    ) -> ACPPromptResult:
        state = self._prompts.get(turn_id)
        if state is None:
            raise ACPProtocolError(f"Unknown ACP prompt handle '{turn_id}'")
        if timeout is None:
            return await asyncio.shield(state.future)
        return await asyncio.wait_for(asyncio.shield(state.future), timeout=timeout)

    async def iter_prompt_events(self, turn_id: str) -> AsyncIterator[ACPEvent]:
        state = self._prompts.get(turn_id)
        if state is None:
            raise ACPProtocolError(f"Unknown ACP prompt handle '{turn_id}'")
        while True:
            item = await state.queue.get()
            if item is _QUEUE_SENTINEL:
                break
            yield item  # type: ignore[misc]

    async def iter_notifications(self) -> AsyncIterator[ACPEvent]:
        while True:
            item = await self._notifications.get()
            if item is _QUEUE_SENTINEL:
                break
            yield item  # type: ignore[misc]

    async def close(self) -> None:
        if self._closed or self._closing:
            return
        self._closing = True
        process = self._process
        try:
            if process is not None and process.returncode is None:
                if self._initialized:
                    try:
                        await self.call_optional("shutdown", {})
                    except ACPError:
                        pass
                    try:
                        await self.notify("exit", {})
                    except ACPError:
                        pass
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
        finally:
            self._closed = True
            self._closing = False
            self._process = None
            for task in (self._reader_task, self._stderr_task, self._wait_task):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            await self._finalize_disconnect(self._disconnect_error)

    async def _ensure_transport_ready(self) -> None:
        await self.start()
        if self._disconnect_error is not None:
            raise self._disconnect_error

    async def _write_message(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise ACPTransportError("ACP subprocess stdin is not available")
        data = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                error = await self._build_disconnect_error()
                await self._finalize_disconnect(error)
                raise error from exc

    async def _read_stdout_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        while True:
            line = await process.stdout.readline()
            if not line:
                return
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                error = ACPProtocolError(
                    f"ACP subprocess emitted invalid JSON: {line[:200]!r}"
                )
                await self._finalize_disconnect(error)
                raise error from exc
            if not isinstance(message, dict):
                error = ACPProtocolError("ACP subprocess emitted a non-object message")
                await self._finalize_disconnect(error)
                raise error
            await self._dispatch_message(message)

    async def _read_stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr_tail.append(text)
                self._logger.debug("ACP stderr: %s", text)

    async def _wait_for_process_exit(self) -> None:
        process = self._process
        if process is None:
            return
        await process.wait()
        if not self._closed and not self._closing:
            error = await self._build_disconnect_error()
            await self._finalize_disconnect(error)

    async def _dispatch_message(self, message: dict[str, Any]) -> None:
        request_id = _normalize_optional_text(message.get("id"))
        method = _normalize_optional_text(message.get("method"))
        if (
            request_id is not None
            and method is not None
            and not ("result" in message or "error" in message)
        ):
            await self._handle_server_request(message, request_id=request_id)
            return
        if request_id is not None and ("result" in message or "error" in message):
            future = self._pending.get(request_id)
            if future is None or future.done():
                return
            error_payload = _coerce_mapping(message.get("error"))
            if error_payload:
                method = self._pending_methods.get(request_id)
                error = self._response_error(method, error_payload)
                future.set_exception(error)
                return
            future.set_result(message.get("result"))
            return

        if not method:
            raise ACPProtocolError("ACP message is missing a method name")
        event = normalize_notification(message)
        await self._notifications.put(event)
        if (
            isinstance(event, ACPPermissionRequestEvent)
            and self._permission_handler is not None
        ):
            decision = await self._permission_handler(event)
            if decision is not None and event.method == "permission/requested":
                asyncio.create_task(
                    self._respond_to_permission_notification(event, decision)
                )
        if self._notification_handler is not None:
            await self._notification_handler(event)
        if event.turn_id:
            state = self._prompts.get(event.turn_id)
            if state is None:
                self._orphan_events[event.turn_id].append(event)
            else:
                await self._record_prompt_event(state, event)

    async def _handle_server_request(
        self, message: dict[str, Any], *, request_id: str
    ) -> None:
        method = _normalize_optional_text(message.get("method")) or ""
        event = normalize_notification(message)
        await self._notifications.put(event)
        if event.turn_id:
            state = self._prompts.get(event.turn_id)
            if state is None:
                self._orphan_events[event.turn_id].append(event)
            else:
                await self._record_prompt_event(state, event)
        if self._notification_handler is not None:
            await self._notification_handler(event)

        if method != "session/request_permission" or not isinstance(
            event, ACPPermissionRequestEvent
        ):
            await self._write_message(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Unsupported ACP server request: {method or 'unknown'}",
                    },
                }
            )
            return

        decision: Any = "cancel"
        if self._permission_handler is not None:
            try:
                decision = await self._permission_handler(event)
            except asyncio.CancelledError:
                decision = "cancel"
            except Exception:
                decision = "cancel"
        await self._write_message(
            {
                "id": request_id,
                "result": _permission_outcome_payload(event, decision),
            }
        )

    async def _respond_to_permission_notification(
        self,
        event: ACPPermissionRequestEvent,
        decision: Any,
    ) -> None:
        payload = {
            "requestId": event.request_id,
            "sessionId": event.session_id,
            "turnId": event.turn_id,
            "decision": _decision_label(decision) or "cancel",
            "outcome": _permission_outcome_payload(event, decision)["outcome"],
        }
        for method in _PERMISSION_NOTIFICATION_METHODS:
            try:
                result = await self.call_optional(method, payload)
            except ACPError:
                continue
            if result is not None:
                return

    def _ensure_prompt_state(self, session_id: str, turn_id: str) -> _PromptState:
        state = self._prompts.get(turn_id)
        if state is None:
            state = _PromptState(session_id=session_id, turn_id=turn_id)
            state.future = asyncio.get_running_loop().create_future()
            self._prompts[turn_id] = state
            for event in self._orphan_events.pop(turn_id, []):
                asyncio.create_task(self._record_prompt_event(state, event))
        return state

    async def _record_prompt_event(self, state: _PromptState, event: ACPEvent) -> None:
        if state.closed:
            return
        state.events.append(event)
        if isinstance(event, ACPOutputDeltaEvent):
            state.final_output += event.delta
        elif isinstance(event, ACPMessageEvent) and event.message:
            state.final_output = event.message
        await state.queue.put(event)
        if not isinstance(event, ACPTurnTerminalEvent):
            return
        state.closed = True
        if not state.future.done():
            final_output = event.final_output or state.final_output
            state.future.set_result(
                ACPPromptResult(
                    session_id=state.session_id,
                    turn_id=state.turn_id,
                    status=event.status,
                    final_output=final_output,
                    error_message=event.error_message,
                    events=tuple(state.events),
                )
            )
        await state.queue.put(_QUEUE_SENTINEL)

    def _response_error(
        self, method: Optional[str], payload: dict[str, Any]
    ) -> ACPResponseError:
        message = (
            _normalize_optional_text(payload.get("message")) or "ACP request failed"
        )
        code = payload.get("code")
        code_int = int(code) if isinstance(code, int) else None
        data = _coerce_mapping(payload.get("data")) or None
        if code_int == -32601:
            return ACPMethodNotFoundError(
                method=method,
                code=code_int,
                message=message,
                data=data,
            )
        return ACPResponseError(
            method=method,
            code=code_int,
            message=message,
            data=data,
        )

    async def _build_disconnect_error(self) -> ACPTransportError:
        process = self._process
        returncode: Optional[int] = None
        if process is not None:
            returncode = process.returncode
            if returncode is None:
                try:
                    returncode = await asyncio.wait_for(process.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    returncode = None
        return ACPProcessCrashedError(
            _build_transport_error_message(
                returncode=returncode,
                stderr_tail=self._stderr_tail,
            ),
            returncode=returncode,
            stderr_tail=tuple(self._stderr_tail),
        )

    async def _finalize_disconnect(
        self,
        error: Optional[ACPError],
    ) -> None:
        if self._disconnect_error is None and error is not None:
            self._disconnect_error = error
        terminal_error = self._disconnect_error or error
        if terminal_error is not None:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(terminal_error)
            for state in self._prompts.values():
                if not state.future.done():
                    state.future.set_exception(terminal_error)
                if not state.closed:
                    state.closed = True
                    await state.queue.put(_QUEUE_SENTINEL)
        await self._notifications.put(_QUEUE_SENTINEL)


__all__ = [
    "ACPClient",
    "ACPPromptHandle",
    "ACPPromptResult",
    "NotificationHandler",
    "PermissionHandler",
]
