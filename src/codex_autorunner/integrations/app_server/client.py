import asyncio
import json
import logging
import random
import re
import uuid
from collections import deque
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence, Union, cast

from ...core.circuit_breaker import CircuitBreaker
from ...core.exceptions import (
    CircuitOpenError,
    CodexError,
    PermanentError,
    TransientError,
)
from ...core.logging_utils import log_event, sanitize_log_value
from ...core.retry import retry_transient

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
_OVERSIZE_PREVIEW_BYTES = 4096
_MAX_OVERSIZE_DRAIN_BYTES = 100 * 1024 * 1024

_RESTART_BACKOFF_INITIAL_SECONDS = 0.5
_RESTART_BACKOFF_MAX_SECONDS = 30.0
_RESTART_BACKOFF_JITTER_RATIO = 0.1


class CodexAppServerError(CodexError):
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


class CodexAppServerDisconnected(CodexAppServerError, TransientError):
    """Raised when the app-server disconnects mid-flight."""

    def __init__(self, message: str = "App-server disconnected") -> None:
        super().__init__(
            message, user_message="App-server temporarily unavailable. Reconnecting..."
        )


class CodexAppServerProtocolError(CodexAppServerError, PermanentError):
    """Raised when the app-server returns malformed responses."""

    def __init__(self, message: str) -> None:
        super().__init__(message, user_message="App-server protocol error. Check logs.")


