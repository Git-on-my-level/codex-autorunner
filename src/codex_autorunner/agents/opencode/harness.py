"""OpenCode harness: turns, progress buffers, SSE → CAR event envelope.

Surfaces consume ``{"message": {"method", "params"}}`` shaped events. When SSE is
sparse, :class:`OpenCodeHarness` polls ``list_messages`` and synthesizes
``message.part.updated`` for reasoning/tool/patch/usage deltas. Codex/Hermes get
turn events from their supervisors instead and do not use this polling path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx

from ...core.logging_utils import log_event
from ...core.orchestration.interfaces import FreshConversationRequiredError
from ...core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_raw_event,
)
from ...core.orchestration.turn_event_buffer import TurnEventBuffer
from ...core.sse import SSEEvent
from ...integrations.chat.agents import DEFAULT_CHAT_AGENT_MODELS
from ..base import AgentHarness
from ..types import (
    AgentId,
    ConversationRef,
    ModelCatalog,
    ModelSpec,
    RuntimeCapability,
    TerminalTurnResult,
    TurnRef,
)
from .progress_synthesis import (
    DescendantSessionTracker,
    start_silent_turn_progress,
)
from .progress_synthesis import (
    progress_event_shape_hint as _progress_event_shape_hint,
)
from .progress_synthesis import (
    synthetic_command_result_events as _synthetic_command_result_events,
)
from .protocol_payload import (
    extract_message_info,
    extract_message_phase,
    extract_status_type,
    status_is_idle,
)
from .runtime import (
    OpenCodeTurnOutput,
    build_turn_id,
    collect_opencode_output_from_events,
    extract_session_id,
    extract_turn_id,
    map_approval_policy_to_permission,
    opencode_event_is_progress_signal,
    opencode_stream_timeouts,
    parse_message_response,
    recover_last_assistant_message,
    split_model_id,
)
from .supervisor_protocol import OpenCodeHarnessSupervisorProtocol

_logger = logging.getLogger(__name__)
_GLOB_META_RE = re.compile(r"[*?\[\]{]")


@dataclass
class _PendingTurnConfig:
    model_payload: Optional[dict[str, str]]
    approval_mode: Optional[str]
    sandbox_policy: Optional[Any]
    question_policy: str
    permission_handler: Optional[Callable[[str, dict[str, Any]], Awaitable[str]]] = None
    question_handler: Optional[
        Callable[[str, dict[str, Any]], Awaitable[list[list[str]] | None]]
    ] = None
    prompt: Optional[str] = None
    reserved_workspace_root: Optional[Path] = None
    command_task: Optional[asyncio.Task[Any]] = None
    event_buffer: TurnEventBuffer = field(default_factory=TurnEventBuffer)
    pre_connected_event_queue: Optional[asyncio.Queue[Any]] = None
    pre_connected_stream_task: Optional[asyncio.Task[None]] = None
    pre_connected_event_seen: asyncio.Event = field(default_factory=asyncio.Event)
    pre_connected_queue_exhausted: bool = False
    synthetic_raw_events: list[dict[str, Any]] = field(default_factory=list)
    progress_events_published: int = 0
    progress_events_skipped_session: int = 0
    progress_events_idle: int = 0
    heartbeat_task: Optional[asyncio.Task[None]] = None
    message_progress_task: Optional[asyncio.Task[None]] = None
    message_progress_roles_seen: set[str] = field(default_factory=set)
    message_progress_part_signatures: dict[str, str] = field(default_factory=dict)


async def _pre_connect_event_stream(
    client: Any,
    workspace_root: Path,
    conversation_id: str,
    *,
    event_seen: Optional[asyncio.Event] = None,
) -> tuple[asyncio.Queue[Any], asyncio.Task[None]]:
    """Start SSE event stream before prompt so no events are missed."""
    event_queue: asyncio.Queue[Any] = asyncio.Queue()
    ready_event = asyncio.Event()

    async def _stream_to_queue() -> None:
        try:
            async for event in client.stream_events(
                directory=str(workspace_root),
                session_id=conversation_id,
                ready_event=ready_event,
            ):
                if event_seen is not None and _preconnected_event_counts_as_progress(
                    event,
                    conversation_id=conversation_id,
                ):
                    event_seen.set()
                await event_queue.put(event)
        except (
            RuntimeError,
            OSError,
            ProcessLookupError,
            BrokenPipeError,
            httpx.HTTPError,
        ):  # intentional: background SSE consumer must not crash
            _logger.debug("Pre-connected SSE stream error", exc_info=True)
        finally:
            await event_queue.put(None)

    task = asyncio.create_task(_stream_to_queue())
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        _logger.debug("SSE pre-connect timed out after 2s, continuing anyway")
    return event_queue, task


def _preconnected_event_counts_as_progress(
    event: SSEEvent,
    *,
    conversation_id: str,
) -> bool:
    return opencode_event_is_progress_signal(
        event,
        session_id=conversation_id,
    )


def _path_is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _permission_candidate_path(value: Any, workspace_root: Path) -> Optional[Path]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    match = _GLOB_META_RE.search(text)
    if match is not None:
        text = text[: match.start()]
    text = text.rstrip("/")
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = workspace_root / path
    return path.resolve(strict=False)


def _collect_permission_paths(
    props: dict[str, Any], workspace_root: Path
) -> list[Path]:
    candidates: list[Path] = []

    def _append(value: Any) -> None:
        path = _permission_candidate_path(value, workspace_root)
        if path is not None:
            candidates.append(path)

    for key in ("path", "filepath", "directory"):
        _append(props.get(key))

    patterns = props.get("patterns")
    if isinstance(patterns, list):
        for item in patterns:
            _append(item)

    metadata = props.get("metadata")
    if isinstance(metadata, dict):
        for key in ("path", "filepath", "directory"):
            _append(metadata.get(key))
        nested_patterns = metadata.get("patterns")
        if isinstance(nested_patterns, list):
            for item in nested_patterns:
                _append(item)

    return candidates


def _observe_background_task(task: asyncio.Task[Any]) -> None:
    def _consume_exception(done: asyncio.Task[Any]) -> None:
        # intentional: silently consume exceptions from background tasks
        with contextlib.suppress(
            asyncio.CancelledError,
            RuntimeError,
            OSError,
            ProcessLookupError,
            BrokenPipeError,
        ):
            done.exception()

    task.add_done_callback(_consume_exception)


def _fresh_conversation_error(
    exc: Exception,
    *,
    conversation_id: str,
    operation: str,
) -> Optional[FreshConversationRequiredError]:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    response = exc.response
    request = exc.request
    status_code = response.status_code if response is not None else None
    if status_code not in {400, 404}:
        return None
    path = request.url.path if request is not None else None
    if not isinstance(path, str) or "/session/" not in path:
        return None
    return FreshConversationRequiredError(
        (
            f"OpenCode session '{conversation_id}' rejected {operation}; "
            "refresh the conversation binding"
        ),
        conversation_id=conversation_id,
        operation=operation,
        status_code=status_code,
    )


def _raise_fresh_conversation_error(
    exc: Exception,
    *,
    conversation_id: str,
    operation: str,
) -> None:
    refresh_error = _fresh_conversation_error(
        exc,
        conversation_id=conversation_id,
        operation=operation,
    )
    if refresh_error is None:
        raise exc
    log_event(
        _logger,
        logging.INFO,
        "opencode.conversation.invalidated",
        conversation_id=conversation_id,
        operation=refresh_error.operation,
        status_code=refresh_error.status_code,
    )
    raise refresh_error from exc


def _workspace_permission_decision(
    props: dict[str, Any],
    *,
    workspace_root: Path,
) -> str:
    permission_kind = (
        str(props.get("permission") or props.get("type") or props.get("tool") or "")
        .strip()
        .lower()
    )
    paths = _collect_permission_paths(props, workspace_root)
    if paths:
        if all(_path_is_within(workspace_root, path) for path in paths):
            return "allow"
        return "reject"
    if permission_kind in {"external_directory", "external_file", "external_path"}:
        return "reject"
    return "allow"


_extract_message_info = extract_message_info
_extract_message_phase = extract_message_phase


def _unwrap_harness_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if isinstance(payload.get("message"), dict):
        message = payload["message"]
        method = message.get("method")
        params = message.get("params")
        if isinstance(method, str) and isinstance(params, dict):
            return method, params
    method = payload.get("method")
    params = payload.get("params")
    if isinstance(method, str) and isinstance(params, dict):
        return method, params
    return "", {}


def _saw_terminal_completion(payloads: list[dict[str, Any]]) -> bool:
    for payload in payloads:
        method, params = _unwrap_harness_payload(payload)
        if method == "turn/completed":
            return True
        if method == "session.idle":
            return True
        if method == "session.status":
            if status_is_idle(extract_status_type(params)):
                return True
        if method == "message.completed":
            return _extract_message_phase(params) != "commentary"
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                return _extract_message_phase(params) != "commentary"
    return False


def _coerce_providers(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        providers = payload.get("providers")
        if isinstance(providers, list):
            return [entry for entry in providers if isinstance(entry, dict)]
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


def _iter_provider_models(models_raw: Any) -> list[tuple[str, dict[str, Any]]]:
    models: list[tuple[str, dict[str, Any]]] = []
    if isinstance(models_raw, dict):
        for model_id, model in models_raw.items():
            if isinstance(model_id, str) and model_id:
                if isinstance(model, dict):
                    models.append((model_id, model))
                else:
                    models.append((model_id, {"id": model_id}))
        return models
    if isinstance(models_raw, list):
        for entry in models_raw:
            if isinstance(entry, dict):
                model_id = entry.get("id") or entry.get("modelID")
                if isinstance(model_id, str) and model_id:
                    models.append((model_id, entry))
            elif isinstance(entry, str) and entry:
                models.append((entry, {"id": entry}))
    return models


class OpenCodeHarness(AgentHarness):
    """Map OpenCode HTTP/SSE (and message snapshots) onto CAR harness APIs."""

    agent_id: AgentId = AgentId("opencode")
    display_name = "OpenCode"
    capabilities = frozenset(
        [
            RuntimeCapability("durable_threads"),
            RuntimeCapability("message_turns"),
            RuntimeCapability("interrupt"),
            RuntimeCapability("active_thread_discovery"),
            RuntimeCapability("review"),
            RuntimeCapability("model_listing"),
            RuntimeCapability("event_streaming"),
        ]
    )

    def allows_parallel_event_stream(self) -> bool:
        return True

    async def list_progress_events(
        self, conversation_id: str, turn_id: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Return buffered progress events for a pending turn (snapshot-friendly)."""
        _ = kwargs
        pending = self._pending_turns.get((conversation_id, turn_id or ""))
        if pending is None:
            for key, candidate in self._pending_turns.items():
                if key[0] == conversation_id:
                    _logger.warning(
                        "list_progress_events: turn_id mismatch for conversation_id=%r; "
                        "requested=%r actual=%r — using fallback",
                        conversation_id,
                        turn_id,
                        key[1],
                    )
                    pending = candidate
                    break
        if pending is None:
            sample_keys = list(self._pending_turns.keys())[:5]
            _logger.warning(
                "list_progress_events: no pending turn for conversation_id=%r turn_id=%r; "
                "known_keys_count=%d sample_keys=%r",
                conversation_id,
                turn_id,
                len(self._pending_turns),
                sample_keys,
            )
            return []
        return pending.event_buffer.snapshot()

    def __init__(self, supervisor: OpenCodeHarnessSupervisorProtocol) -> None:
        self._supervisor = supervisor
        self._pending_turns: dict[tuple[str, str], _PendingTurnConfig] = {}
        self._reserved_conversations: dict[str, Path] = {}

    async def _acquire_turn_client(
        self, workspace_root: Path
    ) -> tuple[Any, Optional[Path]]:
        canonical_workspace = workspace_root.resolve()
        guarded_getter = getattr(self._supervisor, "get_client_for_turn", None)
        if callable(guarded_getter):
            client = await guarded_getter(canonical_workspace)
            return client, canonical_workspace
        client = await self._supervisor.get_client(canonical_workspace)
        marker = getattr(self._supervisor, "mark_turn_started", None)
        if not callable(marker):
            return client, None
        await marker(canonical_workspace)
        return client, canonical_workspace

    async def _release_turn_client(self, workspace_root: Optional[Path]) -> None:
        if workspace_root is None:
            return
        marker = getattr(self._supervisor, "mark_turn_finished", None)
        if not callable(marker):
            return
        try:
            await marker(workspace_root)
        except (
            OSError,
            RuntimeError,
            ProcessLookupError,
        ):  # intentional: cleanup path must not raise
            _logger.debug("turn release marker failed", exc_info=True)

    async def ensure_ready(self, workspace_root: Path) -> None:
        await self._supervisor.get_client(workspace_root)

    async def backend_runtime_instance_id(self, workspace_root: Path) -> Optional[str]:
        resolver = getattr(
            self._supervisor,
            "backend_runtime_instance_id_for_workspace",
            None,
        )
        if not callable(resolver):
            return None
        runtime_instance_id = await resolver(workspace_root)
        if not isinstance(runtime_instance_id, str):
            return None
        normalized = runtime_instance_id.strip()
        return normalized or None

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog:
        client = await self._supervisor.get_client(workspace_root)
        payload = await client.providers(directory=str(workspace_root))
        providers = _coerce_providers(payload)
        models: list[ModelSpec] = []
        default_model = ""
        if isinstance(payload, dict):
            raw_default = payload.get("default")
            if isinstance(raw_default, dict):
                for provider in providers:
                    provider_id = provider.get("id") or provider.get("providerID")
                    if (
                        isinstance(provider_id, str)
                        and provider_id
                        and provider_id in raw_default
                    ):
                        default_model_id = raw_default[provider_id]
                        if isinstance(default_model_id, str) and default_model_id:
                            default_model = f"{provider_id}/{default_model_id}"
                            break
        for provider in providers:
            provider_id = provider.get("id") or provider.get("providerID")
            if not isinstance(provider_id, str) or not provider_id:
                continue
            models_raw = provider.get("models")
            for model_id, model in _iter_provider_models(models_raw):
                name = model.get("name") or model.get("id") or model_id
                display_name = name if isinstance(name, str) and name else model_id
                capabilities = model.get("capabilities")
                supports_reasoning = False
                if isinstance(capabilities, dict):
                    supports_reasoning = bool(capabilities.get("reasoning"))
                variants = model.get("variants")
                reasoning_options: list[str] = []
                if isinstance(variants, dict):
                    reasoning_options = [
                        key for key in variants.keys() if isinstance(key, str)
                    ]
                    if reasoning_options:
                        supports_reasoning = True
                models.append(
                    ModelSpec(
                        id=f"{provider_id}/{model_id}",
                        display_name=display_name,
                        supports_reasoning=supports_reasoning,
                        reasoning_options=reasoning_options,
                    )
                )
        if not default_model and models:
            default_model = models[0].id
        return ModelCatalog(default_model=default_model, models=models)

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        canonical_workspace = workspace_root.resolve()
        client, reserved_workspace = await self._acquire_turn_client(
            canonical_workspace
        )
        try:
            result = await client.create_session(
                title=title,
                directory=str(canonical_workspace),
            )
            session_id = extract_session_id(result) or result.get("id")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("OpenCode did not return a session id")
            if reserved_workspace is not None:
                self._reserved_conversations[session_id] = reserved_workspace
            return ConversationRef(agent=AgentId("opencode"), id=session_id)
        except (
            RuntimeError,
            OSError,
            ProcessLookupError,
            BrokenPipeError,
            httpx.HTTPError,
        ):  # intentional: catch-all to ensure cleanup, then re-raise
            _logger.debug("create_session failed, releasing turn client", exc_info=True)
            await self._release_turn_client(reserved_workspace)
            raise

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        client = await self._supervisor.get_client(workspace_root)
        result = await client.list_sessions(directory=str(workspace_root))
        sessions: list[dict[str, Any]] = []
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                sessions = [entry for entry in data if isinstance(entry, dict)]
        elif isinstance(result, list):
            sessions = [entry for entry in result if isinstance(entry, dict)]
        conversations: list[ConversationRef] = []
        for entry in sessions:
            session_id = extract_session_id(entry) or entry.get("id")
            if isinstance(session_id, str) and session_id:
                conversations.append(
                    ConversationRef(agent=AgentId("opencode"), id=session_id)
                )
        return conversations

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        canonical_workspace = workspace_root.resolve()
        client, reserved_workspace = await self._acquire_turn_client(
            canonical_workspace
        )
        try:
            result = await client.get_session(conversation_id)
        except (
            RuntimeError,
            OSError,
            ProcessLookupError,
            BrokenPipeError,
            httpx.HTTPError,
        ) as exc:
            await self._release_turn_client(reserved_workspace)
            _raise_fresh_conversation_error(
                exc,
                conversation_id=conversation_id,
                operation="resume_conversation",
            )
        session_id = extract_session_id(result) or conversation_id
        if reserved_workspace is not None:
            self._reserved_conversations[session_id] = reserved_workspace
        return ConversationRef(agent=AgentId("opencode"), id=session_id)

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> TurnRef:
        _ = input_items
        canonical_workspace = workspace_root.resolve()
        reserved_workspace = self._reserved_conversations.pop(conversation_id, None)
        try:
            if reserved_workspace is None:
                client, reserved_workspace = await self._acquire_turn_client(
                    canonical_workspace
                )
            else:
                client = await self._supervisor.get_client(canonical_workspace)
        except (
            RuntimeError,
            OSError,
            ProcessLookupError,
            BrokenPipeError,
        ):  # intentional: catch-all to ensure cleanup, then re-raise
            _logger.debug(
                "start_turn client acquisition failed, releasing turn client",
                exc_info=True,
            )
            await self._release_turn_client(reserved_workspace)
            raise
        if model is None:
            model = DEFAULT_CHAT_AGENT_MODELS.get("opencode")
        model_payload = split_model_id(model)
        pre_connected_event_seen = asyncio.Event()
        event_queue, stream_task = await _pre_connect_event_stream(
            client,
            canonical_workspace,
            conversation_id,
            event_seen=pre_connected_event_seen,
        )
        turn_id = build_turn_id(conversation_id)
        pending = _PendingTurnConfig(
            model_payload=model_payload,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
            prompt=prompt,
            reserved_workspace_root=reserved_workspace,
            question_policy=(
                "reject"
                if approval_mode is not None or sandbox_policy is not None
                else "ignore"
            ),
            pre_connected_event_queue=event_queue,
            pre_connected_stream_task=stream_task,
            pre_connected_event_seen=pre_connected_event_seen,
        )
        self._pending_turns[(conversation_id, turn_id)] = pending

        async def _run_prompt_async() -> Any:
            try:
                result = await client.prompt_async(
                    conversation_id,
                    message=prompt,
                    model=model_payload,
                    variant=reasoning,
                )
            except (
                RuntimeError,
                OSError,
                ProcessLookupError,
                BrokenPipeError,
                httpx.HTTPError,
            ) as exc:  # intentional: catches any client error for stale-conversation check
                _raise_fresh_conversation_error(
                    exc,
                    conversation_id=conversation_id,
                    operation="start_turn",
                )
            if (
                pending.progress_events_published == 0
                and not pending.pre_connected_event_seen.is_set()
            ):
                for raw_event in _synthetic_command_result_events(
                    conversation_id, result
                ):
                    pending.synthetic_raw_events.append(raw_event)
                    await pending.event_buffer.append(raw_event)
            return result

        command_task = asyncio.create_task(_run_prompt_async())
        _observe_background_task(command_task)
        pending.command_task = command_task
        await asyncio.sleep(0)
        if command_task.done():
            exc = command_task.exception()
            if exc is not None:
                self._pending_turns.pop((conversation_id, turn_id), None)
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
                await self._release_turn_client(reserved_workspace)
                raise exc
            result = command_task.result()
            resolved_turn_id = extract_turn_id(conversation_id, result)
            if resolved_turn_id and resolved_turn_id != turn_id:
                self._pending_turns.pop((conversation_id, turn_id), None)
                self._pending_turns[(conversation_id, resolved_turn_id)] = pending
                turn_id = resolved_turn_id
        else:
            (
                pending.heartbeat_task,
                pending.message_progress_task,
            ) = start_silent_turn_progress(
                command_task,
                pending,
                conversation_id,
                client,
            )
        return TurnRef(
            conversation_id=conversation_id,
            turn_id=turn_id,
        )

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> TurnRef:
        canonical_workspace = workspace_root.resolve()
        reserved_workspace = self._reserved_conversations.pop(conversation_id, None)
        try:
            if reserved_workspace is None:
                client, reserved_workspace = await self._acquire_turn_client(
                    canonical_workspace
                )
            else:
                client = await self._supervisor.get_client(canonical_workspace)
        except (
            RuntimeError,
            OSError,
            ProcessLookupError,
            BrokenPipeError,
        ):  # intentional: catch-all to ensure cleanup, then re-raise
            _logger.debug(
                "start_review client acquisition failed, releasing turn client",
                exc_info=True,
            )
            await self._release_turn_client(reserved_workspace)
            raise
        if model is None:
            model = DEFAULT_CHAT_AGENT_MODELS.get("opencode")
        arguments = prompt if prompt else ""
        pre_connected_event_seen = asyncio.Event()
        event_queue, stream_task = await _pre_connect_event_stream(
            client,
            canonical_workspace,
            conversation_id,
            event_seen=pre_connected_event_seen,
        )
        turn_id = build_turn_id(conversation_id)
        pending = _PendingTurnConfig(
            model_payload=split_model_id(model),
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
            prompt=prompt,
            reserved_workspace_root=reserved_workspace,
            question_policy=(
                "reject"
                if approval_mode is not None or sandbox_policy is not None
                else "ignore"
            ),
            pre_connected_event_queue=event_queue,
            pre_connected_stream_task=stream_task,
            pre_connected_event_seen=pre_connected_event_seen,
        )
        self._pending_turns[(conversation_id, turn_id)] = pending

        async def _run_review_command() -> Any:
            try:
                result = await client.send_command(
                    conversation_id,
                    command="review",
                    arguments=arguments,
                    model=model,
                )
            except (
                RuntimeError,
                OSError,
                ProcessLookupError,
                BrokenPipeError,
                httpx.HTTPError,
            ) as exc:  # intentional: catches any client error for stale-conversation check
                _raise_fresh_conversation_error(
                    exc,
                    conversation_id=conversation_id,
                    operation="start_review",
                )
            if (
                pending.progress_events_published == 0
                and not pending.pre_connected_event_seen.is_set()
            ):
                for raw_event in _synthetic_command_result_events(
                    conversation_id, result
                ):
                    pending.synthetic_raw_events.append(raw_event)
                    await pending.event_buffer.append(raw_event)
            return result

        command_task = asyncio.create_task(_run_review_command())
        _observe_background_task(command_task)
        pending.command_task = command_task
        await asyncio.sleep(0)
        if command_task.done():
            exc = command_task.exception()
            if exc is not None:
                self._pending_turns.pop((conversation_id, turn_id), None)
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
                await self._release_turn_client(reserved_workspace)
                raise exc
            result = command_task.result()
            resolved_turn_id = extract_turn_id(conversation_id, result)
            if resolved_turn_id and resolved_turn_id != turn_id:
                self._pending_turns.pop((conversation_id, turn_id), None)
                self._pending_turns[(conversation_id, resolved_turn_id)] = pending
                turn_id = resolved_turn_id
        else:
            (
                pending.heartbeat_task,
                pending.message_progress_task,
            ) = start_silent_turn_progress(
                command_task,
                pending,
                conversation_id,
                client,
            )
        return TurnRef(conversation_id=conversation_id, turn_id=turn_id)

    def configure_turn_handlers(
        self,
        conversation_id: str,
        turn_id: str,
        *,
        permission_handler: Optional[
            Callable[[str, dict[str, Any]], Awaitable[str]]
        ] = None,
        question_handler: Optional[
            Callable[[str, dict[str, Any]], Awaitable[list[list[str]] | None]]
        ] = None,
    ) -> None:
        pending = self._pending_turns.get((conversation_id, turn_id or ""))
        if pending is None:
            return
        pending.permission_handler = permission_handler
        pending.question_handler = question_handler

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        client = await self._supervisor.get_client(workspace_root)
        try:
            await client.abort(conversation_id)
        except (httpx.HTTPError, ConnectionError, OSError) as exc:
            _logger.debug(
                "Failed to abort OpenCode session %s: %s", conversation_id, exc
            )

    async def _stream_opencode_sse_wrapped_payloads(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Live SSE for wait_for_turn when no in-memory pending turn exists."""
        _ = turn_id
        client = await self._supervisor.get_client(workspace_root)
        async for event in client.stream_events(
            directory=str(workspace_root),
            session_id=conversation_id,
        ):
            payload = event.data
            try:
                parsed = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                parsed = {"raw": payload}
            session_id = extract_session_id(parsed)
            if (
                event.event == "session.idle"
                or (
                    event.event == "session.status"
                    and status_is_idle(extract_status_type(parsed))
                )
            ) and session_id == conversation_id:
                break
            if session_id and session_id != conversation_id:
                continue
            wrapped = {"message": {"method": event.event, "params": parsed}}
            yield wrapped

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        _ = workspace_root
        pending = self._pending_turns.get((conversation_id, turn_id or ""))
        if pending is None:
            for key, candidate in self._pending_turns.items():
                if key[0] == conversation_id:
                    _logger.warning(
                        "stream_events: turn_id mismatch for conversation_id=%r; "
                        "requested=%r actual=%r — using fallback",
                        conversation_id,
                        turn_id,
                        key[1],
                    )
                    pending = candidate
                    break
        if pending is None:
            sample_keys = list(self._pending_turns.keys())[:5]
            _logger.warning(
                "stream_events: no pending turn for conversation_id=%r turn_id=%r; "
                "known_keys_count=%d sample_keys=%r",
                conversation_id,
                turn_id,
                len(self._pending_turns),
                sample_keys,
            )
            return
        events_yielded = 0
        try:
            async for event in pending.event_buffer.tail():
                events_yielded += 1
                yield event
        finally:
            log_event(
                _logger,
                logging.INFO,
                "opencode.harness.stream_events.done",
                conversation_id=conversation_id,
                turn_id=turn_id or "",
                events_yielded=events_yielded,
                progress_buffer_published=pending.progress_events_published,
                progress_buffer_skipped_session=pending.progress_events_skipped_session,
                progress_buffer_idle=pending.progress_events_idle,
            )

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        pending = self._pending_turns.get((conversation_id, turn_id or ""))
        client = await self._supervisor.get_client(workspace_root)
        workspace_root = workspace_root.resolve()

        async def _collect() -> TerminalTurnResult:
            if pending is None:
                raw_events: list[dict[str, Any]] = []
                stream_error: Optional[Exception] = None

                try:
                    async for payload in self._stream_opencode_sse_wrapped_payloads(
                        workspace_root,
                        conversation_id,
                        turn_id or "",
                    ):
                        if isinstance(payload, dict):
                            raw_events.append(payload)
                except (
                    RuntimeError,
                    OSError,
                    ProcessLookupError,
                    BrokenPipeError,
                    httpx.HTTPError,
                ) as exc:
                    stream_error = exc

                normalized_state = RuntimeThreadRunEventState()
                for raw_event in raw_events:
                    await normalize_runtime_thread_raw_event(
                        raw_event, normalized_state
                    )
                assistant_text = normalized_state.best_assistant_text().strip()
                errors = (
                    [normalized_state.last_error_message]
                    if normalized_state.last_error_message
                    else []
                )
                if not assistant_text and not errors:
                    messages_payload: Any = None
                    try:
                        messages_payload = await client.list_messages(
                            conversation_id, limit=10
                        )
                    except (
                        RuntimeError,
                        OSError,
                        ProcessLookupError,
                        BrokenPipeError,
                        httpx.HTTPError,
                    ) as exc:
                        log_event(
                            _logger,
                            logging.WARNING,
                            "opencode.harness.wait_for_turn.list_messages_failed",
                            conversation_id=conversation_id,
                            turn_id=turn_id or "",
                            error=str(exc),
                        )
                    recovered = recover_last_assistant_message(messages_payload)
                    if recovered.text:
                        assistant_text = recovered.text
                    if recovered.error:
                        errors = [recovered.error]
                if stream_error is not None and not (
                    assistant_text and _saw_terminal_completion(raw_events)
                ):
                    raise stream_error
                return TerminalTurnResult(
                    status="error" if errors else "ok",
                    assistant_text=assistant_text,
                    errors=errors,
                    raw_events=raw_events,
                )

            streamed_raw_events: list[dict[str, Any]] = []
            tracker = DescendantSessionTracker(conversation_id)

            async def _publish_progress_event(raw_event: dict[str, Any]) -> None:
                if not raw_event:
                    return
                await pending.event_buffer.append(raw_event)

            async def _close_progress_streams() -> None:
                await pending.event_buffer.close()

            async def _process_stream_event(event: Any) -> None:
                payload = event.data
                try:
                    parsed = json.loads(payload) if payload else {}
                except json.JSONDecodeError:
                    parsed = {"raw": payload}
                if isinstance(parsed, dict):
                    await tracker.maybe_track_descendant_session(parsed, client)
                    wrapped = {"message": {"method": event.event, "params": parsed}}
                    event_session_id = extract_session_id(parsed)
                    is_idle = event.event == "session.idle" or (
                        event.event == "session.status"
                        and status_is_idle(extract_status_type(parsed))
                    )
                    if is_idle:
                        pending.progress_events_idle += 1
                    visible_events: list[dict[str, Any]] = []
                    if not event_session_id or event_session_id == conversation_id:
                        visible_events = [wrapped]
                    elif event_session_id in tracker.watched_session_ids:
                        visible_events = tracker.descendant_progress_events(
                            event.event,
                            parsed,
                            session_id=event_session_id,
                        )
                    else:
                        pending.progress_events_skipped_session += 1
                        log_event(
                            _logger,
                            logging.INFO,
                            "opencode.progress_event.skipped",
                            method=event.event,
                            conversation_id=conversation_id,
                            event_session_id=event_session_id,
                            reason="session_mismatch",
                            shape_hint=_progress_event_shape_hint(parsed),
                        )
                        return

                    for visible_event in visible_events:
                        streamed_raw_events.append(visible_event)
                        if not is_idle:
                            pending.progress_events_published += 1
                            await _publish_progress_event(visible_event)

            async def _event_stream() -> AsyncIterator[Any]:
                pre_queue = pending.pre_connected_event_queue
                try:
                    if (
                        pre_queue is not None
                        and not pending.pre_connected_queue_exhausted
                    ):
                        while True:
                            event = await pre_queue.get()
                            if event is None:
                                pending.pre_connected_queue_exhausted = True
                                break
                            await _process_stream_event(event)
                            yield event
                    if pre_queue is None or pending.pre_connected_queue_exhausted:
                        async for event in client.stream_events(
                            directory=str(workspace_root),
                            session_id=conversation_id,
                        ):
                            await _process_stream_event(event)
                            yield event
                finally:
                    await _close_progress_streams()

            async def _fetch_session() -> Any:
                statuses = await client.session_status(directory=str(workspace_root))
                if isinstance(statuses, dict):
                    session_status = statuses.get(conversation_id)
                    if session_status is None:
                        if command_task is not None and not command_task.done():
                            return {"status": {"type": "busy"}}
                        return {"status": {"type": "idle"}}
                    if isinstance(session_status, dict):
                        return {"status": session_status}
                    if isinstance(session_status, str):
                        return {"status": session_status}
                return {"status": {}}

            async def _fetch_providers() -> Any:
                return await client.providers(directory=str(workspace_root))

            async def _fetch_messages() -> Any:
                return await client.list_messages(conversation_id, limit=10)

            async def _respond_permission(request_id: str, reply: str) -> None:
                await client.respond_permission(request_id=request_id, reply=reply)

            async def _reply_question(
                request_id: str, answers: list[list[str]]
            ) -> None:
                await client.reply_question(request_id, answers=answers)

            async def _reject_question(request_id: str) -> None:
                await client.reject_question(request_id)

            permission_policy = map_approval_policy_to_permission(
                pending.approval_mode if pending is not None else None,
                default="allow",
            )
            permission_handler = None
            if permission_policy == "ask":
                if pending is not None and pending.permission_handler is not None:
                    permission_handler = pending.permission_handler

                else:

                    async def _permission_handler(
                        _request_id: str, props: dict[str, Any]
                    ) -> str:
                        return _workspace_permission_decision(
                            props,
                            workspace_root=workspace_root,
                        )

                    permission_handler = _permission_handler

            stall_timeout_seconds_value = (
                await self._supervisor.session_stall_timeout_seconds_for_workspace(
                    workspace_root
                )
            )
            stall_timeout, first_event_timeout = opencode_stream_timeouts(
                stall_timeout_seconds_value,
            )
            collect_task = asyncio.create_task(
                collect_opencode_output_from_events(
                    None,
                    session_id=conversation_id,
                    prompt=(pending.prompt if pending is not None else None),
                    model_payload=(
                        pending.model_payload if pending is not None else None
                    ),
                    progress_session_ids=tracker.watched_session_ids,
                    permission_policy=permission_policy,
                    permission_handler=permission_handler,
                    question_policy=(
                        pending.question_policy if pending is not None else "ignore"
                    ),
                    question_handler=(
                        pending.question_handler if pending is not None else None
                    ),
                    respond_permission=_respond_permission,
                    reply_question=_reply_question,
                    reject_question=_reject_question,
                    event_stream_factory=_event_stream,
                    session_fetcher=_fetch_session,
                    provider_fetcher=_fetch_providers,
                    messages_fetcher=_fetch_messages,
                    stall_timeout_seconds=stall_timeout,
                    first_event_timeout_seconds=first_event_timeout,
                )
            )
            command_task = pending.command_task if pending is not None else None
            if command_task is None:
                output = await collect_task
            else:
                try:
                    command_result: Any = None
                    done, _pending = await asyncio.wait(
                        {collect_task, command_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if collect_task in done:
                        output = await collect_task
                        if command_task in done:
                            command_result = command_task.result()
                        else:
                            command_result = await command_task
                    elif command_task in done:
                        command_exc = command_task.exception()
                        if command_exc is not None:
                            collect_task.cancel()
                            try:
                                await collect_task
                            except asyncio.CancelledError:
                                pass
                            raise command_exc
                        command_result = command_task.result()
                        if (
                            pending.progress_events_published == 0
                            and not pending.pre_connected_event_seen.is_set()
                        ):
                            collect_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await collect_task
                            recovered = parse_message_response(command_result)
                            if not recovered.text and not recovered.error:
                                messages_payload = await _fetch_messages()
                                recovered = recover_last_assistant_message(
                                    messages_payload,
                                    prompt=pending.prompt,
                                )
                            output = OpenCodeTurnOutput(
                                text=recovered.text,
                                error=recovered.error,
                            )
                        else:
                            output = await collect_task
                    if (
                        command_result is not None
                        and not output.text
                        and not output.error
                    ):
                        recovered = parse_message_response(command_result)
                        if recovered.text or recovered.error:
                            output = OpenCodeTurnOutput(
                                text=recovered.text or output.text,
                                error=recovered.error or output.error,
                                usage=output.usage,
                            )
                except (
                    Exception
                ) as exc:  # intentional: top-level turn error → error result conversion
                    return TerminalTurnResult(
                        status="error",
                        assistant_text="",
                        errors=[str(exc)],
                        raw_events=(
                            list(pending.synthetic_raw_events) + streamed_raw_events
                            if pending is not None
                            else streamed_raw_events
                        ),
                    )

            errors = [output.error] if output.error else []
            return TerminalTurnResult(
                status="error" if errors else "ok",
                assistant_text=output.text,
                errors=errors,
                raw_events=(
                    list(pending.synthetic_raw_events) + streamed_raw_events
                    if pending is not None
                    else streamed_raw_events
                ),
            )

        try:
            if timeout is None:
                return await _collect()
            return await asyncio.wait_for(_collect(), timeout=timeout)
        finally:
            heartbeat_task = pending.heartbeat_task if pending is not None else None
            message_progress_task = (
                pending.message_progress_task if pending is not None else None
            )
            pre_stream_task = (
                pending.pre_connected_stream_task if pending is not None else None
            )
            reserved_workspace = (
                pending.reserved_workspace_root if pending is not None else None
            )
            if turn_id is not None:
                self._pending_turns.pop((conversation_id, turn_id), None)
            if heartbeat_task is not None and not heartbeat_task.done():
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if message_progress_task is not None and not message_progress_task.done():
                message_progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await message_progress_task
            if pre_stream_task is not None and not pre_stream_task.done():
                pre_stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pre_stream_task
            await self._release_turn_client(reserved_workspace)


__all__ = ["OpenCodeHarness"]
