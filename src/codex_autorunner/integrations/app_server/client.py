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
from dataclasses import dataclass, field
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
from ...core.exceptions import (
    AppServerError,
    CircuitOpenError,
    PermanentError,
    TransientError,
)
from ...core.logging_utils import log_event, sanitize_log_value
from ...core.managed_processes.registry import (
    ProcessRecord,
    delete_process_record,
    write_process_record,
)
from ...core.retry import retry_transient
from .event_decoder import decode_notification
from .ids import extract_thread_id, extract_thread_id_for_turn, extract_turn_id
from .protocol_helpers import (
    _extract_agent_message_phase,
    _extract_agent_message_text,
    extract_resume_snapshot,
    normalize_notification,
    normalize_response,
    normalize_server_request,
)

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

# Per-turn stall detection defaults.
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

# Track live clients so tests/cleanup can cancel any background restart tasks.
_CLIENT_INSTANCES: weakref.WeakSet = weakref.WeakSet()


class CodexAppServerError(AppServerError):
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


_MISSING_THREAD_MARKERS = (
    "thread not found",
    "no rollout found for thread id",
)


def is_missing_thread_error(exc: Exception) -> bool:
    if not isinstance(exc, CodexAppServerResponseError):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _MISSING_THREAD_MARKERS)


@dataclass
class TurnResult:
    turn_id: str
    status: Optional[str]
    final_message: str
    agent_messages: list[str]
    errors: list[str]
    raw_events: list[Dict[str, Any]]
    commentary_messages: list[str] = field(default_factory=list)


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
    agent_messages: list[str] = field(default_factory=list)
    commentary_messages: list[str] = field(default_factory=list)
    final_answer_messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw_events: list[Dict[str, Any]] = field(default_factory=list)
    status: Optional[str] = None
    last_event_at: float = field(default_factory=time.monotonic)
    last_method: Optional[str] = None
    recovery_attempts: int = 0
    last_recovery_at: float = 0.0
    agent_message_deltas: Dict[str, str] = field(default_factory=dict)
    turn_completed_seen: bool = False
    completion_settle_task: Optional[asyncio.Task[None]] = None
    item_completed_count: int = 0
    completion_gap_started_at: Optional[float] = None
    last_stream_delta_at: float = 0.0
    completion_gap_recovery_attempts: int = 0
    last_completion_gap_recovery_at: float = 0.0


