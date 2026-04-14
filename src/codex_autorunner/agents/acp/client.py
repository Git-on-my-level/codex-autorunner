from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Sequence

from ...core.acp_lifecycle import (
    coerce_mapping as _coerce_mapping,
)
from ...core.acp_lifecycle import (
    session_update_content_summary as _session_update_content_summary,
)
from ...core.acp_lifecycle import (
    should_map_missing_turn_id as _should_map_missing_turn_id,
)
from ...core.logging_utils import log_event
from ...core.text_utils import _normalize_optional_text
from .errors import (
    ACPError,
    ACPInitializationError,
    ACPMethodNotFoundError,
    ACPMissingSessionError,
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
    ACPAdvertisedCommand,
    ACPInitializeResult,
    ACPSessionCapabilities,
    ACPSessionDescriptor,
    ACPSessionForkResult,
    ACPSetModelResult,
    ACPSetModeResult,
    coerce_session_list,
    extract_advertised_commands,
    extract_session_capabilities,
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
_ACP_PROTOCOL_VERSION = 1
_ACP_STDOUT_NOISE_PREFIXES = (
    "┊",
    "╎",
    "│",
    "┌",
    "┐",
    "└",
    "┘",
    "├",
    "┤",
    "┬",
    "┴",
    "┼",
    "╭",
    "╮",
    "╰",
    "╯",
)
_ACP_STDOUT_BRACKETED_STATUS_RE = re.compile(r"^\[[^\]\s]{1,32}\]\s+(?![\[{])\S")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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
    replay_task: Optional[asyncio.Task[None]] = None
    request_task: Optional[asyncio.Task[Any]] = None
    request_started_at: Optional[float] = None
    completion_source: Optional[str] = None
    last_runtime_method: Optional[str] = None
    last_session_update_kind: Optional[str] = None
    last_session_update_excerpt: Optional[str] = None
    last_session_update_content_kind: Optional[str] = None
    last_session_update_part_types: tuple[str, ...] = ()
    last_session_update_text_length: Optional[int] = None
    last_session_update_at: Optional[float] = None


def _text_excerpt(value: Any, *, limit: int = 120) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


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


def _coerce_stdout_text(line: bytes) -> str:
    return line.decode("utf-8", errors="replace").strip()


def _strip_terminal_control_sequences(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _is_ignorable_stdout_noise(line: bytes) -> bool:
    text = _coerce_stdout_text(line)
    if not text:
        return True
    stripped = _strip_terminal_control_sequences(text).lstrip()
    return stripped.startswith(_ACP_STDOUT_NOISE_PREFIXES) or (
        _ACP_STDOUT_BRACKETED_STATUS_RE.match(stripped) is not None
    )


class ACPPromptHandle:
    def __init__(self, client: "ACPClient", turn_id: str) -> None:
        self._client = client
        self.turn_id = turn_id

    async def wait(self, *, timeout: Optional[float] = None) -> ACPPromptResult:
        return await self._client.wait_for_prompt(self.turn_id, timeout=timeout)

    async def events(self) -> AsyncIterator[ACPEvent]:
        async for event in self._client.iter_prompt_events(self.turn_id):
            yield event

    def snapshot_events(self) -> tuple[ACPEvent, ...]:
        return self._client.prompt_events_snapshot(self.turn_id)


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
        self._initialize_params = {"protocolVersion": _ACP_PROTOCOL_VERSION}
        self._initialize_params.update(dict(initialize_params or {}))
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
        self._turn_counter = 0
        self._session_active_turns: dict[str, str] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._trace_enabled = False

    @property
    def initialize_result(self) -> Optional[ACPInitializeResult]:
        return self._initialize_result

    @property
    def advertised_commands(self) -> list[ACPAdvertisedCommand]:
        if self._initialize_result is None:
            return []
        return extract_advertised_commands(self._initialize_result.capabilities)

    @property
    def session_capabilities(self) -> ACPSessionCapabilities:
        if self._initialize_result is None:
            return ACPSessionCapabilities()
        return extract_session_capabilities(self._initialize_result.capabilities)

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
                self._trace_enabled = self._should_trace_acp_runtime(
                    self._initialize_result
                )
                self._log_trace_event(
                    "acp.client.initialized",
                    server_name=self._initialize_result.server_name,
                    server_version=self._initialize_result.server_version,
                    protocol_version=self._initialize_result.protocol_version,
                    capabilities=self._initialize_result.capabilities,
                    command=self._command,
                    cwd=self._cwd,
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
        except (ACPTransportError, OSError):
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
        await self.start()
        started_at = time.monotonic()
        params: dict[str, Any] = {
            "cwd": cwd or self._cwd or str(Path.cwd()),
            "mcpServers": [],
        }
        if title:
            params["title"] = title
        if metadata:
            params["metadata"] = dict(metadata)
        result = await self.request("session/new", params)
        session = ACPSessionDescriptor.from_result(result)
        self._log_trace_event(
            "acp.session.new",
            session_id=session.session_id,
            cwd=params["cwd"],
            title=title,
            elapsed_ms=self._elapsed_ms(started_at),
            metadata_keys=sorted((metadata or {}).keys()),
        )
        return session

    async def load_session(self, session_id: str) -> ACPSessionDescriptor:
        await self.start()
        started_at = time.monotonic()
        params = {
            "cwd": self._cwd or str(Path.cwd()),
            "mcpServers": [],
            "sessionId": session_id,
        }
        try:
            result = await self.request("session/load", params)
        except ACPMissingSessionError:
            self._log_trace_event(
                "acp.session.load_missing",
                session_id=session_id,
                cwd=params["cwd"],
                elapsed_ms=self._elapsed_ms(started_at),
            )
            raise
        if result is None:
            self._log_trace_event(
                "acp.session.load_missing",
                session_id=session_id,
                cwd=params["cwd"],
                elapsed_ms=self._elapsed_ms(started_at),
            )
            raise ACPMissingSessionError(
                method="session/load",
                code=-32004,
                message=f"session not found: {session_id}",
            )
        payload = _coerce_mapping(result)
        try:
            session = ACPSessionDescriptor.from_result(payload)
        except ValueError:
            session = ACPSessionDescriptor(
                session_id=session_id,
                raw=payload,
            )
        self._log_trace_event(
            "acp.session.load",
            session_id=session.session_id,
            cwd=params["cwd"],
            elapsed_ms=self._elapsed_ms(started_at),
            sparse_payload=not bool(payload),
        )
        return session

    async def list_sessions(self) -> list[ACPSessionDescriptor]:
        await self.start()
        result = await self.request(
            "session/list",
            {"cwd": self._cwd or str(Path.cwd())},
        )
        return coerce_session_list(result)

    async def fork_session(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ACPSessionForkResult:
        await self.start()
        params: dict[str, Any] = {"sessionId": session_id}
        if title:
            params["title"] = title
        if metadata:
            params["metadata"] = dict(metadata)
        result = await self.call_optional("session/fork", params)
        fork_result = ACPSessionForkResult.from_optional_response(result)
        self._log_trace_event(
            "acp.session.fork",
            source_session_id=session_id,
            forked_session_id=fork_result.session_id,
            supported=fork_result.supported,
            title=title,
        )
        return fork_result

    async def set_session_model(
        self, session_id: str, model_id: str
    ) -> ACPSetModelResult:
        await self.start()
        result = await self.call_optional(
            "session/set_model",
            {"sessionId": session_id, "modelId": model_id},
        )
        set_result = ACPSetModelResult.from_optional_response(result)
        self._log_trace_event(
            "acp.session.set_model",
            session_id=session_id,
            model_id=model_id,
            supported=set_result.supported,
        )
        return set_result

    async def set_session_mode(self, session_id: str, mode: str) -> ACPSetModeResult:
        await self.start()
        result = await self.call_optional(
            "session/set_mode",
            {"sessionId": session_id, "mode": mode},
        )
        set_result = ACPSetModeResult.from_optional_response(result)
        self._log_trace_event(
            "acp.session.set_mode",
            session_id=session_id,
            mode=mode,
            supported=set_result.supported,
        )
        return set_result

    async def start_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ACPPromptHandle:
        await self.start()
        return await self._start_prompt_official(
            session_id,
            prompt,
            model=model,
            metadata=metadata,
        )

    async def cancel_prompt(self, session_id: str, turn_id: str) -> Any:
        await self.start()
        self._log_trace_event(
            "acp.prompt.cancel_requested",
            session_id=session_id,
            turn_id=turn_id,
        )
        await self.notify("session/cancel", {"sessionId": session_id})
        return None

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
            background_tasks = list(self._background_tasks)
            self._background_tasks.clear()
            for task in background_tasks:
                if task.done():
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for transport_task in (
                self._reader_task,
                self._stderr_task,
                self._wait_task,
            ):
                if transport_task is None:
                    continue
                if not transport_task.done():
                    transport_task.cancel()
                    try:
                        await transport_task
                    except asyncio.CancelledError:
                        pass
                self._consume_transport_task_result(transport_task)
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
            text = _coerce_stdout_text(line)
            try:
                message = json.loads(text)
            except json.JSONDecodeError as exc:
                if _is_ignorable_stdout_noise(line):
                    if text:
                        self._logger.warning(
                            "Ignoring non-JSON ACP stdout noise: %s",
                            text[:200],
                        )
                    continue
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
        message = self._message_with_mapped_turn_id(message)
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
                task = asyncio.create_task(
                    self._respond_to_permission_notification(event, decision)
                )
                task.add_done_callback(self._log_background_task_result)
        if self._notification_handler is not None:
            await self._notification_handler(event)
        if event.turn_id:
            state = self._prompts.get(event.turn_id)
            if state is None:
                self._orphan_events[event.turn_id].append(event)
            else:
                await self._record_prompt_event_in_order(state, event)

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
                await self._record_prompt_event_in_order(state, event)
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
            except (
                Exception
            ):  # intentional: user-provided permission handler is arbitrary code
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
            orphan_events = self._orphan_events.pop(turn_id, [])
            if orphan_events:
                task = asyncio.create_task(
                    self._replay_orphan_prompt_events(state, orphan_events)
                )
                state.replay_task = task
                task.add_done_callback(self._log_background_task_result)
        return state

    async def _register_prompt_turn_alias(
        self,
        state: _PromptState,
        alias_turn_id: Optional[str],
    ) -> None:
        normalized_alias = _normalize_optional_text(alias_turn_id)
        if not normalized_alias or normalized_alias == state.turn_id:
            return
        existing = self._prompts.get(normalized_alias)
        if existing is not None and existing is not state:
            raise ACPProtocolError(
                f"Official ACP turn alias '{normalized_alias}' collides with another prompt"
            )
        self._prompts[normalized_alias] = state
        orphan_events = self._orphan_events.pop(normalized_alias, [])
        if orphan_events:
            await self._replay_orphan_prompt_events(state, orphan_events)
        self._log_trace_event(
            "acp.prompt.turn_alias_registered",
            session_id=state.session_id,
            turn_id=state.turn_id,
            aliased_turn_id=normalized_alias,
            **self._prompt_trace_fields(state),
        )

    async def _replay_orphan_prompt_events(
        self,
        state: _PromptState,
        events: list[ACPEvent],
    ) -> None:
        for event in events:
            await self._record_prompt_event(state, event)

    async def _record_prompt_event_in_order(
        self,
        state: _PromptState,
        event: ACPEvent,
    ) -> None:
        replay_task = state.replay_task
        current_task = asyncio.current_task()
        if (
            replay_task is not None
            and replay_task is not current_task
            and not replay_task.done()
        ):
            await asyncio.shield(replay_task)
        await self._record_prompt_event(state, event)

    async def _finalize_prompt_with_event(
        self,
        state: _PromptState,
        event: ACPTurnTerminalEvent,
        *,
        completion_source: Optional[str] = None,
    ) -> None:
        if state.closed:
            return
        resolved_completion_source = completion_source or "terminal_event"
        state.closed = True
        state.completion_source = resolved_completion_source
        if self._session_active_turns.get(state.session_id) == state.turn_id:
            self._session_active_turns.pop(state.session_id, None)
        self._log_trace_event(
            "acp.prompt.terminal_recorded",
            session_id=state.session_id,
            turn_id=state.turn_id,
            status=event.status,
            completion_source=resolved_completion_source,
            error_message=event.error_message,
            elapsed_ms=self._elapsed_ms(state.request_started_at),
            **self._prompt_trace_fields(state),
        )
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

    def _log_background_task_result(self, task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # intentional: catch-all logging for background task failures
            self._logger.exception("Unhandled ACP background task failure")

    def _consume_transport_task_result(self, task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except ACPError:
            return
        except (
            Exception
        ):  # intentional: catch-all logging for transport task failures during shutdown
            self._logger.debug(
                "Unhandled ACP transport task failure during shutdown",
                exc_info=True,
            )

    def prompt_events_snapshot(self, turn_id: str) -> tuple[ACPEvent, ...]:
        state = self._prompts.get(turn_id)
        if state is None:
            return ()
        return tuple(state.events)

    async def _record_prompt_event(self, state: _PromptState, event: ACPEvent) -> None:
        if state.closed:
            return
        self._note_prompt_trace_event(state, event)
        state.events.append(event)
        if isinstance(event, ACPOutputDeltaEvent):
            state.final_output += event.delta
        elif isinstance(event, ACPMessageEvent) and event.message:
            state.final_output = event.message
        await state.queue.put(event)
        if not isinstance(event, ACPTurnTerminalEvent):
            return
        await self._finalize_prompt_with_event(state, event)

    async def _record_prompt_terminal_event(
        self,
        state: _PromptState,
        event: ACPTurnTerminalEvent,
        *,
        completion_source: str,
    ) -> None:
        if state.closed:
            return
        self._note_prompt_trace_event(state, event)
        state.events.append(event)
        if event.final_output:
            state.final_output = event.final_output
        await state.queue.put(event)
        await self._finalize_prompt_with_event(
            state,
            event,
            completion_source=completion_source,
        )

    async def _start_prompt_official(
        self,
        session_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ACPPromptHandle:
        self._turn_counter += 1
        turn_id = f"turn-{self._turn_counter}"
        state = self._ensure_prompt_state(session_id, turn_id)
        self._session_active_turns[session_id] = turn_id
        self._log_trace_event(
            "acp.prompt.started",
            session_id=session_id,
            turn_id=turn_id,
            model=model,
            prompt_chars=len(prompt),
            metadata_keys=sorted((metadata or {}).keys()),
        )
        await self._record_prompt_event(
            state,
            normalize_notification(
                {
                    "method": "prompt/started",
                    "params": {
                        "sessionId": session_id,
                        "turnId": turn_id,
                    },
                }
            ),
        )
        task = asyncio.create_task(
            self._run_official_prompt_request(
                state,
                prompt=prompt,
                model=model,
                metadata=metadata,
            )
        )
        state.request_task = task
        self._track_background_task(task)
        return ACPPromptHandle(self, turn_id)

    async def _run_official_prompt_request(
        self,
        state: _PromptState,
        *,
        prompt: str,
        model: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        state.request_started_at = time.monotonic()
        try:
            if model:
                await self.set_session_model(state.session_id, model)
            params: dict[str, Any] = {
                "sessionId": state.session_id,
                "messageId": state.turn_id,
                "prompt": [{"type": "text", "text": prompt}],
            }
            if metadata:
                params["metadata"] = dict(metadata)
            result = await self.request(
                "session/prompt",
                params,
            )
        except (ACPError, asyncio.TimeoutError) as exc:
            if state.closed:
                self._log_trace_event(
                    "acp.prompt.request_failed_reconciled",
                    session_id=state.session_id,
                    turn_id=state.turn_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    completion_source=state.completion_source or "terminal_event",
                    elapsed_ms=self._elapsed_ms(state.request_started_at),
                    **self._prompt_trace_fields(state),
                )
                return
            self._session_active_turns.pop(state.session_id, None)
            self._log_trace_event(
                "acp.prompt.request_failed",
                session_id=state.session_id,
                turn_id=state.turn_id,
                error=str(exc),
                error_type=type(exc).__name__,
                elapsed_ms=self._elapsed_ms(state.request_started_at),
                **self._prompt_trace_fields(state),
            )
            if not state.future.done():
                state.future.set_exception(exc)
            await state.queue.put(_QUEUE_SENTINEL)
            return
        result_payload = _coerce_mapping(result)
        response_turn_id = self._official_prompt_response_turn_id(result_payload)
        await self._register_prompt_turn_alias(state, response_turn_id)
        self._log_trace_event(
            "acp.prompt.request_returned",
            session_id=state.session_id,
            turn_id=state.turn_id,
            response_turn_id=response_turn_id,
            status=self._official_prompt_terminal_status(result_payload),
            stop_reason=_normalize_optional_text(
                result_payload.get("stopReason") or result_payload.get("stop_reason")
            ),
            completion_source="prompt_return",
            elapsed_ms=self._elapsed_ms(state.request_started_at),
            **self._prompt_trace_fields(state),
        )
        if state.closed:
            self._log_trace_event(
                "acp.prompt.request_reconciled",
                session_id=state.session_id,
                turn_id=state.turn_id,
                response_turn_id=response_turn_id,
                status=self._official_prompt_terminal_status(result_payload),
                stop_reason=_normalize_optional_text(
                    result_payload.get("stopReason")
                    or result_payload.get("stop_reason")
                ),
                completion_source=state.completion_source or "terminal_event",
                elapsed_ms=self._elapsed_ms(state.request_started_at),
                **self._prompt_trace_fields(state),
            )
            return
        terminal_event = normalize_notification(
            {
                "method": self._official_prompt_terminal_method(result),
                "params": {
                    "sessionId": state.session_id,
                    "turnId": state.turn_id,
                    "status": self._official_prompt_terminal_status(result),
                    "finalOutput": state.final_output,
                    "message": self._official_prompt_terminal_error(result),
                },
            }
        )
        if not isinstance(terminal_event, ACPTurnTerminalEvent):
            raise ACPProtocolError(
                "Official ACP prompt return did not normalize terminally"
            )
        await self._record_prompt_terminal_event(
            state,
            terminal_event,
            completion_source="prompt_return",
        )

    def _official_prompt_terminal_method(self, payload: Any) -> str:
        status = self._official_prompt_terminal_status(payload)
        if status == "cancelled":
            return "prompt/cancelled"
        if status == "failed":
            return "prompt/failed"
        return "prompt/completed"

    def _official_prompt_response_turn_id(self, payload: Any) -> Optional[str]:
        result = _coerce_mapping(payload)
        return _normalize_optional_text(
            result.get("userMessageId")
            or result.get("user_message_id")
            or result.get("turnId")
            or result.get("turn_id")
        )

    def _official_prompt_terminal_status(self, payload: Any) -> str:
        result = _coerce_mapping(payload)
        stop_reason = _normalize_optional_text(
            result.get("stopReason") or result.get("stop_reason")
        )
        if stop_reason == "cancelled":
            return "cancelled"
        if stop_reason == "refusal":
            return "failed"
        return "completed"

    def _official_prompt_terminal_error(self, payload: Any) -> Optional[str]:
        if self._official_prompt_terminal_status(payload) != "failed":
            return None
        result = _coerce_mapping(payload)
        return _normalize_optional_text(
            result.get("message")
            or result.get("error")
            or result.get("stopReason")
            or result.get("stop_reason")
        )

    def _message_with_mapped_turn_id(self, message: dict[str, Any]) -> dict[str, Any]:
        method = _normalize_optional_text(message.get("method"))
        params = _coerce_mapping(message.get("params"))
        if _normalize_optional_text(params.get("turnId") or params.get("turn_id")):
            return message
        session_id = _normalize_optional_text(
            params.get("sessionId") or params.get("session_id")
        )
        if not session_id:
            return message
        if not _should_map_missing_turn_id(method or "", params):
            return message
        turn_id = self._session_active_turns.get(session_id)
        if not turn_id:
            return message
        enriched = dict(message)
        enriched_params = dict(params)
        enriched_params["turnId"] = turn_id
        enriched["params"] = enriched_params
        return enriched

    def _track_background_task(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.add(task)

        def _discard(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            self._log_background_task_result(done)

        task.add_done_callback(_discard)

    def _should_trace_acp_runtime(self, result: ACPInitializeResult) -> bool:
        server_name = str(result.server_name or "").strip().lower()
        return "hermes" in server_name

    def _elapsed_ms(self, started_at: Optional[float]) -> Optional[int]:
        if started_at is None:
            return None
        return max(int((time.monotonic() - started_at) * 1000), 0)

    def _log_trace_event(self, event: str, **fields: Any) -> None:
        if not self._trace_enabled:
            return
        log_event(self._logger, logging.INFO, event, **fields)

    def _prompt_trace_fields(self, state: _PromptState) -> dict[str, Any]:
        return {
            "last_runtime_method": state.last_runtime_method,
            "last_session_update_kind": state.last_session_update_kind,
            "last_session_update_excerpt": state.last_session_update_excerpt,
            "last_session_update_content_kind": state.last_session_update_content_kind,
            "last_session_update_part_types": state.last_session_update_part_types,
            "last_session_update_text_length": state.last_session_update_text_length,
            "last_session_update_elapsed_ms": self._elapsed_ms(
                state.last_session_update_at
            ),
        }

    def _note_prompt_trace_event(self, state: _PromptState, event: ACPEvent) -> None:
        state.last_runtime_method = _normalize_optional_text(event.method)
        if event.method != "session/update":
            return
        update = _coerce_mapping(event.payload.get("update"))
        content_summary = _session_update_content_summary(update)
        extracted_text = str(content_summary.get("text") or "")
        state.last_session_update_kind = _normalize_optional_text(
            update.get("sessionUpdate") or update.get("session_update")
        )
        state.last_session_update_excerpt = _text_excerpt(extracted_text)
        state.last_session_update_content_kind = _normalize_optional_text(
            content_summary.get("content_kind")
        )
        state.last_session_update_part_types = tuple(
            str(item)
            for item in (content_summary.get("content_part_types") or ())
            if str(item).strip()
        )
        state.last_session_update_text_length = (
            len(extracted_text) if extracted_text else 0
        )
        state.last_session_update_at = time.monotonic()
        self._log_trace_event(
            "acp.prompt.session_update",
            session_id=state.session_id,
            turn_id=state.turn_id,
            session_update=state.last_session_update_kind,
            text_excerpt=state.last_session_update_excerpt,
            content_kind=state.last_session_update_content_kind,
            content_part_types=state.last_session_update_part_types,
            text_length=state.last_session_update_text_length,
        )
        if (
            state.last_session_update_kind
            in {"agent_message_chunk", "agent_thought_chunk"}
            and not extracted_text
        ):
            self._log_trace_event(
                "acp.prompt.session_update.unparsed",
                session_id=state.session_id,
                turn_id=state.turn_id,
                session_update=state.last_session_update_kind,
                content_kind=state.last_session_update_content_kind,
                content_part_types=state.last_session_update_part_types,
                raw_update=update,
            )

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
        if method == "session/load" and code_int == -32004:
            return ACPMissingSessionError(
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
