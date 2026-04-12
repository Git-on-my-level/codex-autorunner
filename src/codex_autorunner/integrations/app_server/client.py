import asyncio
import json
import logging
import os
import random
import re
import signal
import time
import uuid
import weakref
from collections import deque
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Optional,
    Sequence,
    Union,
    cast,
    no_type_check,
)

from ...core.circuit_breaker import CircuitBreaker
from ...core.exceptions import CircuitOpenError
from ...core.logging_utils import log_event, sanitize_log_value
from ...core.managed_processes.registry import (
    ProcessRecord,
    delete_process_record,
    write_process_record,
)
from ...core.retry import retry_transient
from .errors import (
    CodexAppServerDisconnected,
    CodexAppServerError,
    CodexAppServerProtocolError,
    CodexAppServerResponseError,
)
from .ids import extract_thread_id, extract_thread_id_for_turn, extract_turn_id
from .protocol_helpers import (
    RawApprovalRequestAdapter,
    RawNotificationAdapter,
    _maybe_await,
    normalize_approval_request,
    normalize_notification_envelope,
    normalize_response,
)
from .protocol_helpers import (
    _extract_agent_message_phase as _protocol_extract_agent_message_phase,
)
from .protocol_helpers import (
    _extract_agent_message_text as _protocol_extract_agent_message_text,
)
from .recovery import RecoveryConfig, TurnRecoveryCoordinator
from .transport import AppServerReadBuffer, build_message
from .turn_state import (
    TurnKey,
    TurnResult,
    TurnState,
    TurnStateManager,
    extract_notification_item_id,
)

ApprovalDecision = Union[str, Dict[str, Any]]
ApprovalHandler = Callable[[Dict[str, Any]], Awaitable[ApprovalDecision]]
NotificationHandler = Callable[[Dict[str, Any]], Awaitable[None]]
_TurnState = TurnState

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

_TURN_STALL_TIMEOUT_SECONDS = 60.0
_TURN_STALL_POLL_INTERVAL_SECONDS = 2.0
_TURN_STALL_RECOVERY_MIN_INTERVAL_SECONDS = 10.0
_TURN_STALL_MAX_RECOVERY_ATTEMPTS = 8
_TURN_COMPLETION_GAP_TIMEOUT_SECONDS = 15.0
_TURN_COMPLETION_SETTLE_SECONDS = 0.25
_MAX_TURN_RAW_EVENTS = 200
_INVALID_JSON_PREVIEW_BYTES = 200
_DEFAULT_OUTPUT_POLICY = "final_only"
_OUTPUT_POLICIES = {"final_only", "all_agent_messages"}

_CLIENT_INSTANCES: weakref.WeakSet = weakref.WeakSet()


def _extract_agent_message_text(item: Any) -> Optional[str]:
    return _protocol_extract_agent_message_text(item)


def _extract_agent_message_phase(item: Any) -> Optional[str]:
    return _protocol_extract_agent_message_phase(item)


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