@dataclass
class _ReadLoopState:
    dropping_oversize: bool = False
    drain_limit_reached: bool = False
    oversize_preview: bytearray = field(default_factory=bytearray)
    oversize_bytes_dropped: int = 0


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
        self._restart_backoff_seconds = self._restart_backoff_initial_seconds
        self._stderr_tail: deque[str] = deque(maxlen=5)
        self._turn_stall_timeout_seconds: Optional[float] = turn_stall_timeout_seconds
        if (
            self._turn_stall_timeout_seconds is not None
            and self._turn_stall_timeout_seconds <= 0
        ):
            self._turn_stall_timeout_seconds = None
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
        self._turn_stall_recovery_min_interval_seconds: float = (
            turn_stall_recovery_min_interval_seconds
            if turn_stall_recovery_min_interval_seconds is not None
            else _TURN_STALL_RECOVERY_MIN_INTERVAL_SECONDS
        )
        if (
            self._turn_stall_recovery_min_interval_seconds is not None
            and self._turn_stall_recovery_min_interval_seconds < 0
        ):
            self._turn_stall_recovery_min_interval_seconds = (
                _TURN_STALL_RECOVERY_MIN_INTERVAL_SECONDS
            )
        self._turn_stall_max_recovery_attempts: Optional[int]
        if turn_stall_max_recovery_attempts is None:
            self._turn_stall_max_recovery_attempts = None
        elif turn_stall_max_recovery_attempts <= 0:
            self._turn_stall_max_recovery_attempts = None
        else:
            self._turn_stall_max_recovery_attempts = int(
                turn_stall_max_recovery_attempts
            )
        self._turn_completion_gap_timeout_seconds: Optional[float] = (
            turn_completion_gap_timeout_seconds
        )
        if (
            self._turn_completion_gap_timeout_seconds is not None
            and self._turn_completion_gap_timeout_seconds <= 0
        ):
            self._turn_completion_gap_timeout_seconds = None
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
            await self._maybe_recover_stalled_turn(
                state,
                turn_id=turn_id,
                thread_id=thread_id or state.thread_id,
                deadline=deadline,
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

    async def _maybe_recover_stalled_turn(
        self,
        state: _TurnState,
        *,
        turn_id: str,
        thread_id: Optional[str],
        deadline: Optional[float],
    ) -> None:
        if state.future.done():
            return
        stall_timeout = self._turn_stall_timeout_seconds
        if deadline is None and stall_timeout is None:
            return
        if deadline is not None and deadline <= time.monotonic():
            raise asyncio.TimeoutError()
        idle_seconds = time.monotonic() - state.last_event_at
        if stall_timeout is not None and idle_seconds >= stall_timeout:
            await self._recover_stalled_turn(
                state,
                turn_id,
                thread_id=thread_id,
                idle_seconds=idle_seconds,
            )

    async def _maybe_reconcile_turn_completion_gap(
        self,
        state: _TurnState,
        *,
        turn_id: str,
        thread_id: Optional[str],
    ) -> None:
        if state.future.done() or state.turn_completed_seen:
            return
        completion_gap_timeout = self._turn_completion_gap_timeout_seconds
        if completion_gap_timeout is None or thread_id is None:
            return
        completion_gap_started_at = state.completion_gap_started_at
        if completion_gap_started_at is None:
            return
        now = time.monotonic()
        if (
            state.last_stream_delta_at
            and now - state.last_stream_delta_at < completion_gap_timeout
        ):
            return
        completion_gap_seconds = now - completion_gap_started_at
        if completion_gap_seconds < completion_gap_timeout:
            return
        min_interval = self._turn_stall_recovery_min_interval_seconds
        if (
            min_interval is not None
            and state.last_completion_gap_recovery_at
            and now - state.last_completion_gap_recovery_at < min_interval
        ):
            return

        state.last_completion_gap_recovery_at = now
        state.completion_gap_recovery_attempts += 1
        log_event(
            self._logger,
            logging.WARNING,
            "app_server.turn_completion_gap",
            turn_id=turn_id,
            thread_id=thread_id,
            completion_gap_seconds=round(completion_gap_seconds, 2),
            item_completed_count=state.item_completed_count,
            last_method=state.last_method,
            recovery_attempts=state.completion_gap_recovery_attempts,
        )
        try:
            resume_result = await self.thread_resume(thread_id)
        except (
            CodexAppServerError,
            CircuitOpenError,
            asyncio.TimeoutError,
            ConnectionError,
            OSError,
        ) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.turn_completion_gap_recovery.failed",
                turn_id=turn_id,
                thread_id=thread_id,
                completion_gap_seconds=round(completion_gap_seconds, 2),
                item_completed_count=state.item_completed_count,
                exc=exc,
            )
            self._maybe_fail_completion_gap_turn(
                state,
                turn_id=turn_id,
                thread_id=thread_id,
                completion_gap_seconds=completion_gap_seconds,
                reason="thread_resume_failed",
                recovery_status=state.status,
            )
            return

        snapshot = extract_resume_snapshot(resume_result, turn_id)
        if snapshot is None:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.turn_completion_gap_recovery.missing_snapshot",
                turn_id=turn_id,
                thread_id=thread_id,
                completion_gap_seconds=round(completion_gap_seconds, 2),
                item_completed_count=state.item_completed_count,
            )
            self._maybe_fail_completion_gap_turn(
                state,
                turn_id=turn_id,
                thread_id=thread_id,
                completion_gap_seconds=completion_gap_seconds,
                reason="resume_snapshot_missing",
                recovery_status=state.status,
            )
            return

        status = self._apply_resume_snapshot(state, snapshot)
        if status and _status_is_terminal(status) and not state.future.done():
            await self._emit_recovered_turn_completed_notification(
                state, thread_id=thread_id, recovery_source="turn_completion_gap"
            )
            log_event(
                self._logger,
                logging.INFO,
                "app_server.turn_completion_gap_recovery.completed",
                turn_id=turn_id,
                thread_id=thread_id,
                status=status,
                item_completed_count=state.item_completed_count,
                recovery_attempts=state.completion_gap_recovery_attempts,
            )
            self._set_turn_result_if_pending(state)
            return

        log_event(
            self._logger,
            logging.INFO,
            "app_server.turn_completion_gap_recovery.pending",
            turn_id=turn_id,
            thread_id=thread_id,
            status=state.status,
            item_completed_count=state.item_completed_count,
            recovery_attempts=state.completion_gap_recovery_attempts,
        )
        self._maybe_fail_completion_gap_turn(
            state,
            turn_id=turn_id,
            thread_id=thread_id,
            completion_gap_seconds=completion_gap_seconds,
            reason="resume_non_terminal",
            recovery_status=state.status,
        )

    async def _recover_stalled_turn(
        self,
        state: _TurnState,
        turn_id: str,
        *,
        thread_id: Optional[str],
        idle_seconds: float,
    ) -> None:
        now = time.monotonic()
        if thread_id is None:
            state.last_event_at = now
            return
        min_interval = self._turn_stall_recovery_min_interval_seconds
        if (
            min_interval is not None
            and state.last_recovery_at
            and now - state.last_recovery_at < min_interval
        ):
            return
        state.last_recovery_at = now
        state.recovery_attempts += 1
        log_event(
            self._logger,
            logging.WARNING,
            "app_server.turn_stalled",
            turn_id=turn_id,
            thread_id=thread_id,
            idle_seconds=round(idle_seconds, 2),
            last_method=state.last_method,
            recovery_attempts=state.recovery_attempts,
            max_recovery_attempts=self._turn_stall_max_recovery_attempts,
        )
        try:
            resume_result = await self.thread_resume(thread_id)
        except (
            CodexAppServerError,
            CircuitOpenError,
            asyncio.TimeoutError,
            ConnectionError,
            OSError,
        ) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.turn_recovery.failed",
                turn_id=turn_id,
                thread_id=thread_id,
                idle_seconds=round(idle_seconds, 2),
                exc=exc,
            )
            self._maybe_fail_stalled_turn(
                state,
                turn_id=turn_id,
                thread_id=thread_id,
                idle_seconds=idle_seconds,
                reason="thread_resume_failed",
                recovery_status=state.status,
            )
            state.last_event_at = now
            return

        snapshot = extract_resume_snapshot(resume_result, turn_id)
        if snapshot is None:
            self._maybe_fail_stalled_turn(
                state,
                turn_id=turn_id,
                thread_id=thread_id,
                idle_seconds=idle_seconds,
                reason="resume_snapshot_missing",
                recovery_status=state.status,
            )
            state.last_event_at = now
            return

        status = self._apply_resume_snapshot(state, snapshot)

        if status and _status_is_terminal(status) and not state.future.done():
            await self._emit_recovered_turn_completed_notification(
                state, thread_id=thread_id, recovery_source="turn_stall"
            )
            self._set_turn_result_if_pending(state)
            return

        self._maybe_fail_stalled_turn(
            state,
            turn_id=turn_id,
            thread_id=thread_id,
            idle_seconds=idle_seconds,
            reason="resume_non_terminal",
            recovery_status=state.status,
        )
        state.last_event_at = now

    def _apply_resume_snapshot(
        self,
        state: _TurnState,
        snapshot: tuple[Optional[str], list[str], list[str], list[str], list[str]],
    ) -> Optional[str]:
        status, agent_messages, commentary_messages, final_answer_messages, errors = (
            snapshot
        )
        if agent_messages:
            state.agent_messages = agent_messages
            state.commentary_messages = commentary_messages
            state.final_answer_messages = final_answer_messages
            # Resume snapshots include full message bodies, so older streaming
            # deltas from before recovery are stale once we adopt them.
            state.agent_message_deltas.clear()
        if errors:
            state.errors.extend(errors)
        if status:
            state.status = status
        return status

    async def _emit_recovered_turn_completed_notification(
        self,
        state: _TurnState,
        *,
        thread_id: str,
        recovery_source: str,
    ) -> None:
        if state.turn_completed_seen:
            return
        params = {
            "turnId": state.turn_id,
            "threadId": thread_id,
            "status": state.status,
            "recoveredVia": "thread/resume",
            "recoverySource": recovery_source,
            "synthetic": True,
        }
        message = {
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": params,
        }
        self._mark_notification_event(state=state, method="turn/completed")
        self._apply_turn_completed(state, message, params)
        if self._notification_handler is None:
            return
        self._schedule_notification_handler(message, method="turn/completed")

    def _schedule_notification_handler(
        self, message: Dict[str, Any], *, method: str, handled: bool = True
    ) -> None:
        handler = self._notification_handler
        if handler is None:
            return

        async def _invoke_notification_handler() -> None:
            try:
                await _maybe_await(handler(message))
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

    def _maybe_fail_stalled_turn(
        self,
        state: _TurnState,
        *,
        turn_id: str,
        thread_id: str,
        idle_seconds: float,
        reason: str,
        recovery_status: Optional[str],
    ) -> None:
        if state.future.done():
            return
        max_attempts = self._turn_stall_max_recovery_attempts
        if max_attempts is None or state.recovery_attempts < max_attempts:
            return

        error = (
            "Turn stalled and recovery exhausted: "
            f"attempts={state.recovery_attempts}, "
            f"max_attempts={max_attempts}, "
            f"reason={reason}, "
            f"last_method={state.last_method or 'unknown'}, "
            f"status={recovery_status or state.status or 'unknown'}."
        )
        state.status = "failed"
        state.errors.append(error)
        state.raw_events.append(
            {
                "method": "turn/stalledRecoveryExhausted",
                "params": {
                    "turnId": turn_id,
                    "threadId": thread_id,
                    "reason": reason,
                    "recoveryAttempts": state.recovery_attempts,
                    "maxRecoveryAttempts": max_attempts,
                    "lastMethod": state.last_method,
                    "status": recovery_status or state.status,
                    "idleSeconds": round(idle_seconds, 2),
                },
            }
        )
        log_event(
            self._logger,
            logging.ERROR,
            "app_server.turn_recovery.exhausted",
            turn_id=turn_id,
            thread_id=thread_id,
            reason=reason,
            idle_seconds=round(idle_seconds, 2),
            last_method=state.last_method,
            status=recovery_status or state.status,
            recovery_attempts=state.recovery_attempts,
            max_recovery_attempts=max_attempts,
        )
        self._set_turn_result_if_pending(state)

    def _maybe_fail_completion_gap_turn(
        self,
        state: _TurnState,
        *,
        turn_id: str,
        thread_id: str,
        completion_gap_seconds: float,
        reason: str,
        recovery_status: Optional[str],
    ) -> None:
        if state.future.done():
            return
        max_attempts = self._turn_stall_max_recovery_attempts
        if (
            max_attempts is None
            or state.completion_gap_recovery_attempts < max_attempts
        ):
            return

        error = (
            "Turn completion-gap recovery exhausted: "
            f"attempts={state.completion_gap_recovery_attempts}, "
            f"max_attempts={max_attempts}, "
            f"reason={reason}, "
            f"last_method={state.last_method or 'unknown'}, "
            f"status={recovery_status or state.status or 'unknown'}, "
            f"item_completed_count={state.item_completed_count}."
        )
        state.status = "failed"
        state.errors.append(error)
        state.raw_events.append(
            {
                "method": "turn/completionGapRecoveryExhausted",
                "params": {
                    "turnId": turn_id,
                    "threadId": thread_id,
                    "reason": reason,
                    "recoveryAttempts": state.completion_gap_recovery_attempts,
                    "maxRecoveryAttempts": max_attempts,
                    "lastMethod": state.last_method,
                    "status": recovery_status or state.status,
                    "completionGapSeconds": round(completion_gap_seconds, 2),
                    "itemCompletedCount": state.item_completed_count,
                },
            }
        )
        log_event(
            self._logger,
            logging.ERROR,
            "app_server.turn_completion_gap_recovery.exhausted",
            turn_id=turn_id,
            thread_id=thread_id,
            completion_gap_seconds=round(completion_gap_seconds, 2),
            recovery_attempts=state.completion_gap_recovery_attempts,
            max_recovery_attempts=max_attempts,
            reason=reason,
            last_method=state.last_method,
            status=recovery_status or state.status,
            item_completed_count=state.item_completed_count,
        )
        self._set_turn_result_if_pending(state)

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
        loop_state = _ReadLoopState()
        try:
            while True:
                chunk = await self._process.stdout.read(_READ_CHUNK_SIZE)
                if not chunk:
                    break
                if loop_state.dropping_oversize:
                    await self._read_loop_drain_oversize_chunk(
                        chunk, buffer=buffer, state=loop_state
                    )
                else:
                    await self._read_loop_collect_chunk(
                        chunk, buffer=buffer, state=loop_state
                    )
                if loop_state.dropping_oversize:
                    continue
                await self._handle_partial_payload_lines(buffer)
            await self._finalize_read_loop(buffer, loop_state)
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

    async def _read_loop_collect_chunk(
        self, chunk: bytes, *, buffer: bytearray, state: _ReadLoopState
    ) -> None:
        buffer.extend(chunk)
        await self._drain_buffer_lines(buffer=buffer)
        if self._initializing or len(buffer) <= self._max_message_bytes:
            return
        oversized = bytes(buffer)
        buffer.clear()
        state.dropping_oversize = True
        await self._read_loop_drain_oversize_chunk(
            oversized, buffer=buffer, state=state
        )

    async def _read_loop_drain_oversize_chunk(
        self, chunk: bytes, *, buffer: bytearray, state: _ReadLoopState
    ) -> None:
        newline_index = chunk.find(b"\n")
        if newline_index == -1:
            await self._track_oversize_fragment(chunk=chunk, state=state)
            return
        before = chunk[: newline_index + 1]
        after = chunk[newline_index + 1 :]
        if not state.drain_limit_reached:
            self._append_oversize_preview(chunk=before, state=state)
            state.oversize_bytes_dropped += len(before)
            await self._emit_oversize_warning(
                bytes_dropped=state.oversize_bytes_dropped,
                preview=state.oversize_preview,
            )
        state.dropping_oversize = False
        state.drain_limit_reached = False
        state.oversize_preview = bytearray()
        state.oversize_bytes_dropped = 0
        if after:
            buffer.extend(after)
            await self._handle_partial_payload_lines(buffer)
            if len(buffer) > self._max_message_bytes:
                state.oversize_preview = bytearray(
                    buffer[: self._oversize_preview_bytes]
                )
                state.oversize_bytes_dropped = len(buffer)
                buffer.clear()
                state.dropping_oversize = True

    async def _handle_partial_payload_lines(self, buffer: bytearray) -> None:
        await self._drain_buffer_lines(buffer=buffer)

    async def _track_oversize_fragment(
        self, *, chunk: bytes, state: _ReadLoopState
    ) -> None:
        if state.drain_limit_reached:
            return
        self._append_oversize_preview(chunk=chunk, state=state)
        state.oversize_bytes_dropped += len(chunk)
        if state.oversize_bytes_dropped >= self._max_oversize_drain_bytes:
            await self._emit_oversize_warning(
                bytes_dropped=state.oversize_bytes_dropped,
                preview=state.oversize_preview,
                aborted=True,
                drain_limit=self._max_oversize_drain_bytes,
            )
            state.drain_limit_reached = True

    def _append_oversize_preview(self, *, chunk: bytes, state: _ReadLoopState) -> None:
        if len(state.oversize_preview) >= self._oversize_preview_bytes:
            return
        remaining = self._oversize_preview_bytes - len(state.oversize_preview)
        state.oversize_preview.extend(chunk[:remaining])

    async def _finalize_read_loop(
        self, buffer: bytearray, state: _ReadLoopState
    ) -> None:
        if state.dropping_oversize:
            if state.oversize_bytes_dropped:
                await self._emit_oversize_warning(
                    bytes_dropped=state.oversize_bytes_dropped,
                    preview=state.oversize_preview,
                    truncated=True,
                )
            return
        if not buffer:
            return
        if len(buffer) > self._max_message_bytes:
            await self._emit_oversize_warning(
                bytes_dropped=len(buffer),
                preview=buffer[: self._oversize_preview_bytes],
                truncated=True,
            )
        else:
            await self._handle_payload_line(buffer)

    async def _drain_buffer_lines(self, *, buffer: bytearray) -> None:
        while True:
            newline_index = buffer.find(b"\n")
            if newline_index == -1:
                break
            line = buffer[:newline_index]
            del buffer[: newline_index + 1]
            await self._handle_payload_line(line)

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
        normalized = normalize_server_request(message)
        if normalized is None:
            return

        method = normalized.method
        req_id = normalized.request_id
        params = normalized.params
        decoded_request = decode_notification(message)
        if method in APPROVAL_METHODS:
            turn_id = (
                getattr(decoded_request, "turn_id", None)
                if decoded_request is not None
                else None
            )
            log_event(
                self._logger,
                logging.INFO,
                "app_server.approval.requested",
                request_id=req_id,
                method=method,
                turn_id=turn_id or params.get("turnId"),
            )
            decision: ApprovalDecision = self._default_approval_decision
            if self._approval_handler is not None:
                try:
                    decision = await _maybe_await(self._approval_handler(message))
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
        await self._send_message(
            self._build_message(
                req_id=req_id,
                error={"code": -32601, "message": f"Unsupported method: {method}"},
            )
        )

    async def _handle_notification(self, message: Dict[str, Any]) -> None:
        normalized = normalize_notification(message)
        if normalized is None:
            return

        method = normalized.method
        params = normalized.params
        decoded_notification = decode_notification(message)
        handled = False
        await self._mark_notification_turn_hint(method=method, params=params)
        handler = self._resolve_notification_handler(method)
        if handler is not None:
            handled = await handler(message, params, decoded_notification)
        if self._notification_handler is not None:
            try:
                await _maybe_await(self._notification_handler(message))
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
        if not turn_id:
            return None
        _key, state = await self._find_turn_state(turn_id, thread_id=thread_id)
        if state is not None:
            return state
        if thread_id:
            return self._ensure_turn_state(turn_id, thread_id)
        if create_pending:
            return self._ensure_pending_turn_state(turn_id)
        return None

    def _mark_notification_event(self, *, state: _TurnState, method: str) -> None:
        now = time.monotonic()
        state.last_event_at = now
        state.last_method = method
        if (
            not state.turn_completed_seen
            and state.item_completed_count > 0
            and method
            not in {"item/completed", "turn/completed", "error", "turn/error"}
        ):
            state.completion_gap_started_at = now

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
        state.last_stream_delta_at = time.monotonic()
        self._mark_notification_event(state=state, method="item/agentMessage/delta")
        _record_raw_event(state, message)
        if state.turn_completed_seen and not state.future.done():
            self._schedule_turn_completion_settle(state)
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
        )
        self._pending_turns[turn_id] = state
        return state

    def _merge_turn_state(self, target: _TurnState, source: _TurnState) -> None:
        if not target.agent_messages:
            target.agent_messages = list(source.agent_messages)
        else:
            target.agent_messages.extend(source.agent_messages)
        if not target.commentary_messages:
            target.commentary_messages = list(source.commentary_messages)
        else:
            target.commentary_messages.extend(source.commentary_messages)
        if not target.final_answer_messages:
            target.final_answer_messages = list(source.final_answer_messages)
        else:
            target.final_answer_messages.extend(source.final_answer_messages)
        if source.agent_message_deltas:
            target.agent_message_deltas.update(source.agent_message_deltas)
        if not target.raw_events:
            target.raw_events = list(source.raw_events)
        else:
            target.raw_events.extend(source.raw_events)
        _trim_raw_events(target)
        if not target.errors:
            target.errors = list(source.errors)
        else:
            target.errors.extend(source.errors)
        if source.last_event_at > target.last_event_at:
            target.last_event_at = source.last_event_at
            target.last_method = source.last_method
        elif target.last_method is None and source.last_method is not None:
            target.last_method = source.last_method
        target.turn_completed_seen = (
            target.turn_completed_seen or source.turn_completed_seen
        )
        target.item_completed_count = max(
            target.item_completed_count, source.item_completed_count
        )
        if target.completion_gap_started_at is None:
            target.completion_gap_started_at = source.completion_gap_started_at
        elif source.completion_gap_started_at is not None:
            target.completion_gap_started_at = max(
                target.completion_gap_started_at,
                source.completion_gap_started_at,
            )
        target.last_stream_delta_at = max(
            target.last_stream_delta_at,
            source.last_stream_delta_at,
        )
        target.completion_gap_recovery_attempts = max(
            target.completion_gap_recovery_attempts,
            source.completion_gap_recovery_attempts,
        )
        target.last_completion_gap_recovery_at = max(
            target.last_completion_gap_recovery_at,
            source.last_completion_gap_recovery_at,
        )
        if target.status is None and source.status is not None:
            target.status = source.status
        if source.future.done() and not target.future.done():
            self._set_turn_result_if_pending(target)
            return
        if source.turn_completed_seen and not target.future.done():
            self._schedule_turn_completion_settle(target)
        self._cancel_turn_completion_settle(source)

    def _build_turn_result(self, state: _TurnState) -> TurnResult:
        return TurnResult(
            turn_id=state.turn_id,
            status=state.status,
            final_message=_final_message_for_result(state, policy=self._output_policy),
            agent_messages=_agent_messages_for_result(state),
            commentary_messages=list(state.commentary_messages),
            errors=list(state.errors),
            raw_events=list(state.raw_events),
        )

    def _cancel_turn_completion_settle(self, state: _TurnState) -> None:
        settle_task = state.completion_settle_task
        if settle_task is not None and not settle_task.done():
            settle_task.cancel()
        state.completion_settle_task = None

    def _set_turn_result_if_pending(
        self, state: _TurnState, *, cancel_settle_task: bool = True
    ) -> None:
        if state.future.done():
            return
        if cancel_settle_task:
            self._cancel_turn_completion_settle(state)
        state.future.set_result(self._build_turn_result(state))

    def _schedule_turn_completion_settle(self, state: _TurnState) -> None:
        if state.future.done():
            return
        delay_seconds = max(float(_TURN_COMPLETION_SETTLE_SECONDS), 0.0)
        if delay_seconds <= 0:
            self._set_turn_result_if_pending(state)
            return
        self._cancel_turn_completion_settle(state)

        async def _finalize_after_settle() -> None:
            try:
                await asyncio.sleep(delay_seconds)
            except asyncio.CancelledError:
                return
            state.completion_settle_task = None
            self._set_turn_result_if_pending(state, cancel_settle_task=False)

        state.completion_settle_task = asyncio.create_task(_finalize_after_settle())

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
        self,
        state: _TurnState,
        message: Dict[str, Any],
        params: Any,
        decoded: Any = None,
    ) -> None:
        item = params.get("item") if isinstance(params, dict) else None
        text: Optional[str] = None

        if isinstance(item, dict) and item.get("type") == "agentMessage":
            item_id = params.get("itemId") if isinstance(params, dict) else None
            delta_text: Optional[str] = None
            text = _extract_agent_message_text(item)
            phase = _extract_agent_message_phase(item)
            if isinstance(item_id, str):
                delta_text = state.agent_message_deltas.pop(item_id, None)
            elif text:
                _prune_unambiguous_stale_delta(
                    state.agent_message_deltas, completed_text=text
                )
            if not text:
                text = delta_text
            _append_agent_message_for_phase(state, text, phase=phase)
        review_text = _extract_review_text(item)
        if review_text and review_text != text:
            _append_agent_message(state.agent_messages, review_text)
        state.item_completed_count += 1
        if not state.turn_completed_seen:
            state.completion_gap_started_at = state.last_event_at
        item_type = item.get("type") if isinstance(item, dict) else None
        log_event(
            self._logger,
            logging.INFO,
            "app_server.item.completed",
            turn_id=state.turn_id,
            thread_id=state.thread_id,
            item_type=item_type,
            item_completed_count=state.item_completed_count,
        )
        _record_raw_event(state, message)

    def _apply_error(
        self,
        state: _TurnState,
        message: Dict[str, Any],
        params: Any,
        decoded: Any = None,
    ) -> None:
        error_message = getattr(decoded, "message", None) or _extract_error_message(
            params
        )
        if error_message:
            state.errors.append(error_message)
        error_payload = params.get("error") if isinstance(params, dict) else None
        error_code = getattr(decoded, "code", None)
        if error_code is None:
            error_code = (
                error_payload.get("code") if isinstance(error_payload, dict) else None
            )
        will_retry = getattr(decoded, "will_retry", None)
        if will_retry is None and isinstance(params, dict):
            will_retry = params.get("willRetry")
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
        _record_raw_event(state, message)

    def _apply_turn_completed(
        self,
        state: _TurnState,
        message: Dict[str, Any],
        params: Any,
        decoded: Any = None,
    ) -> None:
        _record_raw_event(state, message)
        status = getattr(decoded, "status", None)
        if status is None and isinstance(params, dict):
            status = params.get("status")
            if status is None and isinstance(params.get("turn"), dict):
                turn_status = params["turn"].get("status")
                if isinstance(turn_status, dict):
                    status = turn_status.get("type") or turn_status.get("status")
                elif isinstance(turn_status, str):
                    status = turn_status
        state.status = status if status is not None else state.status
        log_event(
            self._logger,
            logging.INFO,
            "app_server.turn.completed",
            turn_id=state.turn_id,
            thread_id=state.thread_id,
            status=state.status,
        )
        state.turn_completed_seen = True
        state.completion_gap_started_at = None
        if _status_prefers_completion_settle(state.status) or not _status_is_terminal(
            state.status
        ):
            self._schedule_turn_completion_settle(state)
            return
        self._set_turn_result_if_pending(state)

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