@dataclass
class TurnResult:
    turn_id: str
    status: Optional[str]
    agent_messages: list[str]
    errors: list[str]
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
    future: asyncio.Future["TurnResult"]
    agent_messages: list[str]
    errors: list[str]
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
        self._circuit_breaker = CircuitBreaker("App-Server", logger=self._logger)

        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._start_lock: Optional[asyncio.Lock] = None
        self._write_lock: Optional[asyncio.Lock] = None
        self._data_lock: Optional[asyncio.Lock] = None
        self._pending: Dict[str, asyncio.Future[Any]] = {}
        self._pending_methods: Dict[str, str] = {}
        self._turns: Dict[TurnKey, _TurnState] = {}
        self._pending_turns: Dict[str, _TurnState] = {}
        self._next_id: str = str(uuid.uuid4())
        self._initialized = False
        self._initializing = False
        self._closed = False
        self._disconnected: Optional[asyncio.Event] = None
        self._disconnected_set = True
        self._client_version = _client_version()
        self._include_client_version = True
        self._restart_task: Optional[asyncio.Task] = None
        self._restart_backoff_seconds = _RESTART_BACKOFF_INITIAL_SECONDS
        self._stderr_tail: deque[str] = deque(maxlen=5)

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
        disconnected = self._ensure_disconnect_event()
        if timeout is None:
            await disconnected.wait()
            return
        await asyncio.wait_for(disconnected.wait(), timeout)

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
            raise CodexAppServerProtocolError(
                "thread/resume returned non-object result"
            )
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

    async def thread_archive(self, thread_id: str, **kwargs: Any) -> Any:
        params: Dict[str, Any] = {"threadId": thread_id}
        params.update(kwargs)
        return await self.request("thread/archive", params)

    async def model_list(self, **kwargs: Any) -> Any:
        params = kwargs if kwargs else {}
        return await self.request("model/list", params)

    async def account_read(self, **kwargs: Any) -> Any:
        params = kwargs if kwargs else {}
        return await self.request("account/read", params)

    async def rate_limits_read(self, **kwargs: Any) -> Any:
        params = kwargs if kwargs else {}
        return await self.request("account/rateLimits/read", params)

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

    async def turn_interrupt(
        self, turn_id: str, *, thread_id: Optional[str] = None
    ) -> Any:
        if thread_id is None:
            _key, state = await self._find_turn_state(turn_id, thread_id=None)
            if state is None or not state.thread_id:
                raise CodexAppServerProtocolError(
                    f"Unknown thread id for turn {turn_id}"
                )
            thread_id = state.thread_id
        params = {"turnId": turn_id, "threadId": thread_id}
        return await self.request("turn/interrupt", params)

    async def wait_for_turn(
        self,
        turn_id: str,
        *,
        thread_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> TurnResult:
        key, state = await self._find_turn_state(turn_id, thread_id=thread_id)
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
        async with self._circuit_breaker.call():
            self._ensure_locks()
            start_lock = self._start_lock
            if start_lock is None:
                raise CodexAppServerProtocolError("start lock unavailable")
            async with start_lock:
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
        disconnected = self._ensure_disconnect_event()
        disconnected.clear()
        self._disconnected_set = False
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
        self._ensure_locks()
        data_lock = self._data_lock
        if data_lock is None:
            raise CodexAppServerProtocolError("data lock unavailable")
        request_id = self._next_request_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        async with data_lock:
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
        await self._send_message(
            self._build_message(method, params=params, req_id=request_id)
        )
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
            async with data_lock:
                self._pending.pop(request_id, None)
                self._pending_methods.pop(request_id, None)

    async def _send_message(self, message: Dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise CodexAppServerDisconnected("App-server process is not running")
        self._ensure_locks()
        write_lock = self._write_lock
        if write_lock is None:
            raise CodexAppServerProtocolError("write lock unavailable")
        payload = json.dumps(message, separators=(",", ":"))
        async with write_lock:
            self._process.stdin.write((payload + "\n").encode("utf-8"))
            await self._process.stdin.drain()

    def _build_message(
        self,
        method: Optional[str] = None,
        *,
        params: Optional[Dict[str, Any]] = None,
        req_id: Optional[Union[int, str]] = None,
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

    def _next_request_id(self) -> str:
        self._next_id = str(uuid.uuid4())
        return self._next_id

    def _ensure_locks(self) -> None:
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        if self._data_lock is None:
            self._data_lock = asyncio.Lock()

    def _ensure_disconnect_event(self) -> asyncio.Event:
        if self._disconnected is None:
            self._disconnected = asyncio.Event()
            if self._disconnected_set:
                self._disconnected.set()
        return self._disconnected

    async def _read_loop(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        buffer = bytearray()
        dropping_oversize = False
        drain_limit_reached = False
        oversize_preview = bytearray()
        oversize_bytes_dropped = 0
        try:
            while True:
                chunk = await self._process.stdout.read(_READ_CHUNK_SIZE)
                if not chunk:
                    break
                if dropping_oversize:
                    newline_index = chunk.find(b"\n")
                    if newline_index == -1:
                        if not drain_limit_reached:
                            if len(oversize_preview) < _OVERSIZE_PREVIEW_BYTES:
                                remaining = _OVERSIZE_PREVIEW_BYTES - len(
                                    oversize_preview
                                )
                                oversize_preview.extend(chunk[:remaining])
                            oversize_bytes_dropped += len(chunk)
                            if oversize_bytes_dropped >= _MAX_OVERSIZE_DRAIN_BYTES:
                                await self._emit_oversize_warning(
                                    bytes_dropped=oversize_bytes_dropped,
                                    preview=oversize_preview,
                                    aborted=True,
                                    drain_limit=_MAX_OVERSIZE_DRAIN_BYTES,
                                )
                                drain_limit_reached = True
                        continue
                    before = chunk[: newline_index + 1]
                    after = chunk[newline_index + 1 :]
                    if not drain_limit_reached:
                        if len(oversize_preview) < _OVERSIZE_PREVIEW_BYTES:
                            remaining = _OVERSIZE_PREVIEW_BYTES - len(oversize_preview)
                            oversize_preview.extend(before[:remaining])
                        oversize_bytes_dropped += len(before)
                        await self._emit_oversize_warning(
                            bytes_dropped=oversize_bytes_dropped,
                            preview=oversize_preview,
                        )
                    dropping_oversize = False
                    drain_limit_reached = False
                    oversize_preview = bytearray()
                    oversize_bytes_dropped = 0
                    if not after:
                        continue
                    buffer.extend(after)
                else:
                    buffer.extend(chunk)
                while True:
                    newline_index = buffer.find(b"\n")
                    if newline_index == -1:
                        break
                    line = buffer[:newline_index]
                    del buffer[: newline_index + 1]
                    await self._handle_payload_line(line)
                if not dropping_oversize and len(buffer) > _MAX_MESSAGE_BYTES:
                    oversize_preview = bytearray(buffer[:_OVERSIZE_PREVIEW_BYTES])
                    oversize_bytes_dropped = len(buffer)
                    buffer.clear()
                    dropping_oversize = True
            if dropping_oversize:
                if oversize_bytes_dropped:
                    await self._emit_oversize_warning(
                        bytes_dropped=oversize_bytes_dropped,
                        preview=oversize_preview,
                        truncated=True,
                    )
            elif buffer:
                if len(buffer) > _MAX_MESSAGE_BYTES:
                    await self._emit_oversize_warning(
                        bytes_dropped=len(buffer),
                        preview=buffer[:_OVERSIZE_PREVIEW_BYTES],
                        truncated=True,
                    )
                else:
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
        if not isinstance(message, dict):
            return
        await self._handle_message(message)

    async def _emit_oversize_warning(
        self,
        *,
        bytes_dropped: int,
        preview: bytes,
        truncated: bool = False,
        aborted: bool = False,
        drain_limit: Optional[int] = None,
    ) -> None:
        metadata = _infer_metadata_from_preview(preview)
        log_event(
            self._logger,
            logging.WARNING,
            "app_server.read.oversize_dropped",
            bytes_dropped=bytes_dropped,
            preview_bytes=len(preview),
            preview_excerpt=_preview_excerpt(metadata.get("preview") or ""),
            inferred_method=metadata.get("method"),
            inferred_thread_id=metadata.get("thread_id"),
            inferred_turn_id=metadata.get("turn_id"),
            truncated=truncated,
            aborted=aborted,
            drain_limit=drain_limit,
        )
        if self._notification_handler is None:
            return
        params: Dict[str, Any] = {
            "byteLimit": _MAX_MESSAGE_BYTES,
            "bytesDropped": bytes_dropped,
        }
        inferred_method = metadata.get("method")
        inferred_thread_id = metadata.get("thread_id")
        inferred_turn_id = metadata.get("turn_id")
        if inferred_method:
            params["inferredMethod"] = inferred_method
        if inferred_thread_id:
            params["threadId"] = inferred_thread_id
        if inferred_turn_id:
            params["turnId"] = inferred_turn_id
        if truncated:
            params["truncated"] = True
        if aborted:
            params["aborted"] = True
        if drain_limit is not None:
            params["drainLimit"] = drain_limit
        try:
            await _maybe_await(
                self._notification_handler(
                    {
                        "method": "car/app_server/oversizedMessageDropped",
                        "params": params,
                    }
                )
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.notification_handler.failed",
                method="car/app_server/oversizedMessageDropped",
                handled=False,
                exc=exc,
            )

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
                    sanitized = sanitize_log_value(text)
                    if isinstance(sanitized, str):
                        self._stderr_tail.append(sanitized)
                    else:
                        self._stderr_tail.append(str(sanitized))
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "app_server.stderr",
                        line_len=len(text),
                        tail_size=len(self._stderr_tail),
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
        req_id_raw = message.get("id")

        if not isinstance(req_id_raw, (int, str)):
            return

        req_id = str(req_id_raw) if isinstance(req_id_raw, int) else req_id_raw

        self._ensure_locks()
        data_lock = self._data_lock
        if data_lock is None:
            raise CodexAppServerProtocolError("data lock unavailable")
        async with data_lock:
            future = self._pending.pop(req_id, None)
            method = self._pending_methods.pop(req_id, None)
        if future is None:
            log_event(
                self._logger,
                logging.DEBUG,
                "app_server.response.unmatched",
                request_id=req_id,
                request_id_type=type(req_id).__name__,
                method=method,
            )
            return
        if future.cancelled():
            return
        if "error" in message and message["error"] is not None:
            err = message.get("error") or {}
            error_code = err.get("code")
            if error_code == -32600:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "app_server.response.invalid_request",
                    request_id=req_id,
                    request_id_type=type(req_id).__name__,
                    method=method,
                    error_code=error_code,
                    error_message=err.get("message"),
                )
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.response.error",
                request_id=req_id,
                request_id_type=type(req_id).__name__,
                method=method,
                error_code=error_code,
                error_message=err.get("message"),
            )
            future.set_exception(
                CodexAppServerResponseError(
                    method=method,
                    code=error_code,
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
            request_id_type=type(req_id).__name__,
            method=method,
        )
        future.set_result(message.get("result"))

    async def _handle_server_request(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        req_id = message.get("id")
        if isinstance(method, str) and method in APPROVAL_METHODS:
            params_raw = message.get("params")
            params: Dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}
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
            _key, state = await self._find_turn_state(turn_id, thread_id=thread_id)
            if state is None:
                if thread_id:
                    state = self._ensure_turn_state(turn_id, thread_id)
                else:
                    state = self._ensure_pending_turn_state(turn_id)
            self._apply_item_completed(state, message, params)
            handled = True
        elif method == "turn/completed":
            turn_id = _extract_turn_id(params)
            if not turn_id:
                handled = True
                return
            thread_id = _extract_thread_id_for_turn(params)
            _key, state = await self._find_turn_state(turn_id, thread_id=thread_id)
            if state is None:
                if thread_id:
                    state = self._ensure_turn_state(turn_id, thread_id)
                else:
                    state = self._ensure_pending_turn_state(turn_id)
            self._apply_turn_completed(state, message, params)
            handled = True
        elif method == "error":
            turn_id = _extract_turn_id(params)
            if not turn_id:
                handled = True
                return
            thread_id = _extract_thread_id_for_turn(params)
            _key, state = await self._find_turn_state(turn_id, thread_id=thread_id)
            if state is None:
                if thread_id:
                    state = self._ensure_turn_state(turn_id, thread_id)
                else:
                    state = self._ensure_pending_turn_state(turn_id)
            self._apply_error(state, message, params)
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

    async def _find_turn_state(
        self, turn_id: str, *, thread_id: Optional[str]
    ) -> tuple[Optional[TurnKey], Optional[_TurnState]]:
        self._ensure_locks()
        data_lock = self._data_lock
        if data_lock is None:
            raise CodexAppServerProtocolError("data lock unavailable")
        async with data_lock:
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
        future = cast(asyncio.Future[TurnResult], loop.create_future())
        state = _TurnState(
            turn_id=turn_id,
            thread_id=thread_id,
            future=future,
            agent_messages=[],
            errors=[],
            raw_events=[],
        )
        self._turns[key] = state
        return state

    def _ensure_pending_turn_state(self, turn_id: str) -> _TurnState:
        state = self._pending_turns.get(turn_id)
        if state is not None:
            return state
        loop = asyncio.get_running_loop()
        future = cast(asyncio.Future[TurnResult], loop.create_future())
        state = _TurnState(
            turn_id=turn_id,
            thread_id=None,
            future=future,
            agent_messages=[],
            errors=[],
            raw_events=[],
        )
        self._pending_turns[turn_id] = state
        return state

    def _merge_turn_state(self, target: _TurnState, source: _TurnState) -> None:
        if not target.agent_messages:
            target.agent_messages = list(source.agent_messages)
        else:
            target.agent_messages.extend(source.agent_messages)
        if not target.raw_events:
            target.raw_events = list(source.raw_events)
        else:
            target.raw_events.extend(source.raw_events)
        if not target.errors:
            target.errors = list(source.errors)
        else:
            target.errors.extend(source.errors)
        if target.status is None and source.status is not None:
            target.status = source.status
        if source.future.done() and not target.future.done():
            target.future.set_result(
                TurnResult(
                    turn_id=target.turn_id,
                    status=target.status,
                    agent_messages=list(target.agent_messages),
                    errors=list(target.errors),
                    raw_events=list(target.raw_events),
                )
            )

    def _register_turn_state(self, turn_id: str, thread_id: str) -> _TurnState:
        key = _turn_key(thread_id, turn_id)
        if key is None:
            raise CodexAppServerProtocolError("turn/start missing thread id")
        pending = self._pending_turns.pop(turn_id, None)
        state = self._turns.get(key)
        if pending is not None:
            if state is None:
                pending.thread_id = thread_id
                self._turns[key] = pending
                return pending
            self._merge_turn_state(state, pending)
            return state
        if state is None:
            return self._ensure_turn_state(turn_id, thread_id)
        return state

    def _apply_item_completed(
        self, state: _TurnState, message: Dict[str, Any], params: Any
    ) -> None:
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
            turn_id=state.turn_id,
            item_type=item_type,
        )
        state.raw_events.append(message)

    def _apply_error(
        self, state: _TurnState, message: Dict[str, Any], params: Any
    ) -> None:
        error_message = _extract_error_message(params)
        if error_message:
            state.errors.append(error_message)
        error_payload = params.get("error") if isinstance(params, dict) else None
        error_code = (
            error_payload.get("code") if isinstance(error_payload, dict) else None
        )
        will_retry = params.get("willRetry") if isinstance(params, dict) else None
        log_event(
            self._logger,
            logging.WARNING,
            "app_server.turn_error",
            turn_id=state.turn_id,
            thread_id=state.thread_id,
            message=error_message,
            code=error_code,
            will_retry=will_retry,
        )
        state.raw_events.append(message)

    def _apply_turn_completed(
        self, state: _TurnState, message: Dict[str, Any], params: Any
    ) -> None:
        state.raw_events.append(message)
        status = None
        if isinstance(params, dict):
            status = params.get("status")
        state.status = status
        log_event(
            self._logger,
            logging.INFO,
            "app_server.turn.completed",
            turn_id=state.turn_id,
            status=status,
        )
        if not state.future.done():
            state.future.set_result(
                TurnResult(
                    turn_id=state.turn_id,
                    status=state.status,
                    agent_messages=list(state.agent_messages),
                    errors=list(state.errors),
                    raw_events=list(state.raw_events),
                )
            )

    async def _handle_disconnect(self) -> None:
        self._initialized = False
        self._initializing = False
        disconnected = self._ensure_disconnect_event()
        disconnected.set()
        self._disconnected_set = True
        process = self._process
        returncode = process.returncode if process is not None else None
        pid = process.pid if process is not None else None
        log_event(
            self._logger,
            logging.WARNING,
            "app_server.disconnected",
            auto_restart=self._auto_restart,
            returncode=returncode,
            pid=pid,
            pending_requests=len(self._pending),
            pending_turns=len(self._pending_turns),
            active_turns=len(self._turns),
            initializing=self._initializing,
            initialized=self._initialized,
            closed=self._closed,
            stderr_tail=list(self._stderr_tail),
        )
        if not self._closed:
            self._fail_pending(CodexAppServerDisconnected("App-server disconnected"))
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
        for state in list(self._pending_turns.values()):
            if not state.future.done():
                state.future.set_exception(error)
        self._pending_turns.clear()

    def _schedule_restart(self) -> None:
        if self._restart_task is not None and not self._restart_task.done():
            return
        self._restart_task = asyncio.create_task(self._restart_after_disconnect())

    @retry_transient(max_attempts=10, base_wait=0.5, max_wait=30.0)
    async def _restart_after_disconnect(self) -> None:
        delay = max(self._restart_backoff_seconds, _RESTART_BACKOFF_INITIAL_SECONDS)
        jitter = delay * _RESTART_BACKOFF_JITTER_RATIO
        if jitter:
            delay += random.uniform(0, jitter)
        await asyncio.sleep(delay)
        if self._closed:
            raise CodexAppServerDisconnected("Client closed")
        try:
            await self._ensure_process()
            self._restart_backoff_seconds = _RESTART_BACKOFF_INITIAL_SECONDS
            log_event(
                self._logger,
                logging.INFO,
                "app_server.restarted",
                delay_seconds=round(delay, 2),
            )
        except CodexAppServerDisconnected:
            raise
        except CircuitOpenError:
            await asyncio.sleep(60.0)
            raise
        except Exception as exc:
            next_delay = min(
                max(
                    self._restart_backoff_seconds * 2, _RESTART_BACKOFF_INITIAL_SECONDS
                ),
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
            raise CodexAppServerDisconnected(f"Restart failed: {exc}") from exc

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
        return {"turn_id": params.get("turnId"), "thread_id": params.get("threadId")}
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


def _first_regex_group(text: str, pattern: str) -> Optional[str]:
    try:
        match = re.search(pattern, text)
    except re.error:
        return None
    if not match:
        return None
    value = match.group(1)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _infer_metadata_from_preview(preview: bytes) -> Dict[str, Optional[str]]:
    try:
        text = preview.decode("utf-8", errors="ignore")
    except Exception:
        return {"preview": "", "method": None, "thread_id": None, "turn_id": None}
    method = _first_regex_group(text, r'"method"\s*:\s*"([^"]+)"')
    thread_id = _first_regex_group(text, r'"threadId"\s*:\s*"([^"]+)"')
    if not thread_id:
        thread_id = _first_regex_group(text, r'"thread_id"\s*:\s*"([^"]+)"')
    turn_id = _first_regex_group(text, r'"turnId"\s*:\s*"([^"]+)"')
    if not turn_id:
        turn_id = _first_regex_group(text, r'"turn_id"\s*:\s*"([^"]+)"')
    return {
        "preview": text,
        "method": method,
        "thread_id": thread_id,
        "turn_id": turn_id,
    }


def _preview_excerpt(text: str, limit: int = 256) -> str:
    normalized = " ".join(text.split()).strip()
    if not normalized:
        return ""
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit].rstrip()}..."


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


def _extract_error_message(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    message: Optional[str] = None
    details: Optional[str] = None
    if isinstance(error, dict):
        raw_message = error.get("message")
        if isinstance(raw_message, str):
            message = raw_message.strip() or None
        raw_details = error.get("additionalDetails") or error.get("details")
        if isinstance(raw_details, str):
            details = raw_details.strip() or None
    elif isinstance(error, str):
        message = error.strip() or None
    if message is None:
        fallback = payload.get("message")
        if isinstance(fallback, str):
            message = fallback.strip() or None
    if details and details != message:
        if message:
            return f"{message} ({details})"
        return details
    return message


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


__all__ = [
    "APPROVAL_METHODS",
    "ApprovalDecision",
    "ApprovalHandler",
    "CodexAppServerClient",
    "CodexAppServerDisconnected",
    "CodexAppServerError",
    "CodexAppServerProtocolError",
    "CodexAppServerResponseError",
    "NotificationHandler",
    "TurnHandle",
    "TurnResult",
    "_normalize_sandbox_policy",
]