class CodexAppServerClient:
    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        approval_handler: Optional[ApprovalHandler] = None,
        default_approval_decision: str = "cancel",
        auto_restart: Optional[bool] = None,
        request_timeout: Optional[float] = None,
        turn_stall_timeout_seconds: Optional[float] = _TURN_STALL_TIMEOUT_SECONDS,
        turn_stall_poll_interval_seconds: Optional[float] = None,
        turn_stall_recovery_min_interval_seconds: Optional[float] = None,
        turn_stall_max_recovery_attempts: Optional[
            int
        ] = _TURN_STALL_MAX_RECOVERY_ATTEMPTS,
        turn_completion_gap_timeout_seconds: Optional[
            float
        ] = _TURN_COMPLETION_GAP_TIMEOUT_SECONDS,
        max_message_bytes: Optional[int] = None,
        oversize_preview_bytes: Optional[int] = None,
        max_oversize_drain_bytes: Optional[int] = None,
        restart_backoff_initial_seconds: Optional[float] = None,
        restart_backoff_max_seconds: Optional[float] = None,
        restart_backoff_jitter_ratio: Optional[float] = None,
        output_policy: str = _DEFAULT_OUTPUT_POLICY,
        notification_handler: Optional[NotificationHandler] = None,
        logger: Optional[logging.Logger] = None,
        workspace_id: Optional[str] = None,
    ) -> None:
        self._command = [str(arg) for arg in command]
        self._cwd = str(cwd) if cwd is not None else None
        self._env = env
        self._approval_handler = approval_handler
        self._default_approval_decision = default_approval_decision
        disable_restart_env = os.environ.get(
            "CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS"
        )
        if disable_restart_env:
            self._auto_restart = False
        elif auto_restart is None:
            self._auto_restart = True
        else:
            self._auto_restart = auto_restart
        self._request_timeout = request_timeout
        self._notification_handler = notification_handler
        self._logger = logger or logging.getLogger(__name__)
        self._workspace_id = workspace_id
        self._circuit_breaker = CircuitBreaker("App-Server", logger=self._logger)
        self._max_message_bytes = (
            max_message_bytes
            if max_message_bytes is not None and max_message_bytes > 0
            else _MAX_MESSAGE_BYTES
        )
        self._oversize_preview_bytes = (
            oversize_preview_bytes
            if oversize_preview_bytes is not None and oversize_preview_bytes > 0
            else _OVERSIZE_PREVIEW_BYTES
        )
        self._max_oversize_drain_bytes = (
            max_oversize_drain_bytes
            if max_oversize_drain_bytes is not None and max_oversize_drain_bytes > 0
            else _MAX_OVERSIZE_DRAIN_BYTES
        )
        self._restart_backoff_initial_seconds = (
            restart_backoff_initial_seconds
            if restart_backoff_initial_seconds is not None
            and restart_backoff_initial_seconds > 0
            else _RESTART_BACKOFF_INITIAL_SECONDS
        )
        self._restart_backoff_max_seconds = (
            restart_backoff_max_seconds
            if restart_backoff_max_seconds is not None
            and restart_backoff_max_seconds > 0
            else _RESTART_BACKOFF_MAX_SECONDS
        )
        self._restart_backoff_jitter_ratio = (
            restart_backoff_jitter_ratio
            if restart_backoff_jitter_ratio is not None
            and restart_backoff_jitter_ratio >= 0
            else _RESTART_BACKOFF_JITTER_RATIO
        )
        self._output_policy = _normalize_output_policy(output_policy)

        self._process: Optional[asyncio.subprocess.Process] = None
        self._runtime_instance_id: Optional[str] = None
        self._process_registry_key: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._start_lock: Optional[asyncio.Lock] = None
        self._write_lock: Optional[asyncio.Lock] = None
        self._data_lock: Optional[asyncio.Lock] = None
        self._pending: Dict[str, asyncio.Future[Any]] = {}
        self._pending_methods: Dict[str, str] = {}
        self._turn_state_manager = TurnStateManager(
            logger=self._logger,
            output_policy=self._output_policy,
            completion_settle_seconds=_TURN_COMPLETION_SETTLE_SECONDS,
            max_turn_raw_events=_MAX_TURN_RAW_EVENTS,
        )
        self._turns: Dict[TurnKey, _TurnState] = self._turn_state_manager.turns
        self._pending_turns: Dict[str, _TurnState] = (
            self._turn_state_manager.pending_turns
        )
        self._approval_adapter = RawApprovalRequestAdapter(
            approval_handler,
            default_decision=default_approval_decision,
        )
        self._notification_adapter = RawNotificationAdapter(notification_handler)
        self._next_id: str = str(uuid.uuid4())
        self._initialized = False
        self._initializing = False
        self._closed = False
        self._disconnected: Optional[asyncio.Event] = None
        self._disconnected_set = True
        self._client_version = _client_version()
        self._include_client_version = True
        self._restart_task: Optional[asyncio.Task] = None
        self._restart_backoff_seconds = self._restart_backoff_initial_seconds
        self._stderr_tail: deque[str] = deque(maxlen=5)
        _stall_timeout: Optional[float] = turn_stall_timeout_seconds
        if _stall_timeout is not None and _stall_timeout <= 0:
            _stall_timeout = None
        _recovery_min_interval: float = (
            turn_stall_recovery_min_interval_seconds
            if turn_stall_recovery_min_interval_seconds is not None
            else _TURN_STALL_RECOVERY_MIN_INTERVAL_SECONDS
        )
        if _recovery_min_interval is not None and _recovery_min_interval < 0:
            _recovery_min_interval = _TURN_STALL_RECOVERY_MIN_INTERVAL_SECONDS
        _max_recovery: Optional[int]
        if turn_stall_max_recovery_attempts is None:
            _max_recovery = None
        elif turn_stall_max_recovery_attempts <= 0:
            _max_recovery = None
        else:
            _max_recovery = int(turn_stall_max_recovery_attempts)
        _completion_gap_timeout: Optional[float] = turn_completion_gap_timeout_seconds
        if _completion_gap_timeout is not None and _completion_gap_timeout <= 0:
            _completion_gap_timeout = None
        self._recovery_coordinator = TurnRecoveryCoordinator(
            config=RecoveryConfig(
                stall_timeout_seconds=_stall_timeout,
                stall_recovery_min_interval_seconds=_recovery_min_interval,
                stall_max_recovery_attempts=_max_recovery,
                completion_gap_timeout_seconds=_completion_gap_timeout,
            ),
            logger=self._logger,
            turn_state_manager=self._turn_state_manager,
            dispatch_recovered_notification=self._dispatch_recovered_notification,
        )
        self._turn_stall_poll_interval_seconds: float = (
            turn_stall_poll_interval_seconds
            if turn_stall_poll_interval_seconds is not None
            else _TURN_STALL_POLL_INTERVAL_SECONDS
        )
        if (
            self._turn_stall_poll_interval_seconds is not None
            and self._turn_stall_poll_interval_seconds <= 0
        ):
            self._turn_stall_poll_interval_seconds = _TURN_STALL_POLL_INTERVAL_SECONDS
        _CLIENT_INSTANCES.add(self)

    async def start(self) -> None:
        await self._ensure_process()

    @property
    def runtime_instance_id(self) -> Optional[str]:
        return self._runtime_instance_id

    async def close(self) -> None:
        self._closed = True
        if self._restart_task is not None:
            self._restart_task.cancel()
            try:
                await self._restart_task
            except asyncio.CancelledError:
                pass
            self._restart_task = None
        await self._cancel_background_tasks()
        await self._terminate_process()
        self._fail_pending(CodexAppServerDisconnected("Client closed"))
        _CLIENT_INSTANCES.discard(self)

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
        thread_id = extract_thread_id(result)
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
        resumed_id = extract_thread_id(result)
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
        turn_id = extract_turn_id(result)
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
        turn_id = extract_turn_id(result)
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
            immediate_result = state.future.result()
            if key is not None:
                self._turns.pop(key, None)
            return immediate_result
        timeout = timeout if timeout is not None else self._request_timeout
        deadline = self._turn_wait_deadline(timeout)
        while True:
            slice_timeout = self._turn_wait_slice(timeout_deadline=deadline)
            loop_result = await self._wait_for_turn_slice(
                state, slice_timeout=slice_timeout
            )
            if loop_result is not None:
                if key is not None:
                    self._turns.pop(key, None)
                return loop_result
            await self._maybe_reconcile_turn_completion_gap(
                state,
                turn_id=turn_id,
                thread_id=thread_id or state.thread_id,
            )
            await self._recovery_coordinator.maybe_recover_stalled_turn(
                state,
                turn_id=turn_id,
                thread_id=thread_id or state.thread_id,
                deadline=deadline,
                resume_fn=self.thread_resume,
            )

    def _turn_wait_deadline(self, timeout: Optional[float]) -> Optional[float]:
        return time.monotonic() + timeout if timeout is not None else None

    def _turn_wait_slice(self, timeout_deadline: Optional[float]) -> Optional[float]:
        slice_timeout = self._turn_stall_poll_interval_seconds
        if timeout_deadline is None:
            return slice_timeout
        remaining = timeout_deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        if slice_timeout is None or slice_timeout > remaining:
            return remaining
        return slice_timeout

    async def _wait_for_turn_slice(
        self,
        state: _TurnState,
        *,
        slice_timeout: Optional[float],
    ) -> Optional[TurnResult]:
        try:
            if slice_timeout is None:
                return await asyncio.shield(state.future)
            return await asyncio.wait_for(
                asyncio.shield(state.future), timeout=slice_timeout
            )
        except asyncio.TimeoutError:
            return None

    async def _maybe_reconcile_turn_completion_gap(
        self,
        state: _TurnState,
        *,
        turn_id: str,
        thread_id: Optional[str],
    ) -> None:
        await self._recovery_coordinator.maybe_reconcile_completion_gap(
            state,
            turn_id=turn_id,
            thread_id=thread_id,
            resume_fn=self.thread_resume,
        )

    def _dispatch_recovered_notification(self, message: Dict[str, Any]) -> None:
        self._schedule_notification_handler(message, method="turn/completed")

    @property
    def _turn_stall_timeout_seconds(self) -> Optional[float]:
        return self._recovery_coordinator.config.stall_timeout_seconds

    @property
    def _turn_stall_max_recovery_attempts(self) -> Optional[int]:
        return self._recovery_coordinator.config.stall_max_recovery_attempts

    def _schedule_notification_handler(
        self, message: Dict[str, Any], *, method: str, handled: bool = True
    ) -> None:
        if self._notification_handler is None:
            return
        envelope = normalize_notification_envelope(message)
        if envelope is None:
            return

        async def _invoke_notification_handler() -> None:
            try:
                await self._notification_adapter.notify(envelope)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "app_server.notification_handler.failed",
                    method=method,
                    handled=handled,
                    exc=exc,
                )

        task = asyncio.create_task(_invoke_notification_handler())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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
        popen_kwargs: dict[str, Any] = {
            "cwd": self._cwd,
            "env": self._env,
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        self._process = await asyncio.create_subprocess_exec(
            *self._command, **popen_kwargs
        )
        self._runtime_instance_id = uuid.uuid4().hex
        self._register_process_record()
        log_event(
            self._logger,
            logging.INFO,
            "app_server.spawned",
            command=list(self._command),
            cwd=self._cwd,
            runtime_instance_id=self._runtime_instance_id,
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
        self._restart_backoff_seconds = self._restart_backoff_initial_seconds
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
        return cast(
            Dict[str, Any],
            build_message(
                method,
                params=params,
                req_id=req_id,
                result=result,
                error=error,
            ),
        )

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
        read_buffer = AppServerReadBuffer(
            max_message_bytes=self._max_message_bytes,
            oversize_preview_bytes=self._oversize_preview_bytes,
            max_oversize_drain_bytes=self._max_oversize_drain_bytes,
            on_payload_line=self._handle_payload_line,
            on_oversize=self._emit_oversize_warning,
        )
        try:
            while True:
                chunk = await self._process.stdout.read(_READ_CHUNK_SIZE)
                if not chunk:
                    break
                await read_buffer.feed(chunk, initializing=self._initializing)
            await read_buffer.finalize()
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            ConnectionError,
            BrokenPipeError,
        ) as exc:  # top-level read-loop error handler
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
        except json.JSONDecodeError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.read.invalid_json",
                preview=payload[:_INVALID_JSON_PREVIEW_BYTES],
                length=len(payload),
                exc=exc,
            )
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
            "byteLimit": self._max_message_bytes,
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
        except (
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OSError,
            ConnectionError,
        ) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.notification_handler.failed",
                method="car/app_server/oversizedMessageDropped",
                handled=False,
                exc=exc,
            )
            self._logger.debug("Notification handler failed: %s", exc)

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
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            UnicodeDecodeError,
            ConnectionResetError,
            ConnectionError,
            BrokenPipeError,
            json.JSONDecodeError,
        ) as exc:  # top-level read-loop error boundary, subprocess I/O + JSON decoding
            self._logger.debug("Failed to read stderr: %s", exc)
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
        normalized = normalize_response(message)
        if normalized is None:
            return

        req_id = normalized.request_id

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
        if normalized.error is not None:
            err = normalized.error
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
        future.set_result(normalized.result)

    async def _handle_server_request(self, message: Dict[str, Any]) -> None:
        approval = normalize_approval_request(message)
        method = message.get("method")
        req_id = message.get("id")
        if approval is not None:
            method = approval.method
            req_id = approval.request_id
            turn_id = (
                getattr(approval.request, "turn_id", None)
                if approval.request is not None
                else None
            )
            log_event(
                self._logger,
                logging.INFO,
                "app_server.approval.requested",
                request_id=req_id,
                method=method,
                turn_id=turn_id or approval.params.get("turnId"),
            )
            try:
                decision = await self._approval_adapter.decide(approval)
            except (
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                OSError,
                ConnectionError,
            ) as exc:
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
        if req_id is None or not isinstance(method, str):
            return
        await self._send_message(
            self._build_message(
                req_id=req_id,
                error={"code": -32601, "message": f"Unsupported method: {method}"},
            )
        )

    async def _handle_notification(self, message: Dict[str, Any]) -> None:
        envelope = normalize_notification_envelope(message)
        if envelope is None:
            return

        method = envelope.method
        params = envelope.params
        decoded_notification = envelope.notification
        handled = False
        await self._mark_notification_turn_hint(method=method, params=params)
        handler = self._resolve_notification_handler(method)
        if handler is not None:
            handled = await handler(message, params, decoded_notification)
        if self._notification_handler is not None:
            try:
                await self._notification_adapter.notify(envelope)
            except (
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                OSError,
                ConnectionError,
            ) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "app_server.notification_handler.failed",
                    method=method,
                    handled=handled,
                    exc=exc,
                )

    async def _mark_notification_turn_hint(self, *, method: str, params: Any) -> None:
        if not isinstance(params, dict):
            return
        turn_id_hint = extract_turn_id(params) or extract_turn_id(params.get("turn"))
        if not turn_id_hint:
            return
        thread_id_hint = extract_thread_id_for_turn(params)
        _key, state = await self._find_turn_state(
            turn_id_hint, thread_id=thread_id_hint
        )
        if state is not None:
            self._mark_notification_event(state=state, method=method)

    async def _resolve_notification_turn_state(
        self,
        turn_id: Optional[str],
        thread_id: Optional[str],
        *,
        create_pending: bool = True,
    ) -> Optional[_TurnState]:
        return self._turn_state_manager.resolve_notification_turn_state(
            turn_id,
            thread_id,
            create_pending=create_pending,
        )

    def _mark_notification_event(self, *, state: _TurnState, method: str) -> None:
        self._turn_state_manager.mark_notification_event(state=state, method=method)

    async def _handle_notification_agent_message_delta(
        self, message: Dict[str, Any], params: dict[str, Any], decoded: Any = None
    ) -> bool:
        turn_id = getattr(decoded, "turn_id", None) or extract_turn_id(params)
        if not turn_id:
            return True
        thread_id = getattr(decoded, "thread_id", None) or extract_thread_id_for_turn(
            params
        )
        state = await self._resolve_notification_turn_state(
            turn_id, thread_id, create_pending=True
        )
        if state is None:
            return True
        if decoded is not None:
            item_id = getattr(decoded, "item_id", None) or params.get("itemId")
            content = getattr(decoded, "content", None)
            if isinstance(content, str):
                delta: Optional[str] = content
            else:
                delta = params.get("delta") or params.get("text")
        else:
            item_id = params.get("itemId")
            delta = params.get("delta") or params.get("text")
        if isinstance(item_id, str) and isinstance(delta, str):
            state.agent_message_deltas[item_id] = (
                state.agent_message_deltas.get(item_id, "") + delta
            )
        self._mark_notification_event(state=state, method="item/agentMessage/delta")
        self._turn_state_manager.record_raw_event(state, message)
        if state.turn_completed_seen and not state.future.done():
            self._schedule_turn_completion_settle(state)
        return True

    async def _handle_notification_item_started(
        self, message: Dict[str, Any], params: dict[str, Any], decoded: Any = None
    ) -> bool:
        turn_id = (
            getattr(decoded, "turn_id", None)
            or extract_turn_id(params)
            or extract_turn_id(params.get("item"))
        )
        if not turn_id:
            return True
        thread_id = getattr(decoded, "thread_id", None) or extract_thread_id_for_turn(
            params
        )
        state = await self._resolve_notification_turn_state(
            turn_id, thread_id, create_pending=True
        )
        if state is None:
            return True
        self._mark_notification_event(state=state, method="item/started")
        item_id = extract_notification_item_id(params, decoded)
        if item_id is not None:
            state.active_item_ids.add(item_id)
        self._turn_state_manager.record_raw_event(state, message)
        return True

    async def _handle_notification_item_completed(
        self, message: Dict[str, Any], params: dict[str, Any], decoded: Any = None
    ) -> bool:
        turn_id = (
            getattr(decoded, "turn_id", None)
            or extract_turn_id(params)
            or extract_turn_id(params.get("item"))
        )
        if not turn_id:
            return True
        thread_id = getattr(decoded, "thread_id", None) or extract_thread_id_for_turn(
            params
        )
        state = await self._resolve_notification_turn_state(
            turn_id, thread_id, create_pending=True
        )
        if state is None:
            return True
        self._mark_notification_event(state=state, method="item/completed")
        self._apply_item_completed(state, message, params, decoded)
        if state.turn_completed_seen and not state.future.done():
            self._schedule_turn_completion_settle(state)
        return True

    async def _handle_notification_turn_completed(
        self, message: Dict[str, Any], params: dict[str, Any], decoded: Any = None
    ) -> bool:
        turn_id = getattr(decoded, "turn_id", None) or extract_turn_id(params)
        if not turn_id:
            return True
        thread_id = getattr(decoded, "thread_id", None) or extract_thread_id_for_turn(
            params
        )
        state = await self._resolve_notification_turn_state(
            turn_id, thread_id, create_pending=True
        )
        if state is None:
            return True
        self._mark_notification_event(state=state, method="turn/completed")
        self._apply_turn_completed(state, message, params, decoded)
        return True

    async def _handle_notification_error(
        self, message: Dict[str, Any], params: dict[str, Any], decoded: Any = None
    ) -> bool:
        turn_id = getattr(decoded, "turn_id", None) or extract_turn_id(params)
        if not turn_id:
            return True
        thread_id = getattr(decoded, "thread_id", None) or extract_thread_id_for_turn(
            params
        )
        state = await self._resolve_notification_turn_state(
            turn_id, thread_id, create_pending=True
        )
        if state is None:
            return True
        self._mark_notification_event(state=state, method="error")
        self._apply_error(state, message, params, decoded)
        return True

    def _resolve_notification_handler(
        self, method: object
    ) -> Optional[Callable[..., Awaitable[bool]]]:
        handlers: dict[str, Callable[..., Awaitable[bool]]] = {
            "item/agentMessage/delta": self._handle_notification_agent_message_delta,
            "item/started": self._handle_notification_item_started,
            "item/completed": self._handle_notification_item_completed,
            "turn/completed": self._handle_notification_turn_completed,
            "error": self._handle_notification_error,
            "turn/error": self._handle_notification_error,
        }
        if not isinstance(method, str):
            return None
        return handlers.get(method)

    async def _find_turn_state(
        self, turn_id: str, *, thread_id: Optional[str]
    ) -> tuple[Optional[TurnKey], Optional[_TurnState]]:
        self._ensure_locks()
        data_lock = self._data_lock
        if data_lock is None:
            raise CodexAppServerProtocolError("data lock unavailable")
        async with data_lock:
            return self._turn_state_manager.find_turn_state(
                turn_id, thread_id=thread_id
            )

    def _ensure_turn_state(self, turn_id: str, thread_id: str) -> _TurnState:
        try:
            return self._turn_state_manager.ensure_turn_state(turn_id, thread_id)
        except ValueError as exc:
            raise CodexAppServerProtocolError(str(exc)) from exc

    def _ensure_pending_turn_state(self, turn_id: str) -> _TurnState:
        return self._turn_state_manager.ensure_pending_turn_state(turn_id)

    def _merge_turn_state(self, target: _TurnState, source: _TurnState) -> None:
        self._turn_state_manager.merge_turn_state(target, source)

    def _build_turn_result(self, state: _TurnState) -> TurnResult:
        return self._turn_state_manager.build_turn_result(state)

    def _cancel_turn_completion_settle(self, state: _TurnState) -> None:
        self._turn_state_manager.cancel_turn_completion_settle(state)

    def _set_turn_result_if_pending(
        self, state: _TurnState, *, cancel_settle_task: bool = True
    ) -> None:
        self._turn_state_manager.set_turn_result_if_pending(
            state, cancel_settle_task=cancel_settle_task
        )

    def _schedule_turn_completion_settle(self, state: _TurnState) -> None:
        self._turn_state_manager.schedule_turn_completion_settle(state)

    def _register_turn_state(self, turn_id: str, thread_id: str) -> _TurnState:
        try:
            return self._turn_state_manager.register_turn_state(turn_id, thread_id)
        except ValueError as exc:
            raise CodexAppServerProtocolError(str(exc)) from exc

    def _apply_item_completed(
        self,
        state: _TurnState,
        message: Dict[str, Any],
        params: Any,
        decoded: Any = None,
    ) -> None:
        self._turn_state_manager.apply_item_completed(state, message, params, decoded)

    def _apply_error(
        self,
        state: _TurnState,
        message: Dict[str, Any],
        params: Any,
        decoded: Any = None,
    ) -> None:
        self._turn_state_manager.apply_error(state, message, params, decoded)

    def _apply_turn_completed(
        self,
        state: _TurnState,
        message: Dict[str, Any],
        params: Any,
        decoded: Any = None,
    ) -> None:
        self._turn_state_manager.apply_turn_completed(state, message, params, decoded)

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
        disconnect_error = CodexAppServerDisconnected("App-server disconnected")
        preserve_turns = (
            self._auto_restart
            and not self._closed
            and self._turn_stall_timeout_seconds is not None
        )
        self._fail_pending(
            disconnect_error,
            include_turns=not preserve_turns,
            include_pending_turns=not preserve_turns,
        )
        if self._auto_restart and not self._closed:
            self._schedule_restart()

    def _fail_pending(
        self,
        error: Exception,
        *,
        include_requests: bool = True,
        include_turns: bool = True,
        include_pending_turns: bool = True,
    ) -> None:
        if include_requests:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
        if include_turns:
            for state in list(self._turns.values()):
                self._cancel_turn_completion_settle(state)
                if not state.future.done():
                    state.future.set_exception(error)
            self._turns.clear()
        if include_pending_turns:
            for state in list(self._pending_turns.values()):
                self._cancel_turn_completion_settle(state)
                if not state.future.done():
                    state.future.set_exception(error)
            self._pending_turns.clear()

    def _schedule_restart(self) -> None:
        if self._restart_task is not None and not self._restart_task.done():
            return
        self._restart_task = asyncio.create_task(self._restart_after_disconnect())

    @retry_transient(max_attempts=10, base_wait=0.5, max_wait=30.0)
    async def _restart_after_disconnect(self) -> None:
        try:
            delay = max(
                self._restart_backoff_seconds, self._restart_backoff_initial_seconds
            )
            jitter = delay * self._restart_backoff_jitter_ratio
            if jitter:
                delay += random.uniform(0, jitter)
            await asyncio.sleep(delay)
            if self._closed:
                raise CodexAppServerDisconnected("Client closed")
            try:
                await self._ensure_process()
                self._restart_backoff_seconds = self._restart_backoff_initial_seconds
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
            except (
                RuntimeError,
                OSError,
                ConnectionError,
                BrokenPipeError,
                asyncio.TimeoutError,
            ) as exc:
                next_delay = min(
                    max(
                        self._restart_backoff_seconds * 2,
                        self._restart_backoff_initial_seconds,
                    ),
                    self._restart_backoff_max_seconds,
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
        except asyncio.CancelledError:
            # Ensure any partially-started process is cleaned up to avoid
            # \"Task was destroyed\" noise when event loops shut down.
            await self._terminate_process()
            raise
        finally:
            self._restart_task = None

    async def _terminate_process(self) -> None:
        await self._await_cancelled_task(self._reader_task)
        await self._await_cancelled_task(self._stderr_task)
        process = self._process
        if process is None:
            return
        self._unregister_process_record(process)
        if process.returncode is None:
            await self._terminate_running_process(process)
        self._process = None

    async def _await_cancelled_task(self, task: Optional[asyncio.Task[None]]) -> None:
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _cancel_background_tasks(self) -> None:
        tasks = [task for task in self._background_tasks if not task.done()]
        self._background_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _terminate_running_process(
        self, process: asyncio.subprocess.Process
    ) -> None:
        try:
            if os.name != "nt" and hasattr(os, "killpg"):
                try:
                    # Process is spawned as a session/group leader on POSIX.
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    self._logger.debug(
                        "killpg failed for pid %s", process.pid, exc_info=True
                    )
            try:
                os.kill(process.pid, signal.SIGTERM)
            except OSError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    return
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
                return
            except asyncio.TimeoutError:
                pass
            await self._force_kill_process(process)
        except (
            OSError,
            ProcessLookupError,
            asyncio.TimeoutError,
            RuntimeError,
        ):  # intentional: process termination cleanup
            self._logger.debug(
                "Failed to gracefully terminate app-server process",
                exc_info=True,
            )

    async def _force_kill_process(self, process: asyncio.subprocess.Process) -> None:
        if os.name != "nt" and hasattr(os, "killpg"):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                pass
        try:
            os.kill(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                return
        await process.wait()

    def _register_process_record(self) -> None:
        process = self._process
        if process is None:
            return
        if self._cwd is None:
            return
        workspace_root = Path(self._cwd)
        pgid: Optional[int] = None
        if os.name != "nt":
            try:
                pgid = os.getpgid(process.pid)
            except OSError:
                pgid = None
        record = ProcessRecord(
            kind="codex_app_server",
            workspace_id=self._workspace_id,
            pid=process.pid,
            pgid=pgid,
            base_url=None,
            command=list(self._command),
            owner_pid=os.getpid(),
            started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            metadata={"cwd": self._cwd},
        )
        try:
            write_process_record(workspace_root, record)
            self._process_registry_key = record.record_key()
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.registry.write_failed",
                workspace_id=self._workspace_id,
                cwd=self._cwd,
                exc=exc,
            )

    def _unregister_process_record(
        self, process: Optional[asyncio.subprocess.Process] = None
    ) -> None:
        if self._cwd is None:
            return
        workspace_root = Path(self._cwd)
        key = self._process_registry_key
        if key is None and process is not None and process.pid is not None:
            key = str(process.pid)
        if not key:
            return
        try:
            delete_process_record(workspace_root, "codex_app_server", key)
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.registry.delete_failed",
                workspace_id=self._workspace_id,
                cwd=self._cwd,
                exc=exc,
            )
        finally:
            self._process_registry_key = None