def _turn_key(thread_id: Optional[str], turn_id: Optional[str]) -> Optional[TurnKey]:
    if not thread_id or not turn_id:
        return None
    return (thread_id, turn_id)


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


def _append_agent_message(messages: list[str], candidate: Optional[str]) -> None:
    if not candidate:
        return
    if messages and messages[-1] == candidate:
        return
    messages.append(candidate)


def _append_agent_message_for_phase(
    state: _TurnState,
    candidate: Optional[str],
    *,
    phase: Optional[str],
) -> None:
    if not candidate:
        return
    _append_agent_message(state.agent_messages, candidate)
    if phase == "commentary":
        _append_agent_message(state.commentary_messages, candidate)
    elif phase == "final_answer":
        _append_agent_message(state.final_answer_messages, candidate)


def _record_raw_event(state: _TurnState, message: Dict[str, Any]) -> None:
    state.raw_events.append(message)
    _trim_raw_events(state)


def _trim_raw_events(state: _TurnState) -> None:
    if len(state.raw_events) > _MAX_TURN_RAW_EVENTS:
        state.raw_events = state.raw_events[-_MAX_TURN_RAW_EVENTS:]


def _agent_message_deltas_as_list(agent_message_deltas: Dict[str, str]) -> list[str]:
    return [
        text for text in agent_message_deltas.values() if isinstance(text, str) and text
    ]


