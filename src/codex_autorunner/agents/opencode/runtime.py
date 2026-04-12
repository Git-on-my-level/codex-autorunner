"""OpenCode turn execution: consume SSE, enforce stall/first-event timeouts, assemble output.

Which events count as forward progress (and thus reset stall timers) is OpenCode-
specific — see :func:`opencode_event_is_progress_signal` and busy vs idle
``session.status`` handling. Codex/Hermes paths use different event sources.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    MutableMapping,
    Optional,
    cast,
)

import httpx

from ...core.coercion import coerce_int
from ...core.logging_utils import log_event
from ...core.sse import SSEEvent
from ...core.utils import resolve_opencode_auth_path
from .constants import OPENCODE_MODEL_CONTEXT_KEYS
from .event_fields import (
    extract_message_id as extract_event_message_id,
)
from .event_fields import (
    extract_message_role as extract_event_message_role,
)
from .event_fields import (
    extract_part_id as extract_event_part_id,
)
from .event_fields import (
    extract_part_message_id as extract_event_part_message_id,
)
from .protocol_payload import (
    OPENCODE_PERMISSION_REJECT,
    PERMISSION_ALLOW,
    PERMISSION_ASK,
    PERMISSION_DENY,
    OpenCodeMessageResult,
    auto_answers_for_questions,
    build_turn_id,
    extract_context_window,
    extract_error_text,
    extract_message_phase,
    extract_model_ids,
    extract_permission_request,
    extract_question_request,
    extract_session_id,
    extract_status_type,
    extract_total_tokens,
    extract_turn_id,
    extract_usage_details,
    extract_visible_message_text,
    format_permission_prompt,
    map_approval_policy_to_permission,
    normalize_message_phase,
    normalize_permission_decision,
    normalize_question_answers,
    normalize_question_policy,
    parse_message_response,
    permission_policy_reply,
    recover_last_assistant_message,
    split_model_id,
    status_is_idle,
    summarize_question_answers,
)
from .usage_decoder import extract_usage

PermissionDecision = str
PermissionHandler = Callable[[str, dict[str, Any]], Awaitable[PermissionDecision]]
QuestionHandler = Callable[[str, dict[str, Any]], Awaitable[Optional[list[list[str]]]]]
PartHandler = Callable[[str, dict[str, Any], Optional[str]], Awaitable[None]]

_OPENCODE_STREAM_STALL_TIMEOUT_SECONDS = 60.0
_OPENCODE_FIRST_EVENT_TIMEOUT_SECONDS = 60.0
_OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 5.0, 10.0)
_OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS = 5
_OPENCODE_STREAM_MAX_STALL_RECONNECT_SECONDS = 120.0
_OPENCODE_STREAM_STALL_TIMEOUT_REASON = "opencode_stream_stalled_timeout"
_OPENCODE_FIRST_EVENT_TIMEOUT_REASON = "opencode_first_event_timeout"
_OPENCODE_POST_COMPLETION_GRACE_SECONDS = 5.0
_OPENCODE_ABSOLUTE_MAX_IDLE_SECONDS = 300.0


@dataclass(frozen=True)
class OpenCodeTurnOutput:
    text: str
    error: Optional[str] = None
    usage: Optional[dict[str, Any]] = None


_extract_error_text = extract_error_text
_extract_visible_message_text = extract_visible_message_text
_extract_message_phase = extract_message_phase
_extract_permission_request = extract_permission_request
_extract_question_request = extract_question_request
_extract_model_ids = extract_model_ids
_extract_total_tokens = extract_total_tokens
_extract_usage_details = extract_usage_details
_extract_context_window = extract_context_window
_extract_status_type = extract_status_type
_status_is_idle = status_is_idle
_normalize_message_phase = normalize_message_phase
_normalize_question_policy = normalize_question_policy
_normalize_permission_decision = normalize_permission_decision
_permission_policy_reply = permission_policy_reply
_auto_answers_for_questions = auto_answers_for_questions
_normalize_question_answers = normalize_question_answers
_summarize_question_answers = summarize_question_answers


def opencode_event_is_progress_signal(
    event: SSEEvent,
    *,
    session_id: str,
    progress_session_ids: Optional[set[str]] = None,
) -> bool:
    """Whether *event* should count as stream progress for stall / first-event logic.

    Ignores transport noise (``server.connected``, ``server.heartbeat``). For
    ``session.status``, only idle-like statuses count; busy heartbeats must not
    reset timers. Session scope: primary *session_id*, or any id in
    *progress_session_ids* when the turn spans multiple server sessions.

    Used only on the OpenCode SSE path; Codex/Hermes harnesses do not call this.
    """
    event_type = (event.event or "").strip().lower()
    if event_type in {"server.connected", "server.heartbeat"}:
        return False

    raw = event.data or ""
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}

    event_session_id = extract_session_id(payload)
    if event_session_id:
        if progress_session_ids is None:
            if event_session_id != session_id:
                return False
        elif event_session_id not in progress_session_ids:
            return False

    if event_type == "session.status":
        return _status_is_idle(_extract_status_type(payload))

    return True


async def opencode_missing_env(
    client: Any,
    workspace_root: str,
    model_payload: Optional[dict[str, str]],
    *,
    env: Optional[MutableMapping[str, str]] = None,
) -> list[str]:
    if not model_payload:
        return []
    provider_id = model_payload.get("providerID")
    if not provider_id:
        return []
    try:
        payload = await client.providers(directory=workspace_root)
    except (httpx.HTTPError, ValueError, OSError):
        return []
    providers: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        raw_providers = payload.get("providers")
        if isinstance(raw_providers, list):
            providers = [entry for entry in raw_providers if isinstance(entry, dict)]
    elif isinstance(payload, list):
        providers = [entry for entry in payload if isinstance(entry, dict)]
    for provider in providers:
        pid = provider.get("id") or provider.get("providerID")
        if not pid or pid != provider_id:
            continue
        if _provider_has_auth(pid, workspace_root):
            return []
        env_keys = provider.get("env")
        if not isinstance(env_keys, list):
            return []
        missing = [
            key
            for key in env_keys
            if isinstance(key, str) and key and not _get_env_value(key, env)
        ]
        return missing
    return []


def _get_env_value(
    key: str, env: Optional[MutableMapping[str, str]] = None
) -> Optional[str]:
    if env is not None:
        return env.get(key)
    return os.getenv(key)


def _provider_has_auth(provider_id: str, workspace_root: str) -> bool:
    auth_path = resolve_opencode_auth_path(workspace_root, env=os.environ)
    if auth_path is None or not auth_path.exists():
        return False
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    entry = payload.get(provider_id)
    return isinstance(entry, dict) and any(bool(value) for value in entry.values())


def opencode_stream_timeouts(
    stall_timeout_seconds: Optional[float] = None,
) -> tuple[float, float]:
    """Derive (stall_timeout, first_event_timeout) from a single stall config.

    All callers of ``collect_opencode_output`` / ``collect_opencode_output_from_events``
    should use this so timeout policy stays consistent.  When *stall_timeout_seconds*
    is ``None`` (not configured), the module default is used — callers never get an
    unbounded stall window.
    """
    resolved_stall = (
        stall_timeout_seconds
        if stall_timeout_seconds is not None
        else _OPENCODE_STREAM_STALL_TIMEOUT_SECONDS
    )
    first_event = min(resolved_stall, _OPENCODE_FIRST_EVENT_TIMEOUT_SECONDS)
    return (resolved_stall, first_event)


async def collect_opencode_output_from_events(
    events: Optional[AsyncIterator[SSEEvent]] = None,
    *,
    session_id: str,
    prompt: Optional[str] = None,
    model_payload: Optional[dict[str, str]] = None,
    progress_session_ids: Optional[set[str]] = None,
    permission_policy: str = PERMISSION_ALLOW,
    permission_handler: Optional[PermissionHandler] = None,
    question_policy: str = "ignore",
    question_handler: Optional[QuestionHandler] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    respond_permission: Optional[Callable[[str, str], Awaitable[None]]] = None,
    reply_question: Optional[Callable[[str, list[list[str]]], Awaitable[None]]] = None,
    reject_question: Optional[Callable[[str], Awaitable[None]]] = None,
    part_handler: Optional[PartHandler] = None,
    event_stream_factory: Optional[Callable[[], AsyncIterator[SSEEvent]]] = None,
    session_fetcher: Optional[Callable[[], Awaitable[Any]]] = None,
    provider_fetcher: Optional[Callable[[], Awaitable[Any]]] = None,
    messages_fetcher: Optional[Callable[[], Awaitable[Any]]] = None,
    stall_timeout_seconds: Optional[float] = _OPENCODE_STREAM_STALL_TIMEOUT_SECONDS,
    first_event_timeout_seconds: Optional[
        float
    ] = _OPENCODE_FIRST_EVENT_TIMEOUT_SECONDS,
    logger: Optional[logging.Logger] = None,
) -> OpenCodeTurnOutput:
    text_parts: list[str] = []
    part_lengths: dict[str, int] = {}
    last_full_text = ""
    error: Optional[str] = None
    message_roles: dict[str, str] = {}
    message_roles_seen = False
    pending_text: dict[str, list[str]] = {}
    pending_no_id: list[str] = []
    no_id_role: Optional[str] = None
    fallback_message: Optional[tuple[Optional[str], Optional[str], str]] = None
    last_completed_assistant_text: Optional[str] = None
    last_usage_signature: Optional[
        tuple[
            Optional[str],
            Optional[str],
            Optional[int],
            Optional[int],
            Optional[int],
            Optional[int],
            Optional[int],
            Optional[int],
        ]
    ] = None
    latest_usage_snapshot: Optional[dict[str, Any]] = None
    part_types: dict[str, str] = {}
    seen_question_request_ids: set[tuple[Optional[str], str]] = set()
    logged_permission_errors: set[str] = set()
    normalized_question_policy = _normalize_question_policy(question_policy)
    if logger is None:
        logger = logging.getLogger(__name__)
    providers_cache: Optional[list[dict[str, Any]]] = None
    context_window_cache: dict[str, Optional[int]] = {}
    session_model_ids: Optional[tuple[Optional[str], Optional[str]]] = None
    default_model_ids = (
        _extract_model_ids(model_payload) if isinstance(model_payload, dict) else None
    )

    def _register_message_role(payload: Any) -> tuple[Optional[str], Optional[str]]:
        nonlocal message_roles_seen
        if not isinstance(payload, dict):
            return None, None
        role = extract_event_message_role(payload)
        msg_id = extract_event_message_id(payload)
        if isinstance(role, str) and msg_id:
            message_roles[msg_id] = role
            message_roles_seen = True
        return msg_id, role

    def _flush_pending_no_id_as_assistant() -> None:
        nonlocal no_id_role
        if pending_no_id:
            text_parts.extend(pending_no_id)
            pending_no_id.clear()
        no_id_role = "assistant"

    def _discard_pending_no_id() -> None:
        if pending_no_id:
            pending_no_id.clear()

    def _append_text_for_message(message_id: Optional[str], text: str) -> None:
        if not text:
            return
        if message_id is None:
            if no_id_role == "assistant":
                text_parts.append(text)
            else:
                pending_no_id.append(text)
            return
        role = message_roles.get(message_id)
        if role == "user":
            return
        if role == "assistant":
            text_parts.append(text)
            return
        pending_text.setdefault(message_id, []).append(text)

    def _flush_pending_text(message_id: Optional[str]) -> None:
        if not message_id:
            return
        role = message_roles.get(message_id)
        if role != "assistant":
            pending_text.pop(message_id, None)
            return
        pending = pending_text.pop(message_id, [])
        if pending:
            text_parts.extend(pending)

    def _flush_all_pending_text() -> None:
        if pending_text:
            for pending in list(pending_text.values()):
                if pending:
                    text_parts.extend(pending)
            pending_text.clear()
        if pending_no_id:
            # If we have not seen a role yet, assume assistant for backwards
            # compatibility with providers that omit roles entirely. Otherwise,
            # only flush when we have already classified no-id text as assistant
            # or when we have no other text (to avoid echoing user prompts).
            if not message_roles_seen or no_id_role == "assistant" or not text_parts:
                text_parts.extend(pending_no_id)
            pending_no_id.clear()

    def _handle_role_update(message_id: Optional[str], role: Optional[str]) -> None:
        nonlocal no_id_role
        if not role:
            return
        if role == "assistant":
            _flush_pending_text(message_id)
            _flush_pending_no_id_as_assistant()
            return
        if role == "user":
            _flush_pending_text(message_id)
            _discard_pending_no_id()
            no_id_role = None

    async def _resolve_session_model_ids() -> tuple[Optional[str], Optional[str]]:
        nonlocal session_model_ids
        if session_model_ids is not None:
            return session_model_ids
        resolved_ids: Optional[tuple[Optional[str], Optional[str]]] = None
        if session_fetcher is not None:
            try:
                payload = await session_fetcher()
                resolved_ids = _extract_model_ids(payload)
            except (httpx.HTTPError, ValueError, OSError):
                resolved_ids = None
        # If we failed to resolve model ids from the session (including the empty
        # tuple case), fall back to the caller-provided model payload so we can
        # still backfill usage metadata.
        if not resolved_ids or all(value is None for value in resolved_ids):
            resolved_ids = default_model_ids
        session_model_ids = resolved_ids or (None, None)
        return session_model_ids

    async def _resolve_context_window_from_providers(
        provider_id: Optional[str], model_id: Optional[str]
    ) -> Optional[int]:
        nonlocal providers_cache
        if not provider_id or not model_id:
            return None
        cache_key = f"{provider_id}/{model_id}"
        if cache_key in context_window_cache:
            return context_window_cache[cache_key]
        if provider_fetcher is None:
            context_window_cache[cache_key] = None
            return None
        if providers_cache is None:
            try:
                payload = await provider_fetcher()
            except (httpx.HTTPError, ValueError, OSError):
                context_window_cache[cache_key] = None
                return None
            providers: list[dict[str, Any]] = []
            if isinstance(payload, dict):
                raw_providers = payload.get("providers")
                if isinstance(raw_providers, list):
                    providers = [
                        entry for entry in raw_providers if isinstance(entry, dict)
                    ]
            elif isinstance(payload, list):
                providers = [entry for entry in payload if isinstance(entry, dict)]
            providers_cache = providers
        context_window = None
        for provider in providers_cache or []:
            pid = provider.get("id") or provider.get("providerID")
            if pid != provider_id:
                continue
            models = provider.get("models")
            model_entry = None
            if isinstance(models, dict):
                candidate = models.get(model_id)
                if isinstance(candidate, dict):
                    model_entry = candidate
            elif isinstance(models, list):
                for entry in models:
                    if not isinstance(entry, dict):
                        continue
                    entry_id = entry.get("id") or entry.get("modelID")
                    if entry_id == model_id:
                        model_entry = entry
                        break
            if isinstance(model_entry, dict):
                limit = model_entry.get("limit") or model_entry.get("limits")
                if isinstance(limit, dict):
                    for key in OPENCODE_MODEL_CONTEXT_KEYS:
                        value = coerce_int(limit.get(key))
                        if value is not None and value > 0:
                            context_window = value
                            break
                if context_window is None:
                    for key in OPENCODE_MODEL_CONTEXT_KEYS:
                        value = coerce_int(model_entry.get(key))
                        if value is not None and value > 0:
                            context_window = value
                            break
            if context_window is None:
                limit = provider.get("limit") or provider.get("limits")
                if isinstance(limit, dict):
                    for key in OPENCODE_MODEL_CONTEXT_KEYS:
                        value = coerce_int(limit.get(key))
                        if value is not None and value > 0:
                            context_window = value
                            break
            break
        context_window_cache[cache_key] = context_window
        return context_window

    async def _emit_usage_update(payload: Any, *, is_primary_session: bool) -> None:
        nonlocal last_usage_signature, latest_usage_snapshot
        if not is_primary_session:
            return
        usage = extract_usage(payload)
        if usage is None:
            return
        provider_id, model_id = _extract_model_ids(payload)
        if not provider_id or not model_id:
            provider_id, model_id = await _resolve_session_model_ids()
        total_tokens = _extract_total_tokens(usage)
        context_window = _extract_context_window(payload, usage)
        if context_window is None:
            context_window = await _resolve_context_window_from_providers(
                provider_id, model_id
            )
        usage_details = _extract_usage_details(usage)
        usage_signature = (
            provider_id,
            model_id,
            total_tokens,
            usage_details.get("inputTokens"),
            usage_details.get("cachedInputTokens"),
            usage_details.get("outputTokens"),
            usage_details.get("reasoningTokens"),
            context_window,
        )
        if usage_signature == last_usage_signature:
            return
        last_usage_signature = usage_signature
        usage_snapshot: dict[str, Any] = {}
        if provider_id:
            usage_snapshot["providerID"] = provider_id
        if model_id:
            usage_snapshot["modelID"] = model_id
        if total_tokens is not None:
            usage_snapshot["totalTokens"] = total_tokens
        if usage_details:
            usage_snapshot.update(usage_details)
        if context_window is not None:
            usage_snapshot["modelContextWindow"] = context_window
        if usage_snapshot:
            latest_usage_snapshot = dict(usage_snapshot)
            if part_handler is not None:
                await part_handler("usage", usage_snapshot, None)

    stream_factory = event_stream_factory
    if events is None and stream_factory is None:
        raise ValueError("events or event_stream_factory must be provided")

    def _new_stream() -> AsyncIterator[SSEEvent]:
        if stream_factory is not None:
            return stream_factory()
        if events is None:
            raise ValueError("events or event_stream_factory must be provided")
        return events

    async def _close_stream(iterator: AsyncIterator[SSEEvent]) -> None:
        aclose = getattr(iterator, "aclose", None)
        if aclose is None:
            return
        with suppress(Exception):
            await aclose()

    stream_started_at = time.monotonic()
    stream_iter = _new_stream().__aiter__()
    last_relevant_event_at = stream_started_at
    received_any_event = False
    last_primary_completion_at: Optional[float] = None
    post_completion_deadline: Optional[float] = None
    reconnect_attempts = 0
    reconnect_started_at: Optional[float] = None
    can_reconnect = (
        event_stream_factory is not None and stall_timeout_seconds is not None
    )

    async def _fail_first_event_timeout(*, now: float) -> None:
        nonlocal error
        timeout_seconds = first_event_timeout_seconds
        if timeout_seconds is None:
            return
        idle_seconds = now - stream_started_at
        error = (
            f"{_OPENCODE_FIRST_EVENT_TIMEOUT_REASON}: "
            f"no relevant events received within {timeout_seconds:.1f}s"
        )
        log_event(
            logger,
            logging.ERROR,
            "opencode.stream.first_event_timeout",
            session_id=session_id,
            idle_seconds=idle_seconds,
            timeout_seconds=timeout_seconds,
        )
        if part_handler is not None:
            await part_handler(
                "status",
                {
                    "type": "first_event_timeout",
                    "reason": _OPENCODE_FIRST_EVENT_TIMEOUT_REASON,
                    "idleSeconds": idle_seconds,
                    "firstEventTimeoutSeconds": timeout_seconds,
                },
                None,
            )

    async def _attempt_reconnect(
        *,
        now: float,
        idle_seconds: float,
        status_type: Optional[str],
    ) -> bool:
        nonlocal stream_iter, reconnect_attempts, reconnect_started_at, error, last_relevant_event_at
        if not can_reconnect:
            return False

        if reconnect_started_at is None:
            reconnect_started_at = now
        stalled_elapsed_seconds = now - reconnect_started_at
        attempts_exceeded = (
            reconnect_attempts >= _OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS
        )
        elapsed_exceeded = (
            stalled_elapsed_seconds >= _OPENCODE_STREAM_MAX_STALL_RECONNECT_SECONDS
        )
        if attempts_exceeded or elapsed_exceeded:
            error = (
                f"{_OPENCODE_STREAM_STALL_TIMEOUT_REASON}: "
                f"stalled for {stalled_elapsed_seconds:.1f}s after "
                f"{reconnect_attempts} reconnect attempts"
            )
            log_event(
                logger,
                logging.ERROR,
                "opencode.stream.stalled.timeout",
                session_id=session_id,
                idle_seconds=idle_seconds,
                stalled_elapsed_seconds=stalled_elapsed_seconds,
                reconnect_attempts=reconnect_attempts,
                max_reconnect_attempts=_OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS,
                max_stalled_seconds=_OPENCODE_STREAM_MAX_STALL_RECONNECT_SECONDS,
                status_type=status_type,
            )
            if part_handler is not None:
                await part_handler(
                    "status",
                    {
                        "type": "stall_timeout",
                        "reason": _OPENCODE_STREAM_STALL_TIMEOUT_REASON,
                        "idleSeconds": idle_seconds,
                        "stalledSeconds": stalled_elapsed_seconds,
                        "attempts": reconnect_attempts,
                    },
                    None,
                )
            return False

        backoff_index = min(
            reconnect_attempts,
            len(_OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS) - 1,
        )
        backoff = _OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS[backoff_index]
        reconnect_attempts += 1
        log_event(
            logger,
            logging.WARNING,
            "opencode.stream.stalled.reconnecting",
            session_id=session_id,
            idle_seconds=idle_seconds,
            backoff_seconds=backoff,
            status_type=status_type,
            attempts=reconnect_attempts,
        )
        if part_handler is not None:
            await part_handler(
                "status",
                {
                    "type": "reconnecting",
                    "idleSeconds": idle_seconds,
                    "backoffSeconds": backoff,
                    "attempts": reconnect_attempts,
                    "maxAttempts": _OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS,
                    "stalledSeconds": stalled_elapsed_seconds,
                    "maxStalledSeconds": _OPENCODE_STREAM_MAX_STALL_RECONNECT_SECONDS,
                },
                None,
            )
        await _close_stream(stream_iter)
        await asyncio.sleep(backoff)
        stream_iter = _new_stream().__aiter__()
        last_relevant_event_at = now
        return True

    async def _handle_stall_recovery(*, now: float) -> tuple[bool, bool]:
        """Poll session and attempt reconnect on stall.

        Returns (should_break, should_continue).
        """
        nonlocal error
        idle_seconds = now - last_relevant_event_at
        status_type = None
        if session_fetcher is not None:
            try:
                fetched = await session_fetcher()
                status_type = _extract_status_type(fetched)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "opencode.session.poll_failed",
                    session_id=session_id,
                    exc=exc,
                )
        if _status_is_idle(status_type):
            log_event(
                logger,
                logging.INFO,
                "opencode.stream.stalled.session_idle",
                session_id=session_id,
                status_type=status_type,
                idle_seconds=idle_seconds,
            )
            if not text_parts and (pending_text or pending_no_id):
                _flush_all_pending_text()
            return (True, False)
        if last_primary_completion_at is not None:
            log_event(
                logger,
                logging.INFO,
                "opencode.stream.stalled.after_completion",
                session_id=session_id,
                status_type=status_type,
                idle_seconds=idle_seconds,
            )
        reconnected = await _attempt_reconnect(
            now=now,
            idle_seconds=idle_seconds,
            status_type=status_type,
        )
        if not reconnected:
            if status_type and not _status_is_idle(status_type):
                error = None
                while True:
                    await asyncio.sleep(5.0)
                    if session_fetcher is not None:
                        try:
                            fetched = await session_fetcher()
                            status_type = _extract_status_type(fetched)
                        except (ConnectionError, OSError, TimeoutError):
                            logger.debug(
                                "session fetch during stall recovery failed",
                                exc_info=True,
                            )
                    if _status_is_idle(status_type):
                        break
                return (True, False)
            return (True, False)
        return (False, True)

    try:
        while True:
            if should_stop is not None and should_stop():
                break
            try:
                wait_timeout: Optional[float] = None
                if first_event_timeout_seconds is not None and not received_any_event:
                    wait_timeout = max(
                        0.0,
                        stream_started_at
                        + first_event_timeout_seconds
                        - time.monotonic(),
                    )
                if (
                    can_reconnect
                    and stall_timeout_seconds is not None
                    and (received_any_event or first_event_timeout_seconds is None)
                ):
                    wait_timeout = stall_timeout_seconds
                if post_completion_deadline is not None:
                    remaining_completion_seconds = max(
                        0.0,
                        post_completion_deadline - time.monotonic(),
                    )
                    if wait_timeout is None:
                        wait_timeout = remaining_completion_seconds
                    else:
                        wait_timeout = min(
                            wait_timeout,
                            remaining_completion_seconds,
                        )
                if wait_timeout is None and received_any_event:
                    wait_timeout = _OPENCODE_ABSOLUTE_MAX_IDLE_SECONDS
                if wait_timeout is not None:
                    event = await asyncio.wait_for(
                        stream_iter.__anext__(), timeout=wait_timeout
                    )
                else:
                    event = await stream_iter.__anext__()
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                now = time.monotonic()
                if (
                    post_completion_deadline is not None
                    and now >= post_completion_deadline
                ):
                    log_event(
                        logger,
                        logging.INFO,
                        "opencode.stream.completed.grace_elapsed",
                        session_id=session_id,
                        grace_seconds=_OPENCODE_POST_COMPLETION_GRACE_SECONDS,
                        idle_seconds=now - last_relevant_event_at,
                    )
                    if not text_parts and (pending_text or pending_no_id):
                        _flush_all_pending_text()
                    break
                if not received_any_event and first_event_timeout_seconds is not None:
                    if now - stream_started_at >= first_event_timeout_seconds:
                        status_type = None
                        if session_fetcher is not None:
                            try:
                                fetched = await session_fetcher()
                                status_type = _extract_status_type(fetched)
                            except (ConnectionError, OSError, TimeoutError):
                                logger.debug(
                                    "session fetch during first-event timeout check failed",
                                    exc_info=True,
                                )

                        if status_type and not _status_is_idle(status_type):
                            (
                                should_break,
                                should_continue,
                            ) = await _handle_stall_recovery(now=now)
                            if should_break:
                                break
                            if should_continue:
                                continue
                        else:
                            await _fail_first_event_timeout(now=now)
                            break
                    continue
                should_break, should_continue = await _handle_stall_recovery(now=now)
                if should_break:
                    break
                if should_continue:
                    continue
            now = time.monotonic()
            raw = event.data or ""
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            event_session_id = extract_session_id(payload)
            is_relevant = opencode_event_is_progress_signal(
                event,
                session_id=session_id,
                progress_session_ids=progress_session_ids,
            )
            if not is_relevant:
                if not received_any_event and first_event_timeout_seconds is not None:
                    if now - stream_started_at >= first_event_timeout_seconds:
                        status_type = None
                        if session_fetcher is not None:
                            try:
                                fetched = await session_fetcher()
                                status_type = _extract_status_type(fetched)
                            except (ConnectionError, OSError, TimeoutError):
                                logger.debug(
                                    "session fetch during irrelevant-event timeout check failed",
                                    exc_info=True,
                                )

                        if status_type and not _status_is_idle(status_type):
                            (
                                should_break,
                                should_continue,
                            ) = await _handle_stall_recovery(now=now)
                            if should_break:
                                break
                            if should_continue:
                                continue
                        else:
                            await _fail_first_event_timeout(now=now)
                            break
                    continue
                if (
                    stall_timeout_seconds is not None
                    and now - last_relevant_event_at > stall_timeout_seconds
                ):
                    should_break, should_continue = await _handle_stall_recovery(
                        now=now
                    )
                    if should_break:
                        break
                    if should_continue:
                        continue
                continue
            last_relevant_event_at = now
            received_any_event = True
            reconnect_attempts = 0
            reconnect_started_at = None
            is_primary_session = event_session_id == session_id or not event_session_id
            if event.event == "question.asked":
                request_id, props = _extract_question_request(payload)
                questions = props.get("questions") if isinstance(props, dict) else []
                question_count = len(questions) if isinstance(questions, list) else 0
                log_event(
                    logger,
                    logging.INFO,
                    "opencode.question.asked",
                    request_id=request_id,
                    question_count=question_count,
                    session_id=event_session_id,
                )
                if not request_id:
                    continue
                dedupe_key = (event_session_id, request_id)
                if dedupe_key in seen_question_request_ids:
                    continue
                seen_question_request_ids.add(dedupe_key)
                if question_handler is not None:
                    try:
                        answers = await question_handler(request_id, props)
                    except Exception as exc:  # intentional: pluggable callback handler
                        log_event(
                            logger,
                            logging.WARNING,
                            "opencode.question.auto_reply_failed",
                            request_id=request_id,
                            session_id=event_session_id,
                            exc=exc,
                        )
                        if reject_question is not None:
                            try:
                                await reject_question(request_id)
                            except (OSError, RuntimeError, ValueError):
                                logger.debug(
                                    "reject_question after auto_reply_failed failed",
                                    exc_info=True,
                                )
                        continue
                    if answers is None:
                        if reject_question is not None:
                            try:
                                await reject_question(request_id)
                            except (OSError, RuntimeError, ValueError):
                                logger.debug(
                                    "reject_question for null answers failed",
                                    exc_info=True,
                                )
                        continue
                    normalized_answers = _normalize_question_answers(
                        answers, question_count=question_count
                    )
                    if reply_question is not None:
                        try:
                            await reply_question(request_id, normalized_answers)
                            log_event(
                                logger,
                                logging.INFO,
                                "opencode.question.replied",
                                request_id=request_id,
                                question_count=question_count,
                                session_id=event_session_id,
                                mode="handler",
                            )
                        except (OSError, RuntimeError, ValueError) as exc:
                            log_event(
                                logger,
                                logging.WARNING,
                                "opencode.question.auto_reply_failed",
                                request_id=request_id,
                                session_id=event_session_id,
                                exc=exc,
                            )
                    continue
                if normalized_question_policy == "ignore":
                    continue
                if normalized_question_policy == "reject":
                    if reject_question is not None:
                        try:
                            await reject_question(request_id)
                        except (OSError, RuntimeError, ValueError) as exc:
                            log_event(
                                logger,
                                logging.WARNING,
                                "opencode.question.auto_reply_failed",
                                request_id=request_id,
                                session_id=event_session_id,
                                exc=exc,
                            )
                    continue
                auto_answers = _auto_answers_for_questions(
                    questions if isinstance(questions, list) else [],
                    normalized_question_policy,
                )
                normalized_answers = _normalize_question_answers(
                    auto_answers, question_count=question_count
                )
                if reply_question is not None:
                    try:
                        await reply_question(request_id, normalized_answers)
                        log_event(
                            logger,
                            logging.INFO,
                            "opencode.question.auto_replied",
                            request_id=request_id,
                            question_count=question_count,
                            session_id=event_session_id,
                            policy=normalized_question_policy,
                            answers=_summarize_question_answers(normalized_answers),
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        log_event(
                            logger,
                            logging.WARNING,
                            "opencode.question.auto_reply_failed",
                            request_id=request_id,
                            session_id=event_session_id,
                            exc=exc,
                        )
                continue
            if event.event == "permission.asked":
                request_id, props = _extract_permission_request(payload)
                if request_id and respond_permission is not None:
                    if (
                        permission_policy == PERMISSION_ASK
                        and permission_handler is not None
                    ):
                        try:
                            decision = await permission_handler(request_id, props)
                        except Exception:  # intentional: pluggable callback handler
                            decision = OPENCODE_PERMISSION_REJECT
                        reply = _normalize_permission_decision(decision)
                    else:
                        reply = _permission_policy_reply(permission_policy)
                    try:
                        await respond_permission(request_id, reply)
                    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
                        status_code = None
                        body_preview = None
                        if isinstance(exc, httpx.HTTPStatusError):
                            status_code = exc.response.status_code
                            body_preview = (exc.response.text or "").strip()[
                                :200
                            ] or None
                            if (
                                status_code is not None
                                and 400 <= status_code < 500
                                and request_id not in logged_permission_errors
                            ):
                                logged_permission_errors.add(request_id)
                                log_event(
                                    logger,
                                    logging.ERROR,
                                    "opencode.permission.reply_failed",
                                    request_id=request_id,
                                    reply=reply,
                                    status_code=status_code,
                                    body_preview=body_preview,
                                    session_id=event_session_id,
                                )
                        else:
                            log_event(
                                logger,
                                logging.ERROR,
                                "opencode.permission.reply_failed",
                                request_id=request_id,
                                reply=reply,
                                session_id=event_session_id,
                                exc=exc,
                            )
                        if is_primary_session:
                            detail = body_preview or _extract_error_text(payload)
                            error = "OpenCode permission reply failed"
                            if status_code is not None:
                                error = f"{error} ({status_code})"
                            if detail:
                                error = f"{error}: {detail}"
                            break
            if event.event == "session.error":
                error_text = _extract_error_text(payload) or "OpenCode session error"
                log_event(
                    logger,
                    logging.ERROR,
                    "opencode.session.error",
                    session_id=session_id,
                    event_session_id=event_session_id,
                    error=error_text,
                    is_primary=is_primary_session,
                )
                if is_primary_session:
                    error = error_text
                    break
                continue
            if event.event in ("message.updated", "message.completed"):
                if is_primary_session:
                    msg_id, role = _register_message_role(payload)
                    _handle_role_update(msg_id, role)
            if event.event in ("message.part.updated", "message.part.delta"):
                properties = (
                    payload.get("properties") if isinstance(payload, dict) else None
                )
                if isinstance(properties, dict):
                    part = properties.get("part")
                    delta = properties.get("delta")
                else:
                    part = payload.get("part")
                    delta = payload.get("delta")
                part_dict = part if isinstance(part, dict) else None
                part_with_session = None
                if isinstance(part_dict, dict):
                    part_with_session = dict(part_dict)
                    part_with_session["sessionID"] = event_session_id
                part_type = part_dict.get("type") if part_dict else None
                part_ignored = bool(part_dict.get("ignored")) if part_dict else False
                part_message_id = extract_event_part_message_id(payload)
                part_id = extract_event_part_id(payload)
                if (
                    isinstance(part_id, str)
                    and part_id
                    and isinstance(part_type, str)
                    and part_type
                ):
                    part_types[part_id] = part_type
                elif (
                    isinstance(part_id, str)
                    and part_id
                    and not isinstance(part_type, str)
                    and part_id in part_types
                ):
                    part_type = part_types[part_id]
                if part_with_session is None and (
                    isinstance(part_id, str)
                    or isinstance(part_message_id, str)
                    or isinstance(part_type, str)
                ):
                    part_with_session = {"sessionID": event_session_id}
                    if isinstance(part_id, str) and part_id:
                        part_with_session["id"] = part_id
                    if isinstance(part_message_id, str) and part_message_id:
                        part_with_session["messageID"] = part_message_id
                    if isinstance(part_type, str) and part_type:
                        part_with_session["type"] = part_type
                if isinstance(delta, dict):
                    delta_text = delta.get("text")
                elif isinstance(delta, str):
                    delta_text = delta
                else:
                    delta_text = None
                if isinstance(delta_text, str) and delta_text:
                    if part_type == "reasoning":
                        if part_handler and part_with_session:
                            await part_handler(
                                "reasoning", part_with_session, delta_text
                            )
                    elif part_type in (None, "text") and not part_ignored:
                        if not is_primary_session:
                            continue
                        _append_text_for_message(part_message_id, delta_text)
                        # Update dedupe bookkeeping for text deltas to prevent re-adding later
                        if isinstance(part_id, str) and part_id:
                            if isinstance(part_dict, dict):
                                text = part_dict.get("text")
                                if isinstance(text, str):
                                    part_lengths[part_id] = len(text)
                                else:
                                    part_lengths[part_id] = part_lengths.get(
                                        part_id, 0
                                    ) + len(delta_text)
                            else:
                                part_lengths[part_id] = part_lengths.get(
                                    part_id, 0
                                ) + len(delta_text)
                        elif isinstance(part_dict, dict):
                            text = part_dict.get("text")
                            if isinstance(text, str):
                                last_full_text = text
                        if part_handler and part_with_session:
                            await part_handler("text", part_with_session, delta_text)
                    elif part_handler and part_with_session and part_type:
                        await part_handler(part_type, part_with_session, delta_text)
                elif (
                    isinstance(part_dict, dict)
                    and part_type in (None, "text")
                    and not part_ignored
                ):
                    if not is_primary_session:
                        continue
                    text = part_dict.get("text")
                    if isinstance(text, str) and text:
                        part_id = part_dict.get("id") or part_dict.get("partId")
                        if isinstance(part_id, str) and part_id:
                            last_len = part_lengths.get(part_id, 0)
                            if len(text) > last_len:
                                _append_text_for_message(
                                    part_message_id, text[last_len:]
                                )
                                part_lengths[part_id] = len(text)
                        else:
                            if last_full_text and text.startswith(last_full_text):
                                _append_text_for_message(
                                    part_message_id, text[len(last_full_text) :]
                                )
                            elif text != last_full_text:
                                _append_text_for_message(part_message_id, text)
                            last_full_text = text
                elif part_handler and part_with_session and part_type:
                    await part_handler(part_type, part_with_session, None)
                if part_type != "usage":
                    await _emit_usage_update(
                        payload, is_primary_session=is_primary_session
                    )
            message_role: Optional[str] = None
            if event.event in ("message.completed", "message.updated"):
                message_result = parse_message_response(payload)
                msg_id = None
                role = None
                if is_primary_session:
                    msg_id, role = _register_message_role(payload)
                    resolved_role = role
                    if resolved_role is None and msg_id:
                        resolved_role = message_roles.get(msg_id)
                    message_role = resolved_role
                    if message_result.text:
                        if resolved_role == "assistant" or resolved_role is None:
                            fallback_message = (
                                msg_id,
                                resolved_role,
                                message_result.text,
                            )
                            if resolved_role is None:
                                log_event(
                                    logger,
                                    logging.DEBUG,
                                    "opencode.message.completed.role_missing",
                                    session_id=event_session_id,
                                    message_id=msg_id,
                                )
                        else:
                            log_event(
                                logger,
                                logging.DEBUG,
                                "opencode.message.completed.ignored",
                                session_id=event_session_id,
                                message_id=msg_id,
                                role=resolved_role,
                            )
                    if message_result.error and not error:
                        error = message_result.error
                await _emit_usage_update(payload, is_primary_session=is_primary_session)
            if (
                event.event == "message.completed"
                and is_primary_session
                and message_role == "assistant"
                and _extract_message_phase(payload) != "commentary"
            ):
                last_primary_completion_at = time.monotonic()
                last_completed_assistant_text = (
                    message_result.text if message_result.text else None
                )
                post_completion_deadline = last_primary_completion_at + max(
                    _OPENCODE_POST_COMPLETION_GRACE_SECONDS, 0.0
                )
            if event.event == "session.idle" or (
                event.event == "session.status"
                and _status_is_idle(_extract_status_type(payload))
            ):
                if event_session_id != session_id:
                    continue
                if not text_parts and (pending_text or pending_no_id):
                    _flush_all_pending_text()
                break
            if (
                post_completion_deadline is not None
                and time.monotonic() >= post_completion_deadline
            ):
                log_event(
                    logger,
                    logging.INFO,
                    "opencode.stream.completed.grace_elapsed",
                    session_id=session_id,
                    grace_seconds=_OPENCODE_POST_COMPLETION_GRACE_SECONDS,
                    idle_seconds=time.monotonic() - last_relevant_event_at,
                )
                if not text_parts and (pending_text or pending_no_id):
                    _flush_all_pending_text()
                break
    finally:
        await _close_stream(stream_iter)

    if not text_parts and fallback_message is not None:
        msg_id, role, text = fallback_message
        resolved_role = role
        if resolved_role is None and msg_id:
            resolved_role = message_roles.get(msg_id)
        if resolved_role == "assistant" or (
            resolved_role is None
            and text
            and (prompt is None or text.strip() != prompt.strip())
        ):
            text_parts.append(text)

    if not text_parts and messages_fetcher is not None:
        try:
            messages_payload = await messages_fetcher()
        except (httpx.HTTPError, ValueError, OSError, AttributeError) as exc:
            log_event(
                logger,
                logging.DEBUG,
                "opencode.messages.fetch_failed",
                session_id=session_id,
                exc=exc,
            )
        else:
            recovered = recover_last_assistant_message(
                messages_payload,
                prompt=prompt,
            )
            if recovered.text:
                text_parts.append(recovered.text)
            if recovered.error and not error:
                error = recovered.error

    final_text = last_completed_assistant_text or "".join(text_parts)
    return OpenCodeTurnOutput(
        text=final_text.strip(),
        error=error,
        usage=latest_usage_snapshot,
    )


async def collect_opencode_output(
    client: Any,
    *,
    session_id: str,
    workspace_path: str,
    prompt: Optional[str] = None,
    model_payload: Optional[dict[str, str]] = None,
    progress_session_ids: Optional[set[str]] = None,
    permission_policy: str = PERMISSION_ALLOW,
    permission_handler: Optional[PermissionHandler] = None,
    question_policy: str = "ignore",
    question_handler: Optional[QuestionHandler] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    ready_event: Optional[Any] = None,
    part_handler: Optional[PartHandler] = None,
    stall_timeout_seconds: Optional[float] = _OPENCODE_STREAM_STALL_TIMEOUT_SECONDS,
    first_event_timeout_seconds: Optional[
        float
    ] = _OPENCODE_FIRST_EVENT_TIMEOUT_SECONDS,
    logger: Optional[logging.Logger] = None,
) -> OpenCodeTurnOutput:
    async def _respond(request_id: str, reply: str) -> None:
        await client.respond_permission(request_id=request_id, reply=reply)

    async def _reply_question(request_id: str, answers: list[list[str]]) -> None:
        await client.reply_question(request_id, answers=answers)

    async def _reject_question(request_id: str) -> None:
        await client.reject_question(request_id)

    def _stream_factory() -> AsyncIterator[SSEEvent]:
        return cast(
            AsyncIterator[SSEEvent],
            client.stream_events(
                directory=workspace_path,
                ready_event=ready_event,
                session_id=session_id,
            ),
        )

    async def _fetch_session() -> Any:
        statuses = await client.session_status(directory=workspace_path)
        if isinstance(statuses, dict):
            session_status = statuses.get(session_id)
            if session_status is None:
                return {"status": {"type": "idle"}}
            if isinstance(session_status, dict):
                return {"status": session_status}
            if isinstance(session_status, str):
                return {"status": session_status}
        return {"status": {}}

    async def _fetch_providers() -> Any:
        return await client.providers(directory=workspace_path)

    async def _fetch_messages() -> Any:
        return await client.list_messages(session_id, limit=10)

    return await collect_opencode_output_from_events(
        None,
        session_id=session_id,
        prompt=prompt,
        progress_session_ids=progress_session_ids,
        permission_policy=permission_policy,
        permission_handler=permission_handler,
        question_policy=question_policy,
        question_handler=question_handler,
        should_stop=should_stop,
        respond_permission=_respond,
        reply_question=_reply_question,
        reject_question=_reject_question,
        part_handler=part_handler,
        event_stream_factory=_stream_factory,
        model_payload=model_payload,
        session_fetcher=_fetch_session,
        provider_fetcher=_fetch_providers,
        messages_fetcher=_fetch_messages,
        stall_timeout_seconds=stall_timeout_seconds,
        first_event_timeout_seconds=first_event_timeout_seconds,
        logger=logger,
    )


__all__ = [
    "PERMISSION_ALLOW",
    "PERMISSION_ASK",
    "PERMISSION_DENY",
    "OpenCodeMessageResult",
    "OpenCodeTurnOutput",
    "PartHandler",
    "QuestionHandler",
    "build_turn_id",
    "collect_opencode_output",
    "collect_opencode_output_from_events",
    "extract_session_id",
    "extract_turn_id",
    "format_permission_prompt",
    "map_approval_policy_to_permission",
    "opencode_missing_env",
    "opencode_stream_timeouts",
    "parse_message_response",
    "recover_last_assistant_message",
    "split_model_id",
]