def _summarize_params(method: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    summarizer: dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
        "turn/start": _summarize_turn_start_params,
        "turn/interrupt": _summarize_turn_interrupt_params,
        "thread/start": _summarize_thread_start_params,
        "thread/resume": _summarize_thread_resume_params,
        "thread/list": _summarize_thread_list_params,
        "review/start": _summarize_review_start_params,
    }
    return summarizer.get(method, _summarize_generic_params)(params)


def _summarize_turn_start_params(params: Dict[str, Any]) -> Dict[str, Any]:
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


def _summarize_turn_interrupt_params(params: Dict[str, Any]) -> Dict[str, Any]:
    return {"turn_id": params.get("turnId"), "thread_id": params.get("threadId")}


def _summarize_thread_start_params(params: Dict[str, Any]) -> Dict[str, Any]:
    return {"cwd": params.get("cwd")}


def _summarize_thread_resume_params(params: Dict[str, Any]) -> Dict[str, Any]:
    return {"thread_id": params.get("threadId")}


def _summarize_thread_list_params(params: Dict[str, Any]) -> Dict[str, Any]:
    return {}


def _summarize_review_start_params(params: Dict[str, Any]) -> Dict[str, Any]:
    return {"thread_id": params.get("threadId")}


def _summarize_generic_params(params: Dict[str, Any]) -> Dict[str, Any]:
    return {"param_keys": list(params.keys())[:10]}


def _client_version() -> str:
    try:
        return importlib_metadata.version("codex-autorunner")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


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
    except UnicodeDecodeError:
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


def _normalize_output_policy(policy: Optional[str]) -> str:
    candidate = str(policy or "").strip().lower()
    if candidate in _OUTPUT_POLICIES:
        return candidate
    return _DEFAULT_OUTPUT_POLICY


@no_type_check
async def _close_all_clients() -> None:
    """
    Close any CodexAppServerClient instances that may still be alive.

    This is primarily used in tests to avoid pending restart tasks keeping
    subprocess transports alive when the event loop shuts down.
    """
    logger = logging.getLogger(__name__)
    for client in list(_CLIENT_INSTANCES):
        try:
            await client.close()
        except (RuntimeError, OSError, ConnectionError, asyncio.CancelledError) as exc:
            logger.debug("Failed to close client: %s", exc)
            continue


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
    "_close_all_clients",
    "_normalize_sandbox_policy",
]