def _prune_unambiguous_stale_delta(
    agent_message_deltas: Dict[str, str], *, completed_text: str
) -> None:
    cleaned_completed = completed_text.strip()
    if not cleaned_completed:
        return
    matching_keys = [
        item_id
        for item_id, delta_text in agent_message_deltas.items()
        if isinstance(delta_text, str)
        and delta_text.strip()
        and cleaned_completed.startswith(delta_text.strip())
    ]
    if len(matching_keys) == 1:
        agent_message_deltas.pop(matching_keys[0], None)


def _agent_messages_for_result(state: _TurnState) -> list[str]:
    messages = list(state.agent_messages)
    pending_deltas = _agent_message_deltas_as_list(state.agent_message_deltas)
    if not messages:
        return pending_deltas
    for text in pending_deltas:
        if not isinstance(text, str):
            continue
        candidate = text.strip()
        if not candidate:
            continue
        last = messages[-1].strip() if isinstance(messages[-1], str) else ""
        if last == candidate:
            continue
        messages.append(candidate)
    return messages


def _normalize_output_policy(policy: Optional[str]) -> str:
    candidate = str(policy or "").strip().lower()
    if candidate in _OUTPUT_POLICIES:
        return candidate
    return _DEFAULT_OUTPUT_POLICY


def _final_message_for_result(state: _TurnState, *, policy: str) -> str:
    final_answers = [
        msg.strip()
        for msg in state.final_answer_messages
        if isinstance(msg, str) and msg.strip()
    ]
    if final_answers:
        if policy == "all_agent_messages":
            return "\n\n".join(
                msg.strip()
                for msg in _agent_messages_for_result(state)
                if isinstance(msg, str) and msg.strip()
            )
        return final_answers[-1]
    messages = _agent_messages_for_result(state)
    cleaned = [msg.strip() for msg in messages if isinstance(msg, str) and msg.strip()]
    if not cleaned:
        return ""
    if policy == "all_agent_messages":
        return "\n\n".join(cleaned)
    return cleaned[-1]


def _extract_status_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("type", "status", "state"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def _status_is_terminal(status: Any) -> bool:
    normalized = _extract_status_value(status)
    if not isinstance(normalized, str):
        return False
    normalized = normalized.lower()
    return normalized in {
        "completed",
        "complete",
        "done",
        "failed",
        "error",
        "errored",
        "cancelled",
        "canceled",
        "interrupted",
        "stopped",
        "success",
        "succeeded",
    }


def _status_prefers_completion_settle(status: Any) -> bool:
    normalized = _extract_status_value(status)
    if not isinstance(normalized, str):
        return False
    return normalized.lower() in {
        "completed",
        "complete",
        "done",
        "success",
        "succeeded",
    }


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
