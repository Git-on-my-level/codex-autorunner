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

from ...core.logging_utils import log_event
from ...core.sse import SSEEvent
from ...core.utils import resolve_opencode_auth_path
from .event_fields import (
    extract_part_id as extract_event_part_id,
)
from .event_fields import (
    extract_part_message_id as extract_event_part_message_id,
)
from .output_assembly import OutputAssembler
from .protocol_payload import (
    OPENCODE_PERMISSION_REJECT,
    PERMISSION_ALLOW,
    PERMISSION_ASK,
    PERMISSION_DENY,
    OpenCodeMessageResult,
    auto_answers_for_questions,
    build_turn_id,
    extract_delta_text_value,
    extract_error_text,
    extract_message_phase,
    extract_part_and_delta,
    extract_permission_request,
    extract_question_request,
    extract_session_id,
    extract_status_type,
    extract_turn_id,
    format_permission_prompt,
    map_approval_policy_to_permission,
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
from .stream_lifecycle import (
    _OPENCODE_FIRST_EVENT_TIMEOUT_SECONDS,
    _OPENCODE_STREAM_STALL_TIMEOUT_SECONDS,
    LifecycleAction,
    StreamLifecycleController,
)

PermissionDecision = str
PermissionHandler = Callable[[str, dict[str, Any]], Awaitable[PermissionDecision]]
QuestionHandler = Callable[[str, dict[str, Any]], Awaitable[Optional[list[list[str]]]]]
PartHandler = Callable[[str, dict[str, Any], Optional[str]], Awaitable[None]]


@dataclass(frozen=True)
class OpenCodeTurnOutput:
    text: str
    error: Optional[str] = None
    usage: Optional[dict[str, Any]] = None


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
        return status_is_idle(extract_status_type(payload))

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
    prompt_response_awaitable: Optional[Awaitable[Any]] = None,
    logger: Optional[logging.Logger] = None,
) -> OpenCodeTurnOutput:
    seen_question_request_ids: set[tuple[Optional[str], str]] = set()
    logged_permission_errors: set[str] = set()
    normalized_question_policy = normalize_question_policy(question_policy)
    if logger is None:
        logger = logging.getLogger(__name__)

    assembler = OutputAssembler(
        session_id=session_id,
        prompt=prompt,
        model_payload=model_payload,
        session_fetcher=session_fetcher,
        provider_fetcher=provider_fetcher,
        messages_fetcher=messages_fetcher,
        part_handler=part_handler,
        logger=logger,
    )

    lifecycle = StreamLifecycleController(
        session_id=session_id,
        event_stream_factory=event_stream_factory,
        session_fetcher=session_fetcher,
        stall_timeout_seconds=stall_timeout_seconds,
        first_event_timeout_seconds=first_event_timeout_seconds,
        status_event_handler=part_handler,
        logger=logger,
    )

    if events is None and event_stream_factory is None:
        raise ValueError("events or event_stream_factory must be provided")

    if event_stream_factory is not None:
        lifecycle.set_stream_iterator(event_stream_factory().__aiter__())
    elif events is not None:
        lifecycle.set_stream_iterator(events.__aiter__())
    else:
        raise ValueError("events or event_stream_factory must be provided")

    try:
        while True:
            if should_stop is not None and should_stop():
                break
            try:
                now = time.monotonic()
                wait_timeout = lifecycle.compute_wait_timeout(now=now)
                _stream_iter = lifecycle.stream_iterator
                assert _stream_iter is not None
                if wait_timeout is not None:
                    event = await asyncio.wait_for(
                        _stream_iter.__anext__(),
                        timeout=wait_timeout,
                    )
                else:
                    event = await _stream_iter.__anext__()
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                now = time.monotonic()
                decision = await lifecycle.on_timeout_error(now=now)
                if (
                    decision.should_flush_if_pending
                    and not assembler.has_text
                    and assembler.has_pending_text
                ):
                    assembler.flush_pending()
                if decision.error is not None:
                    assembler.error = decision.error
                if decision.action == LifecycleAction.BREAK:
                    break
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
                decision = await lifecycle.on_irrelevant_event(now=now)
                if (
                    decision.should_flush_if_pending
                    and not assembler.has_text
                    and assembler.has_pending_text
                ):
                    assembler.flush_pending()
                if decision.error is not None:
                    assembler.error = decision.error
                if decision.action == LifecycleAction.BREAK:
                    break
                continue
            lifecycle.on_relevant_event(now=now)
            is_primary_session = event_session_id == session_id or not event_session_id
            if event.event == "question.asked":
                request_id, props = extract_question_request(payload)
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
                    normalized_answers = normalize_question_answers(
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
                auto_answers = auto_answers_for_questions(
                    questions if isinstance(questions, list) else [],
                    normalized_question_policy,
                )
                normalized_answers = normalize_question_answers(
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
                            answers=summarize_question_answers(normalized_answers),
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
                request_id, props = extract_permission_request(payload)
                if request_id and respond_permission is not None:
                    if (
                        permission_policy == PERMISSION_ASK
                        and permission_handler is not None
                    ):
                        try:
                            perm_decision = await permission_handler(request_id, props)
                        except Exception:  # intentional: pluggable callback handler
                            perm_decision = OPENCODE_PERMISSION_REJECT
                        reply = normalize_permission_decision(perm_decision)
                    else:
                        reply = permission_policy_reply(permission_policy)
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
                            detail = body_preview or extract_error_text(payload)
                            perm_error = "OpenCode permission reply failed"
                            if status_code is not None:
                                perm_error = f"{perm_error} ({status_code})"
                            if detail:
                                perm_error = f"{perm_error}: {detail}"
                            assembler.error = perm_error
                            break
            if event.event == "session.error":
                error_text = extract_error_text(payload) or "OpenCode session error"
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
                    assembler.error = error_text
                    break
                continue
            if event.event in ("message.updated", "message.completed"):
                if is_primary_session:
                    msg_id, role = assembler.on_register_message_role(payload)
                    assembler.on_handle_role_update(msg_id, role)
            if event.event in ("message.part.updated", "message.part.delta"):
                part_dict, delta = extract_part_and_delta(payload)
                part_with_session = None
                if isinstance(part_dict, dict):
                    part_with_session = dict(part_dict)
                    part_with_session["sessionID"] = event_session_id
                part_type = part_dict.get("type") if part_dict else None
                part_ignored = bool(part_dict.get("ignored")) if part_dict else False
                part_message_id = extract_event_part_message_id(payload)
                part_id = extract_event_part_id(payload)
                assembler.remember_part_type(part_id, part_type)
                resolved_part_type = (
                    part_type
                    if isinstance(part_type, str)
                    else assembler.lookup_part_type(part_id)
                )
                if part_with_session is None and (
                    isinstance(part_id, str)
                    or isinstance(part_message_id, str)
                    or isinstance(resolved_part_type, str)
                ):
                    part_with_session = {"sessionID": event_session_id}
                    if isinstance(part_id, str) and part_id:
                        part_with_session["id"] = part_id
                    if isinstance(part_message_id, str) and part_message_id:
                        part_with_session["messageID"] = part_message_id
                    if isinstance(resolved_part_type, str) and resolved_part_type:
                        part_with_session["type"] = resolved_part_type
                delta_text = extract_delta_text_value(delta)
                if isinstance(delta_text, str) and delta_text:
                    if resolved_part_type == "reasoning":
                        if part_handler and part_with_session:
                            await part_handler(
                                "reasoning", part_with_session, delta_text
                            )
                    elif resolved_part_type in (None, "text") and not part_ignored:
                        if not is_primary_session:
                            continue
                        await assembler.on_text_delta(
                            part_message_id=part_message_id,
                            delta_text=delta_text,
                            part_id=part_id,
                            part_dict=part_dict,
                        )
                        if part_handler and part_with_session:
                            await part_handler("text", part_with_session, delta_text)
                    elif part_handler and part_with_session and resolved_part_type:
                        await part_handler(
                            resolved_part_type, part_with_session, delta_text
                        )
                elif (
                    isinstance(part_dict, dict)
                    and resolved_part_type in (None, "text")
                    and not part_ignored
                ):
                    if not is_primary_session:
                        continue
                    await assembler.on_full_text_part(
                        part_message_id=part_message_id,
                        part_dict=part_dict,
                    )
                elif part_handler and part_with_session and resolved_part_type:
                    await part_handler(resolved_part_type, part_with_session, None)
                if resolved_part_type != "usage":
                    await assembler.emit_usage_update(
                        payload, is_primary_session=is_primary_session
                    )
            message_role: Optional[str] = None
            if event.event in ("message.completed", "message.updated"):
                message_role = assembler.on_message_completed(
                    payload,
                    is_primary_session=is_primary_session,
                    event_session_id=event_session_id,
                )
                await assembler.emit_usage_update(
                    payload, is_primary_session=is_primary_session
                )
            if event.event == "message.completed" and is_primary_session:
                assembler.on_primary_assistant_completion(payload, message_role)
                if (
                    message_role == "assistant"
                    and extract_message_phase(payload) != "commentary"
                ):
                    lifecycle.on_primary_completion()
            if event.event == "session.idle" or (
                event.event == "session.status"
                and status_is_idle(extract_status_type(payload))
            ):
                if event_session_id != session_id:
                    continue
                if not assembler.has_text and assembler.has_pending_text:
                    assembler.flush_pending()
                break
            grace_decision = lifecycle.check_grace_elapsed()
            if grace_decision is not None:
                if (
                    grace_decision.should_flush_if_pending
                    and not assembler.has_text
                    and assembler.has_pending_text
                ):
                    assembler.flush_pending()
                if grace_decision.error is not None:
                    assembler.error = grace_decision.error
                break
    finally:
        await lifecycle.close()

    if prompt_response_awaitable is not None:
        try:
            prompt_response = await prompt_response_awaitable
            assembler.on_prompt_response(prompt_response)
        except Exception:
            pass

    result = await assembler.build_result()
    return OpenCodeTurnOutput(
        text=result.text,
        error=result.error,
        usage=result.usage,
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
    prompt_response_awaitable: Optional[Awaitable[Any]] = None,
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
        prompt_response_awaitable=prompt_response_awaitable,
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
