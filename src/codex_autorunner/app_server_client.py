import asyncio
import json
import logging
import random
import re
from importlib import metadata as importlib_metadata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence, Union

from .logging_utils import log_event

ApprovalDecision = Union[str, Dict[str, Any]]
ApprovalHandler = Callable[[Dict[str, Any]], Awaitable[ApprovalDecision]]
NotificationHandler = Callable[[Dict[str, Any]], Awaitable[None]]
TurnKey = tuple[str, str]

APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}
_READ_CHUNK_SIZE = 64 * 1024
_MAX_MESSAGE_BYTES = 50 * 1024 * 1024

_RESTART_BACKOFF_INITIAL_SECONDS = 0.5
_RESTART_BACKOFF_MAX_SECONDS = 30.0
_RESTART_BACKOFF_JITTER_RATIO = 0.1


class CodexAppServerError(Exception):
    """Base error for app-server client failures."""


class CodexAppServerResponseError(CodexAppServerError):
    """Raised when the app-server responds with an error payload."""

    def __init__(
        self,
        *,
        method: Optional[str],
        code: Optional[int],
        message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.code = code
        self.data = data


class CodexAppServerDisconnected(CodexAppServerError):
    """Raised when the app-server disconnects mid-flight."""


class CodexAppServerProtocolError(CodexAppServerError):
    """Raised when the app-server returns malformed responses."""


@dataclass
class TurnResult:
    turn_id: str
    status: Optional[str]
    agent_messages: list[str]
    raw_events: list[Dict[str, Any]]


class TurnHandle:
    def __init__(
        self, client: "CodexAppServerClient", turn_id: str, thread_id: str
    ) -> None:
        self._client = client
        self.turn_id = turn_id
        self.thread_id = thread_id

    async def wait(self, *, timeout: Optional[float] = None) -> TurnResult:
        return await self._client.wait_for_turn(
            self.turn_id, thread_id=self.thread_id, timeout=timeout
        )


@dataclass
class _TurnState:
    turn_id: str
    thread_id: Optional[str]
    future: asyncio.Future
    agent_messages: list[str]
    raw_events: list[Dict[str, Any]]
    status: Optional[str] = None


class CodexAppServerClient:
    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        approval_handler: Optional[ApprovalHandler] = None,
        default_approval_decision: str = "cancel",
        auto_restart: bool = True,
        request_timeout: Optional[float] = None,
        notification_handler: Optional[NotificationHandler] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._command = [str(arg) for arg in command]
        self._cwd = str(cwd) if cwd is not None else None
        self._env = env
        self._approval_handler = approval_handler
        self._default_approval_decision = default_approval_decision
        self._auto_restart = auto_restart
        self._request_timeout = request_timeout
        self._notification_handler = notification_handler
        self._logger = logger or logging.getLogger(__name__)

        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: Dict[int, asyncio.Future] = {}
        self._pending_methods: Dict[int, str] = {}
        self._turns: Dict[TurnKey, _TurnState] = {}
        self._next_id = 1
        self._initialized = False
        self._initializing = False
        self._closed = False
        self._disconnected = asyncio.Event()
        self._disconnected.set()
        self._client_version = _client_version()
        self._include_client_version = True
        self._restart_task: Optional[asyncio.Task] = None
        self._restart_backoff_seconds = _RESTART_BACKOFF_INITIAL_SECONDS

    async def start(self) -> None:
        await self._ensure_process()

    async def close(self) -> None:
        self._closed = True
        if self._restart_task is not None:
            self._restart_task.cancel()
            try:
                await self._restart_task
            except asyncio.CancelledError:
                pass
            self._restart_task = None
        await self._terminate_process()
        self._fail_pending(CodexAppServerDisconnected("Client closed"))

    async def wait_for_disconnect(self, *, timeout: Optional[float] = None) -> None:
        if timeout is None:
            await self._disconnected.wait()
            return
        await asyncio.wait_for(self._disconnected.wait(), timeout)

    async def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        await self._ensure_process()
        return await self._request_raw(method, params=params, timeout=timeout)

    async def notify(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        await self._ensure_process()
        log_event(
            self._logger,
            logging.INFO,
            "app_server.notify",
            method=method,
            **_summarize_params(method, params),
        )
        await self._send_message(self._build_message(method, params=params))

    async def thread_start(self, cwd: str, **kwargs: Any) -> Dict[str, Any]:
        params = {"cwd": cwd}
        params.update(kwargs)
        result = await self.request("thread/start", params)
        if not isinstance(result, dict):
            raise CodexAppServerProtocolError("thread/start returned non-object result")
        thread_id = _extract_thread_id(result)
        if thread_id and "id" not in result:
            result = dict(result)
            result["id"] = thread_id
        return result

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> Dict[str, Any]:
        params = {"threadId": thread_id}
        params.update(kwargs)
        result = await self.request("thread/resume", params)
        if not isinstance(result, dict):
            raise CodexAppServerProtocolError("thread/resume returned non-object result")
        resumed_id = _extract_thread_id(result)
        if resumed_id and "id" not in result:
            result = dict(result)
            result["id"] = resumed_id
        return result

    async def thread_list(self, **kwargs: Any) -> Any:
        params = kwargs if kwargs else {}
        result = await self.request("thread/list", params)
        if isinstance(result, dict) and "threads" not in result:
            for key in ("data", "items", "results"):
                value = result.get(key)
                if isinstance(value, list):
                    result = dict(result)
                    result["threads"] = value
                    break
        return result

    async def turn_start(
        self,
        thread_id: str,
        text: str,
        *,
        input_items: Optional[list[Dict[str, Any]]] = None,
        approval_policy: Optional[str] = None,
        sandbox_policy: Optional[str] = None,
        **kwargs: Any,
    ) -> TurnHandle:
        params: Dict[str, Any] = {"threadId": thread_id}
        if input_items is None:
            params["input"] = [{"type": "text", "text": text}]
        else:
            params["input"] = input_items
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if sandbox_policy:
            params["sandboxPolicy"] = _normalize_sandbox_policy(sandbox_policy)
        params.update(kwargs)
        result = await self.request("turn/start", params)
        if not isinstance(result, dict):
            raise CodexAppServerProtocolError("turn/start returned non-object result")
        turn_id = _extract_turn_id(result)
        if not turn_id:
            raise CodexAppServerProtocolError("turn/start response missing turn id")
        self._register_turn_state(turn_id, thread_id)
        return TurnHandle(self, turn_id, thread_id)

    async def review_start(
        self,
        thread_id: str,
        *,
        target: Dict[str, Any],
        delivery: str = "inline",
        approval_policy: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        **kwargs: Any,
    ) -> TurnHandle:
        params: Dict[str, Any] = {
            "threadId": thread_id,
            "target": target,
            "delivery": delivery,
        }
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if sandbox_policy:
            params["sandboxPolicy"] = _normalize_sandbox_policy(sandbox_policy)
        params.update(kwargs)
        result = await self.request("review/start", params)
        if not isinstance(result, dict):
            raise CodexAppServerProtocolError("review/start returned non-object result")
        turn_id = _extract_turn_id(result)
        if not turn_id:
            raise CodexAppServerProtocolError("review/start response missing turn id")
        self._register_turn_state(turn_id, thread_id)
        return TurnHandle(self, turn_id, thread_id)

    async def turn_interrupt(self, turn_id: str) -> Any:
        params = {"turnId": turn_id}
        return await self.request("turn/interrupt", params)

    async def wait_for_turn(
        self,
        turn_id: str,
        *,
        thread_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> TurnResult:
        key, state = self._find_turn_state(turn_id, thread_id=thread_id)
        if state is None:
            raise CodexAppServerProtocolError(
                f"Unknown turn id {turn_id} (thread {thread_id})"
            )
        if state.future.done():
            result = state.future.result()
            if key is not None:
                self._turns.pop(key, None)
            return result
        timeout = timeout if timeout is not None else self._request_timeout
        if timeout is None:
            result = await state.future
            if key is not None:
                self._turns.pop(key, None)
            return result
        result = await asyncio.wait_for(state.future, timeout)
        if key is not None:
            self._turns.pop(key, None)
        return result

    async def _ensure_process(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise CodexAppServerDisconnected("Client closed")
            if (
                self._process is not None
                and self._process.returncode is None
                and self._initialized
            ):
                return
            await self._spawn_process()
            await self._initialize_handshake()

    async def _spawn_process(self) -> None:
        await self._terminate_process()
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=self._cwd,
            env=self._env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log_event(
            self._logger,
            logging.INFO,
            "app_server.spawned",
            command=list(self._command),
            cwd=self._cwd,
        )
        self._disconnected.clear()
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._initialized = False

    async def _initialize_handshake(self) -> None:
        client_info: Dict[str, Any] = {"name": "codex-autorunner"}
        if self._include_client_version:
            client_info["version"] = self._client_version
        params = {"clientInfo": client_info}
        self._initializing = True
        try:
            await self._request_raw("initialize", params=params)
        except CodexAppServerResponseError as exc:
            if self._include_client_version:
                self._include_client_version = False
                log_event(
                    self._logger,
                    logging.WARNING,
                    "app_server.initialize.retry",
                    reason="response_error",
                    error_code=exc.code,
                )
            raise
        except CodexAppServerDisconnected:
            if self._include_client_version:
                self._include_client_version = False
                log_event(
                    self._logger,
                    logging.WARNING,
                    "app_server.initialize.retry",
                    reason="disconnect",
                )
            raise
        finally:
            self._initializing = False
        await self._send_message(self._build_message("initialized", params=None))
        self._initialized = True
        self._restart_backoff_seconds = _RESTART_BACKOFF_INITIAL_SECONDS
        log_event(self._logger, logging.INFO, "app_server.initialized")

    async def _request_raw(
        self,
        method: str,
        params: Optional[Dict[str, Any]],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        request_id = self._next_request_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future
        self._pending_methods[request_id] = method
        log_event(
            self._logger,
            logging.INFO,
            "app_server.request",
            request_id=request_id,
            method=method,
            **_summarize_params(method, params),
        )
        await self._send_message(self._build_message(method, params=params, req_id=request_id))
        timeout = timeout if timeout is not None else self._request_timeout
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            if not future.done():
                future.cancel()
            raise
        finally:
            self._pending.pop(request_id, None)
            self._pending_methods.pop(request_id, None)

    async def _send_message(self, message: Dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise CodexAppServerDisconnected("App-server process is not running")
        payload = json.dumps(message, separators=(",", ":"))
        async with self._write_lock:
            self._process.stdin.write((payload + "\n").encode("utf-8"))
            await self._process.stdin.drain()

    def _build_message(
        self,
        method: Optional[str] = None,
        *,
        params: Optional[Dict[str, Any]] = None,
        req_id: Optional[int] = None,
        result: Optional[Any] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        message: Dict[str, Any] = {}
        if req_id is not None:
            message["id"] = req_id
        if method is not None:
            message["method"] = method
        if params is not None:
            message["params"] = params
        if result is not None:
            message["result"] = result
        if error is not None:
            message["error"] = error
        return message

    def _next_request_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    async def _read_loop(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        buffer = bytearray()
        try:
            while True:
                chunk = await self._process.stdout.read(_READ_CHUNK_SIZE)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    newline_index = buffer.find(b"\n")
                    if newline_index == -1:
                        break
                    line = buffer[:newline_index]
                    del buffer[: newline_index + 1]
                    await self._handle_payload_line(line)
                if len(buffer) > _MAX_MESSAGE_BYTES:
                    raise ValueError(
                        f"App-server message exceeded {_MAX_MESSAGE_BYTES} bytes without newline"
                    )
            if buffer:
                if len(buffer) > _MAX_MESSAGE_BYTES:
                    raise ValueError(
                        f"App-server message exceeded {_MAX_MESSAGE_BYTES} bytes without newline"
                    )
                await self._handle_payload_line(buffer)
        except Exception as exc:
            log_event(self._logger, logging.WARNING, "app_server.read.failed", exc=exc)
        finally:
            await self._handle_disconnect()

    async def _handle_payload_line(self, line: bytes) -> None:
        if not line:
            return
        payload = line.decode("utf-8", errors="ignore").strip()
        if not payload:
            return
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            return
        await self._handle_message(message)

    async def _drain_stderr(self) -> None:
        if not self._process or not self._process.stderr:
            return
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="ignore").strip()
                if text:
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "app_server.stderr",
                        line_len=len(text),
                    )
        except Exception:
            return

    async def _handle_message(self, message: Dict[str, Any]) -> None:
        if "id" in message and "method" not in message:
            await self._handle_response(message)
            return
        if "id" in message and "method" in message:
            await self._handle_server_request(message)
            return
        if "method" in message:
            await self._handle_notification(message)

    async def _handle_response(self, message: Dict[str, Any]) -> None:
        req_id = message.get("id")
        future = self._pending.pop(req_id, None)
        method = self._pending_methods.pop(req_id, None)
        if future is None:
            return
        if future.cancelled():
            return
        if "error" in message and message["error"] is not None:
            err = message.get("error") or {}
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.response.error",
                request_id=req_id,
                method=method,
                error_code=err.get("code"),
                error_message=err.get("message"),
            )
            future.set_exception(
                CodexAppServerResponseError(
                    method=method,
                    code=err.get("code"),
                    message=err.get("message") or "app-server error",
                    data=err.get("data"),
                )
            )
            return
        log_event(
            self._logger,
            logging.INFO,
            "app_server.response",
            request_id=req_id,
            method=method,
        )
        future.set_result(message.get("result"))

    async def _handle_server_request(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        req_id = message.get("id")
        if method in APPROVAL_METHODS:
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            log_event(
                self._logger,
                logging.INFO,
                "app_server.approval.requested",
                request_id=req_id,
                method=method,
                turn_id=params.get("turnId"),
            )
            decision: ApprovalDecision = self._default_approval_decision
            if self._approval_handler is not None:
                try:
                    decision = await _maybe_await(self._approval_handler(message))
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "app_server.approval.failed",
                        request_id=req_id,
                        method=method,
                        exc=exc,
                    )
                    await self._send_message(
                        self._build_message(
                            req_id=req_id,
                            error={
                                "code": -32001,
                                "message": "approval handler failed",
                            },
                        )
                    )
                    return
            result = decision if isinstance(decision, dict) else {"decision": decision}
            log_event(
                self._logger,
                logging.INFO,
                "app_server.approval.responded",
                request_id=req_id,
                method=method,
                decision=result.get("decision") if isinstance(result, dict) else None,
            )
            await self._send_message(self._build_message(req_id=req_id, result=result))
            return
        await self._send_message(
            self._build_message(
                req_id=req_id,
                error={"code": -32601, "message": f"Unsupported method: {method}"},
            )
        )

    async def _handle_notification(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        handled = False
        if method == "item/completed":
            turn_id = _extract_turn_id(params) or _extract_turn_id(
                params.get("item") if isinstance(params, dict) else None
            )
            if not turn_id:
                handled = True
                return
            thread_id = _extract_thread_id_for_turn(params)
            key, state = self._find_turn_state(turn_id, thread_id=thread_id)
            if state is None:
                if thread_id:
                    state = self._ensure_turn_state(turn_id, thread_id)
                else:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "app_server.turn.unknown",
                        method=method,
                        turn_id=turn_id,
                    )
                    handled = True
                    return
            item = params.get("item") if isinstance(params, dict) else None
            text = None
            def append_message(candidate: Optional[str]) -> None:
                if not candidate:
                    return
                if state.agent_messages and state.agent_messages[-1] == candidate:
                    return
                state.agent_messages.append(candidate)
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = item.get("text")
                if isinstance(text, str):
                    append_message(text)
            review_text = _extract_review_text(item)
            if review_text and review_text != text:
                append_message(review_text)
            item_type = item.get("type") if isinstance(item, dict) else None
            log_event(
                self._logger,
                logging.INFO,
                "app_server.item.completed",
                turn_id=turn_id,
                item_type=item_type,
            )
            state.raw_events.append(message)
            handled = True
        elif method == "turn/completed":
            turn_id = _extract_turn_id(params)
            if not turn_id:
                handled = True
                return
            thread_id = _extract_thread_id_for_turn(params)
            key, state = self._find_turn_state(turn_id, thread_id=thread_id)
            if state is None:
                if thread_id:
                    state = self._ensure_turn_state(turn_id, thread_id)
                else:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "app_server.turn.unknown",
                        method=method,
                        turn_id=turn_id,
                    )
                    handled = True
                    return
            state.raw_events.append(message)
            status = None
            if isinstance(params, dict):
                status = params.get("status")
            state.status = status
            log_event(
                self._logger,
                logging.INFO,
                "app_server.turn.completed",
                turn_id=turn_id,
                status=status,
            )
            if not state.future.done():
                state.future.set_result(
                    TurnResult(
                        turn_id=turn_id,
                        status=state.status,
                        agent_messages=list(state.agent_messages),
                        raw_events=list(state.raw_events),
                    )
                )
            handled = True
        if self._notification_handler is not None:
            try:
                await _maybe_await(self._notification_handler(message))
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "app_server.notification_handler.failed",
                    method=method,
                    handled=handled,
                    exc=exc,
                )

    def _find_turn_state(
        self, turn_id: str, *, thread_id: Optional[str]
    ) -> tuple[Optional[TurnKey], Optional[_TurnState]]:
        key = _turn_key(thread_id, turn_id)
        if key is not None:
            state = self._turns.get(key)
            if state is not None:
                return key, state
        matches = [
            (candidate_key, state)
            for candidate_key, state in self._turns.items()
            if candidate_key[1] == turn_id
        ]
        if len(matches) == 1:
            candidate_key, state = matches[0]
            if key is not None and candidate_key != key:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "app_server.turn.thread_mismatch",
                    turn_id=turn_id,
                    requested_thread_id=thread_id,
                    actual_thread_id=candidate_key[0],
                )
            return candidate_key, state
        if len(matches) > 1:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.turn.ambiguous",
                turn_id=turn_id,
                matches=len(matches),
            )
        return None, None

    def _ensure_turn_state(self, turn_id: str, thread_id: str) -> _TurnState:
        key = _turn_key(thread_id, turn_id)
        if key is None:
            raise CodexAppServerProtocolError("turn state missing thread id")
        state = self._turns.get(key)
        if state is not None:
            return state
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        state = _TurnState(
            turn_id=turn_id,
            thread_id=thread_id,
            future=future,
            agent_messages=[],
            raw_events=[],
        )
        self._turns[key] = state
        return state

    def _register_turn_state(self, turn_id: str, thread_id: str) -> _TurnState:
        key = _turn_key(thread_id, turn_id)
        if key is None:
            raise CodexAppServerProtocolError("turn/start missing thread id")
        state = self._turns.get(key)
        if state is None:
            return self._ensure_turn_state(turn_id, thread_id)
        if not state.future.done():
            log_event(
                self._logger,
                logging.ERROR,
                "app_server.turn_id.collision",
                turn_id=turn_id,
                thread_id=thread_id,
            )
            raise CodexAppServerProtocolError(
                f"turn/start returned duplicate turn id {turn_id}"
            )
        return state

    async def _handle_disconnect(self) -> None:
        self._initialized = False
        self._initializing = False
        self._disconnected.set()
        log_event(
            self._logger,
            logging.WARNING,
            "app_server.disconnected",
            auto_restart=self._auto_restart,
        )
        if not self._closed:
            self._fail_pending(
                CodexAppServerDisconnected("App-server disconnected")
            )
        if self._auto_restart and not self._closed:
            self._schedule_restart()

    def _fail_pending(self, error: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        for state in list(self._turns.values()):
            if not state.future.done():
                state.future.set_exception(error)
        self._turns.clear()

    def _schedule_restart(self) -> None:
        if self._restart_task is not None and not self._restart_task.done():
            return
        self._restart_task = asyncio.create_task(self._restart_after_disconnect())

    async def _restart_after_disconnect(self) -> None:
        delay = max(self._restart_backoff_seconds, _RESTART_BACKOFF_INITIAL_SECONDS)
        jitter = delay * _RESTART_BACKOFF_JITTER_RATIO
        if jitter:
            delay += random.uniform(0, jitter)
        await asyncio.sleep(delay)
        if self._closed:
            return
        try:
            await self._ensure_process()
            self._restart_backoff_seconds = _RESTART_BACKOFF_INITIAL_SECONDS
            log_event(
                self._logger,
                logging.INFO,
                "app_server.restarted",
                delay_seconds=round(delay, 2),
            )
        except Exception as exc:
            next_delay = min(
                max(self._restart_backoff_seconds * 2, _RESTART_BACKOFF_INITIAL_SECONDS),
                _RESTART_BACKOFF_MAX_SECONDS,
            )
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.restart.failed",
                delay_seconds=round(delay, 2),
                next_delay_seconds=round(next_delay, 2),
                exc=exc,
            )
            self._restart_backoff_seconds = next_delay
            if not self._closed:
                self._schedule_restart()

    async def _terminate_process(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        if self._process is None:
            return
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=1)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        self._process = None


def _summarize_params(method: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    if method == "turn/start":
        input_items = params.get("input")
        input_chars = 0
        if isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        input_chars += len(text)
        summary: Dict[str, Any] = {
            "thread_id": params.get("threadId"),
            "input_chars": input_chars,
        }
        if "approvalPolicy" in params:
            summary["approval_policy"] = params.get("approvalPolicy")
        if "sandboxPolicy" in params:
            summary["sandbox_policy"] = params.get("sandboxPolicy")
        return summary
    if method == "turn/interrupt":
        return {"turn_id": params.get("turnId")}
    if method == "thread/start":
        return {"cwd": params.get("cwd")}
    if method == "thread/resume":
        return {"thread_id": params.get("threadId")}
    if method == "thread/list":
        return {}
    if method == "review/start":
        return {"thread_id": params.get("threadId")}
    return {"param_keys": list(params.keys())[:10]}


def _client_version() -> str:
    try:
        return importlib_metadata.version("codex-autorunner")
    except Exception:
        return "unknown"


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


def _extract_turn_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("turnId", "turn_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    turn = payload.get("turn")
    if isinstance(turn, dict):
        for key in ("id", "turnId", "turn_id"):
            value = turn.get(key)
            if isinstance(value, str):
                return value
    return None


def _turn_key(thread_id: Optional[str], turn_id: Optional[str]) -> Optional[TurnKey]:
    if not thread_id or not turn_id:
        return None
    return (thread_id, turn_id)


def _extract_thread_id_for_turn(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for candidate in (payload, payload.get("turn"), payload.get("item")):
        thread_id = _extract_thread_id_from_container(candidate)
        if thread_id:
            return thread_id
    return None


def _extract_thread_id_from_container(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("threadId", "thread_id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    thread = payload.get("thread")
    if isinstance(thread, dict):
        for key in ("id", "threadId", "thread_id"):
            value = thread.get(key)
            if isinstance(value, str):
                return value
    return None


def _extract_review_text(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    exited = item.get("exitedReviewMode")
    if isinstance(exited, dict):
        review = exited.get("review")
        if isinstance(review, str) and review.strip():
            return review
    if item.get("type") == "review":
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text
    review = item.get("review")
    if isinstance(review, str) and review.strip():
        return review
    return None


def _extract_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("threadId", "thread_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    thread = payload.get("thread")
    if isinstance(thread, dict):
        for key in ("id", "threadId", "thread_id"):
            value = thread.get(key)
            if isinstance(value, str):
                return value
    return None


_SANDBOX_POLICY_CANONICAL = {
    "dangerfullaccess": "dangerFullAccess",
    "readonly": "readOnly",
    "workspacewrite": "workspaceWrite",
    "externalsandbox": "externalSandbox",
}


def _normalize_sandbox_policy(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        type_value = value.get("type")
        if isinstance(type_value, str):
            canonical = _normalize_sandbox_policy_type(type_value)
            if canonical != type_value:
                updated = dict(value)
                updated["type"] = canonical
                return updated
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        canonical = _normalize_sandbox_policy_type(raw)
        return {"type": canonical}
    return value


def _normalize_sandbox_policy_type(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "", raw.strip())
    if not cleaned:
        return raw.strip()
    canonical = _SANDBOX_POLICY_CANONICAL.get(cleaned.lower())
    return canonical or raw.strip()
