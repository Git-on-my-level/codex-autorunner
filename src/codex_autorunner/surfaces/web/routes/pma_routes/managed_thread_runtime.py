from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .....agents.base import (
    harness_progress_event_stream,
    harness_supports_progress_event_stream,
)
from .....agents.managed_runtime import sync_managed_workspace_compat_files
from .....core.car_context import (
    build_car_context_bundle,
    default_managed_thread_context_profile,
    normalize_car_context_profile,
    render_injected_car_context,
)
from .....core.chat_bindings import (
    DISCORD_STATE_FILE_DEFAULT,
    TELEGRAM_STATE_FILE_DEFAULT,
)
from .....core.config import PMA_DEFAULT_MAX_TEXT_CHARS
from .....core.orchestration import MessageRequest
from .....core.orchestration.bindings import OrchestrationBindingStore
from .....core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    merge_runtime_thread_raw_events,
    normalize_runtime_thread_raw_event,
)
from .....core.orchestration.runtime_threads import (
    RUNTIME_THREAD_INTERRUPTED_ERROR,
    RUNTIME_THREAD_TIMEOUT_ERROR,
    RuntimeThreadExecution,
    RuntimeThreadOutcome,
    await_runtime_thread_outcome,
    begin_next_queued_runtime_thread_execution,
    begin_runtime_thread_execution,
)
from .....core.orchestration.service import (
    LOST_BACKEND_THREAD_ERROR,
    MISSING_BACKEND_THREAD_ERROR,
    BusyInterruptFailedError,
)
from .....core.orchestration.turn_timeline import persist_turn_timeline
from .....core.pma_context import format_pma_discoverability_preamble
from .....core.pma_thread_store import (
    ManagedThreadAlreadyHasRunningTurnError,
    ManagedThreadNotActiveError,
    PmaThreadStore,
)
from .....core.pma_transcripts import PmaTranscriptStore
from .....core.ports.run_event import Completed, Failed, RunEvent
from .....core.time_utils import now_iso
from .....integrations.chat.approval_modes import resolve_approval_mode_policies
from .....integrations.discord.rendering import (
    chunk_discord_message,
    format_discord_message,
)
from .....integrations.discord.state import DiscordStateStore
from .....integrations.discord.state import OutboxRecord as DiscordOutboxRecord
from .....integrations.telegram.state import OutboxRecord as TelegramOutboxRecord
from .....integrations.telegram.state import TelegramStateStore, parse_topic_key
from ...schemas import PmaManagedThreadMessageRequest
from ...services.pma.managed_thread_followup import (
    ManagedThreadAutomationClient,
    ManagedThreadAutomationUnavailable,
)
from .automation_adapter import (
    first_callable,
    get_automation_store,
    normalize_optional_text,
)
from .managed_threads import (
    build_managed_thread_orchestration_service as _shared_managed_thread_orchestration_service,
)
from .publish import (
    PMA_DISCORD_MESSAGE_MAX_LEN,
    enqueue_with_retry,
    resolve_chat_state_path,
)

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

MANAGED_THREAD_PUBLIC_EXECUTION_ERROR = "Managed thread execution failed"
MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR = "Failed to interrupt backend turn"
MANAGED_THREAD_INTERRUPT_FAILED_DETAIL = (
    "Interrupt attempt failed; the active managed turn is still running"
)
PMA_TIMEOUT_SECONDS = 7200
PMA_MAX_TEXT = PMA_DEFAULT_MAX_TEXT_CHARS
BOUND_CHAT_SURFACE_KINDS = frozenset({"discord", "telegram"})


def _build_managed_thread_orchestration_service(
    request: Request, *, thread_store: Optional[PmaThreadStore] = None
):
    _ = thread_store
    return _shared_managed_thread_orchestration_service(request)


def _truncate_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    s = str(value)
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _resolve_managed_thread_policies(
    thread: dict[str, Any],
) -> tuple[Optional[str], Optional[Any]]:
    metadata = thread.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return resolve_approval_mode_policies(
        normalize_optional_text(
            thread.get("approval_mode") or metadata.get("approval_mode")
        ),
        default_approval_policy="never",
        default_sandbox_policy="dangerFullAccess",
        override_approval_policy=normalize_optional_text(
            thread.get("approval_policy") or metadata.get("approval_policy")
        ),
        override_sandbox_policy=(
            thread.get("sandbox_policy")
            if thread.get("sandbox_policy") is not None
            else metadata.get("sandbox_policy")
        ),
    )


def _compose_compacted_prompt(compact_seed: str, message: str) -> str:
    return (
        "Context summary (from compaction):\n"
        f"{compact_seed}\n\n"
        "User message:\n"
        f"{message}"
    )


def _compose_execution_prompt(
    *,
    agent: Any,
    hub_root: Path,
    stored_backend_id: Optional[str],
    compact_seed: Optional[str],
    message: str,
    context_profile: Any,
) -> str:
    execution_message = message
    if not stored_backend_id and compact_seed:
        execution_message = _compose_compacted_prompt(compact_seed, message)

    # ZeroClaw persists user turns into durable session history via
    # `--session-state-file`, so PMA bootstrap/docs must not be wrapped into
    # the conversational turn payload.
    if str(agent or "").strip().lower() == "zeroclaw":
        return execution_message

    preamble = format_pma_discoverability_preamble(hub_root=hub_root)
    user_message = "<user_message>\n" f"{execution_message}\n" "</user_message>\n"
    bundle = build_car_context_bundle(
        context_profile,
        prompt_text=message,
    )
    car_context = render_injected_car_context(bundle)
    if not car_context:
        return f"{preamble}{user_message}"
    return f"{preamble}{car_context}\n\n{user_message}"


def _sanitize_managed_thread_result_error(detail: Any) -> str:
    sanitized = normalize_optional_text(detail)
    if sanitized in {RUNTIME_THREAD_TIMEOUT_ERROR, "PMA chat timed out"}:
        return "PMA chat timed out"
    if sanitized in {RUNTIME_THREAD_INTERRUPTED_ERROR, "PMA chat interrupted"}:
        return "PMA chat interrupted"
    if sanitized in {"PMA chat timed out", "PMA chat interrupted"}:
        return sanitized
    return MANAGED_THREAD_PUBLIC_EXECUTION_ERROR


def _normalize_busy_policy(value: Any) -> Literal["queue", "interrupt", "reject"]:
    normalized = normalize_optional_text(value)
    if normalized is None:
        return "queue"
    busy_policy = normalized.lower()
    if busy_policy not in {"queue", "interrupt", "reject"}:
        raise HTTPException(
            status_code=400,
            detail="busy_policy must be one of: queue, interrupt, reject",
        )
    return cast(Literal["queue", "interrupt", "reject"], busy_policy)


def _interrupt_failure_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: Optional[str],
    backend_thread_id: str,
    detail: str,
    delivery_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "error",
        "send_state": "rejected",
        "interrupt_state": "failed",
        "execution_state": "running",
        "reason": "interrupt_failed",
        "detail": detail,
        "next_step": (
            "Wait for the active turn to finish, inspect thread status, "
            "or retry the interrupt after checking runtime health."
        ),
        "active_turn_status": "running",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": None,
        "active_managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": detail,
        **delivery_payload,
    }


def _interrupt_recovered_lost_backend_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    updated_turn: Optional[dict[str, Any]],
    backend_error: str,
    backend_interrupt_attempted: bool,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "interrupt_state": "recovered_lost_backend",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "turn": updated_turn,
        "backend_error": backend_error,
        "detail": "Recovered stale managed-thread state after backend thread was lost.",
        "backend_interrupt_attempted": backend_interrupt_attempted,
        "recovered_lost_backend": True,
    }


async def _interrupt_managed_thread_via_orchestration(
    *,
    managed_thread_id: str,
    request: Request,
) -> dict[str, Any]:
    from .....agents.registry import get_available_agents
    from .....core.orchestration.catalog import map_agent_capabilities

    hub_root = request.app.state.config.root
    store = PmaThreadStore(hub_root)
    thread = store.get_thread(managed_thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Managed thread not found")

    agent = str(thread.get("agent") or "").strip().lower()
    if agent:
        available = get_available_agents(request.app.state)
        descriptor = available.get(agent)
        if descriptor is not None:
            capabilities = map_agent_capabilities(descriptor.capabilities)
            if "interrupt" not in capabilities:
                raise HTTPException(
                    status_code=403,
                    detail=f"Agent '{agent}' does not support interrupt (missing capability: interrupt)",
                )

    running_turn = store.get_running_turn(managed_thread_id)
    if running_turn is None:
        raise HTTPException(
            status_code=409, detail="Managed thread has no running turn"
        )
    managed_turn_id = str(running_turn.get("managed_turn_id") or "")
    if not managed_turn_id:
        raise HTTPException(status_code=500, detail="Running turn is missing id")

    backend_thread_id = normalize_optional_text(thread.get("backend_thread_id"))
    backend_turn_id = normalize_optional_text(running_turn.get("backend_turn_id"))
    backend_error: Optional[str] = None
    backend_interrupt_attempted = bool(backend_thread_id)
    service = _build_managed_thread_orchestration_service(
        request,
        thread_store=store,
    )
    try:
        stop_outcome = await service.stop_thread(managed_thread_id)
    except Exception:
        logger.exception(
            "Failed to interrupt managed-thread turn via orchestration service (managed_thread_id=%s, managed_turn_id=%s)",
            managed_thread_id,
            managed_turn_id,
        )
        interrupted_execution = service.get_execution(
            managed_thread_id,
            managed_turn_id,
        )
        stop_outcome = None
        if interrupted_execution is None or interrupted_execution.status == "running":
            backend_error = MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR
    else:
        interrupted_execution = stop_outcome.execution

    if stop_outcome and stop_outcome.interrupted_active:
        await notify_managed_thread_terminal_transition(
            request,
            thread=thread,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            to_state="interrupted",
            reason=backend_error or "managed_turn_interrupted",
        )
        store.append_action(
            "managed_thread_interrupt",
            managed_thread_id=managed_thread_id,
            payload_json=json.dumps(
                {
                    "agent": agent,
                    "managed_turn_id": managed_turn_id,
                    "backend_thread_id": backend_thread_id,
                    "backend_turn_id": backend_turn_id,
                    "backend_interrupt_attempted": backend_interrupt_attempted,
                    "backend_error": backend_error,
                },
                ensure_ascii=True,
            ),
        )
        updated_turn = store.get_turn(managed_thread_id, managed_turn_id)
        return {
            "status": "ok",
            "interrupt_state": "succeeded",
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "turn": updated_turn,
            "backend_error": backend_error,
        }

    if stop_outcome and stop_outcome.recovered_lost_backend:
        resolved_backend_error = (
            interrupted_execution.error
            if interrupted_execution is not None
            and interrupted_execution.error
            in {
                LOST_BACKEND_THREAD_ERROR,
                MISSING_BACKEND_THREAD_ERROR,
            }
            else backend_error or LOST_BACKEND_THREAD_ERROR
        )
        await notify_managed_thread_terminal_transition(
            request,
            thread=thread,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            to_state="failed",
            reason=resolved_backend_error,
        )
        store.append_action(
            "managed_thread_interrupt",
            managed_thread_id=managed_thread_id,
            payload_json=json.dumps(
                {
                    "agent": agent,
                    "managed_turn_id": managed_turn_id,
                    "backend_thread_id": backend_thread_id,
                    "backend_turn_id": backend_turn_id,
                    "backend_interrupt_attempted": backend_interrupt_attempted,
                    "backend_error": resolved_backend_error,
                    "recovered_lost_backend": True,
                },
                ensure_ascii=True,
            ),
        )
        updated_turn = store.get_turn(managed_thread_id, managed_turn_id)
        return _interrupt_recovered_lost_backend_payload(
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            updated_turn=updated_turn,
            backend_error=resolved_backend_error,
            backend_interrupt_attempted=backend_interrupt_attempted,
        )

    updated_turn = store.get_turn(managed_thread_id, managed_turn_id)
    return {
        "status": "error",
        "interrupt_state": "failed",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "turn": updated_turn,
        "backend_error": backend_error or MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR,
        "detail": MANAGED_THREAD_INTERRUPT_FAILED_DETAIL,
    }


def _build_managed_thread_turn_metadata(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    thread_row: dict[str, Any],
    backend_thread_id: str,
    backend_turn_id: Optional[str],
    workspace_root: Optional[Path],
    model: Optional[str],
    reasoning: Optional[str],
    status: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "repo_id": thread_row.get("repo_id"),
        "resource_kind": thread_row.get("resource_kind"),
        "resource_id": thread_row.get("resource_id"),
        "agent": thread_row.get("agent"),
        "backend_thread_id": backend_thread_id,
        "backend_turn_id": backend_turn_id,
        "model": model,
        "reasoning": reasoning,
        "status": status,
    }
    if workspace_root is not None:
        metadata["workspace_root"] = str(workspace_root)
    return metadata


def _persist_managed_thread_timeline(
    *,
    hub_root: Path,
    managed_thread_id: str,
    managed_turn_id: str,
    thread_row: dict[str, Any],
    metadata: dict[str, Any],
    events: list[RunEvent],
    log_status: str,
) -> None:
    try:
        persist_turn_timeline(
            hub_root,
            execution_id=managed_turn_id,
            target_kind="thread_target",
            target_id=managed_thread_id,
            repo_id=normalize_optional_text(thread_row.get("repo_id")),
            resource_kind=normalize_optional_text(thread_row.get("resource_kind")),
            resource_id=normalize_optional_text(thread_row.get("resource_id")),
            metadata=metadata,
            events=events,
        )
    except Exception:
        logger.exception(
            "Failed to persist %s managed-thread timeline (managed_thread_id=%s, managed_turn_id=%s)",
            log_status,
            managed_thread_id,
            managed_turn_id,
        )


async def _timeline_from_runtime_raw_events(
    raw_events: tuple[Any, ...],
) -> list[RunEvent]:
    state = RuntimeThreadRunEventState()
    timeline_events: list[RunEvent] = []
    for raw_event in raw_events:
        timeline_events.extend(
            await normalize_runtime_thread_raw_event(
                raw_event,
                state,
            )
        )
    return timeline_events


async def deliver_bound_chat_assistant_output(
    request: Request,
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    assistant_text: str,
) -> None:
    message = str(assistant_text or "").strip()
    if not message:
        return

    hub_root = request.app.state.config.root
    binding_store = OrchestrationBindingStore(hub_root)
    bindings = [
        binding
        for binding in binding_store.list_bindings(
            thread_target_id=managed_thread_id,
            include_disabled=False,
            limit=1000,
        )
        if binding.surface_kind in BOUND_CHAT_SURFACE_KINDS
    ]
    if not bindings:
        return

    discord_store: Optional[DiscordStateStore] = None
    telegram_store: Optional[TelegramStateStore] = None
    created_at = now_iso()
    try:
        for binding in bindings:
            try:
                if binding.surface_kind == "discord":
                    if discord_store is None:
                        discord_store = DiscordStateStore(
                            resolve_chat_state_path(
                                request,
                                section="discord_bot",
                                default_state_file=DISCORD_STATE_FILE_DEFAULT,
                            )
                        )
                    channel_id = normalize_optional_text(binding.surface_key)
                    if channel_id is None:
                        continue
                    chunks = chunk_discord_message(
                        format_discord_message(message),
                        max_len=PMA_DISCORD_MESSAGE_MAX_LEN,
                        with_numbering=False,
                    )
                    if not chunks:
                        chunks = [format_discord_message(message)]
                    for index, chunk in enumerate(chunks, start=1):
                        digest = hashlib.sha256(
                            (
                                f"managed-thread:{managed_turn_id}:discord:{channel_id}:{index}"
                            ).encode("utf-8")
                        ).hexdigest()[:24]
                        record_id = f"managed-thread:{digest}"
                        if await discord_store.get_outbox(record_id) is not None:
                            continue
                        record = DiscordOutboxRecord(
                            record_id=record_id,
                            channel_id=channel_id,
                            message_id=None,
                            operation="send",
                            payload_json={"content": chunk},
                            created_at=created_at,
                        )
                        store = discord_store
                        await enqueue_with_retry(
                            lambda record=record, store=store: store.enqueue_outbox(
                                record
                            )
                        )
                    continue

                if binding.surface_kind != "telegram":
                    continue
                if telegram_store is None:
                    telegram_store = TelegramStateStore(
                        resolve_chat_state_path(
                            request,
                            section="telegram_bot",
                            default_state_file=TELEGRAM_STATE_FILE_DEFAULT,
                        )
                    )
                surface_key = normalize_optional_text(binding.surface_key)
                if surface_key is None:
                    continue
                try:
                    chat_id, thread_id, _scope = parse_topic_key(surface_key)
                except Exception:
                    logger.warning(
                        "Failed to parse telegram bound-chat surface key for managed-thread delivery: %s",
                        surface_key,
                    )
                    continue
                digest = hashlib.sha256(
                    (
                        f"managed-thread:{managed_turn_id}:telegram:{chat_id}:{thread_id or 'root'}"
                    ).encode("utf-8")
                ).hexdigest()[:24]
                record_id = f"managed-thread:{digest}"
                if await telegram_store.get_outbox(record_id) is not None:
                    continue
                outbox_key = f"managed-thread:{managed_turn_id}:{chat_id}:{thread_id or 'root'}:send"
                record = TelegramOutboxRecord(
                    record_id=record_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to_message_id=None,
                    placeholder_message_id=None,
                    text=message,
                    created_at=created_at,
                    operation="send",
                    message_id=None,
                    outbox_key=outbox_key,
                )
                store = telegram_store
                await enqueue_with_retry(
                    lambda record=record, store=store: store.enqueue_outbox(record)
                )
            except Exception:
                logger.exception(
                    "Failed to enqueue bound-chat delivery target (managed_thread_id=%s, managed_turn_id=%s, surface_kind=%s, surface_key=%s)",
                    managed_thread_id,
                    managed_turn_id,
                    binding.surface_kind,
                    binding.surface_key,
                )
    finally:
        if discord_store is not None:
            try:
                await discord_store.close()
            except Exception:
                logger.exception("Failed to close discord bound-chat delivery store")
        if telegram_store is not None:
            try:
                await telegram_store.close()
            except Exception:
                logger.exception("Failed to close telegram bound-chat delivery store")


async def notify_managed_thread_terminal_transition(
    request: Request,
    *,
    thread: dict[str, Any],
    managed_thread_id: str,
    managed_turn_id: str,
    to_state: str,
    reason: str,
) -> None:
    normalized_to_state = (to_state or "").strip().lower() or "failed"
    await _notify_hub_automation_transition(
        request,
        repo_id=normalize_optional_text(thread.get("repo_id")),
        resource_kind=normalize_optional_text(thread.get("resource_kind")),
        resource_id=normalize_optional_text(thread.get("resource_id")),
        run_id=None,
        thread_id=managed_thread_id,
        from_state="running",
        to_state=normalized_to_state,
        reason=reason,
        extra={
            "event_type": f"managed_thread_{normalized_to_state}",
            "transition_id": f"managed_turn:{managed_turn_id}:{normalized_to_state}",
            "idempotency_key": (
                f"managed_turn:{managed_turn_id}:{normalized_to_state}"
            ),
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "agent": normalize_optional_text(thread.get("agent")) or "",
        },
    )


async def _notify_hub_automation_transition(
    request: Request,
    *,
    repo_id: Optional[str] = None,
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
    run_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    from_state: str,
    to_state: str,
    reason: Optional[str] = None,
    timestamp: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    from .....core.time_utils import now_iso

    payload: dict[str, Any] = {
        "from_state": (from_state or "").strip(),
        "to_state": (to_state or "").strip(),
        "reason": normalize_optional_text(reason) or "",
        "timestamp": normalize_optional_text(timestamp) or now_iso(),
    }
    normalized_repo_id = normalize_optional_text(repo_id)
    normalized_resource_kind = normalize_optional_text(resource_kind)
    normalized_resource_id = normalize_optional_text(resource_id)
    normalized_run_id = normalize_optional_text(run_id)
    normalized_thread_id = normalize_optional_text(thread_id)
    if normalized_repo_id:
        payload["repo_id"] = normalized_repo_id
    if normalized_resource_kind:
        payload["resource_kind"] = normalized_resource_kind
    if normalized_resource_id:
        payload["resource_id"] = normalized_resource_id
    if normalized_run_id:
        payload["run_id"] = normalized_run_id
    if normalized_thread_id:
        payload["thread_id"] = normalized_thread_id
    if isinstance(extra, dict):
        payload.update(extra)

    supervisor = getattr(request.app.state, "hub_supervisor", None)
    store = await get_automation_store(request, None)
    if store is None:
        return

    method = first_callable(
        store,
        (
            "notify_transition",
            "record_transition",
            "handle_transition",
            "on_transition",
            "process_transition",
        ),
    )
    if method is None:
        return

    async def await_if_needed(value: Any) -> Any:
        if asyncio.iscoroutine(value):
            return await value
        return value

    async def call_with_fallbacks(
        method: Any, attempts: list[tuple[tuple[Any, ...], dict[str, Any]]]
    ) -> Any:
        last_type_error: Optional[TypeError] = None
        for args, kwargs in attempts:
            try:
                return await await_if_needed(method(*args, **kwargs))
            except TypeError as exc:
                last_type_error = exc
                continue
        if last_type_error is not None:
            raise last_type_error
        raise RuntimeError("No automation method call attempts were provided")

    try:
        await call_with_fallbacks(
            method,
            [
                ((dict(payload),), {}),
                ((), {"payload": dict(payload)}),
                ((), dict(payload)),
            ],
        )
    except Exception:
        logger.exception("Failed to notify hub automation transition")
        return

    process_now = (
        getattr(supervisor, "process_pma_automation_now", None)
        if supervisor is not None
        else None
    )
    if not callable(process_now):
        return
    try:
        await await_if_needed(process_now(include_timers=False))
    except TypeError:
        try:
            await await_if_needed(process_now())
        except Exception:
            logger.exception("Failed immediate PMA automation processing")
    except Exception:
        logger.exception("Failed immediate PMA automation processing")


def build_managed_thread_runtime_routes(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build managed-thread runtime routes (send message, interrupt)."""

    def _get_pma_config(request: Request) -> dict[str, Any]:
        raw = getattr(request.app.state.config, "raw", {})
        return _pma_config_from_raw(raw)

    def _pma_config_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "enabled": True,
            "max_text_chars": PMA_DEFAULT_MAX_TEXT_CHARS,
        }
        if not isinstance(raw, dict):
            return defaults
        pma = raw.get("pma")
        if not isinstance(pma, dict):
            return defaults
        return {**defaults, **pma}

    @router.post("/threads/{managed_thread_id}/messages")
    async def send_managed_thread_message(
        managed_thread_id: str,
        request: Request,
        payload: PmaManagedThreadMessageRequest,
    ) -> Any:
        busy_policy = _normalize_busy_policy(payload.busy_policy)

        message = payload.message or ""
        if not message.strip():
            raise HTTPException(status_code=400, detail="message is required")

        defaults = _get_pma_config(request)
        max_text_chars = int(defaults.get("max_text_chars", 0) or 0)
        if max_text_chars > 0 and len(message) > max_text_chars:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"message exceeds max_text_chars ({max_text_chars} characters)"
                ),
            )

        hub_root = request.app.state.config.root
        thread_store = PmaThreadStore(hub_root)
        transcripts = PmaTranscriptStore(hub_root)
        thread = thread_store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        notify_on = normalize_optional_text(payload.notify_on)
        if notify_on and notify_on != "terminal":
            raise HTTPException(
                status_code=400, detail="notify_on must be 'terminal' when provided"
            )
        notify_on = None if not notify_on else notify_on

        notify_lane = normalize_optional_text(payload.notify_lane)
        notify_once = bool(payload.notify_once)
        defer_execution = bool(payload.defer_execution)
        delivery_payload = {"delivered_message": message}

        if (thread.get("status") or "") == "archived":
            return JSONResponse(
                status_code=409,
                content={
                    "status": "error",
                    "send_state": "rejected",
                    "reason": "thread_archived",
                    "detail": "Managed thread is archived and read-only",
                    "next_step": "Use `car pma thread resume` or spawn a new thread.",
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": None,
                    "backend_thread_id": normalize_optional_text(
                        thread.get("backend_thread_id")
                    )
                    or "",
                    "assistant_text": "",
                    "error": "Managed thread is archived and read-only",
                },
            )
        model = normalize_optional_text(payload.model) or defaults.get("model")
        reasoning = normalize_optional_text(payload.reasoning) or defaults.get(
            "reasoning"
        )
        stored_backend_id = normalize_optional_text(thread.get("backend_thread_id"))
        compact_seed = normalize_optional_text(thread.get("compact_seed"))
        metadata = thread.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        context_profile = normalize_car_context_profile(
            thread.get("context_profile") or metadata.get("context_profile"),
            default=default_managed_thread_context_profile(
                resource_kind=thread.get("resource_kind")
            ),
        )
        approval_policy, sandbox_policy = _resolve_managed_thread_policies(thread)
        execution_prompt = _compose_execution_prompt(
            agent=thread.get("agent"),
            hub_root=hub_root,
            stored_backend_id=stored_backend_id,
            compact_seed=compact_seed,
            message=message,
            context_profile=context_profile,
        )
        if str(thread.get("agent") or "").strip().lower() == "zeroclaw":
            sync_managed_workspace_compat_files(
                "zeroclaw",
                runtime_workspace_root=Path(thread["workspace_root"]) / "workspace",
                bundle=build_car_context_bundle(
                    context_profile,
                    prompt_text=message,
                ),
            )
        service = _build_managed_thread_orchestration_service(
            request,
            thread_store=thread_store,
        )
        try:
            started_execution = await begin_runtime_thread_execution(
                service,
                MessageRequest(
                    target_id=managed_thread_id,
                    target_kind="thread",
                    message_text=message,
                    busy_policy=busy_policy,
                    model=model,
                    reasoning=reasoning,
                    approval_mode=approval_policy,
                    context_profile=context_profile,
                    metadata={
                        "runtime_prompt": execution_prompt,
                        "execution_error_message": (
                            MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
                        ),
                    },
                ),
                sandbox_policy=sandbox_policy,
            )
        except ManagedThreadNotActiveError as exc:
            if exc.status == "archived":
                detail = "Managed thread is archived and read-only"
            else:
                detail = "Managed thread is not active"
            return JSONResponse(
                status_code=409,
                content={
                    "status": "error",
                    "send_state": "rejected",
                    "reason": "thread_not_active",
                    "detail": detail,
                    "next_step": "Resume the thread or create a new active thread.",
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": None,
                    "backend_thread_id": stored_backend_id or "",
                    "assistant_text": "",
                    "error": detail,
                },
            )
        except ManagedThreadAlreadyHasRunningTurnError:
            running_turn = thread_store.get_running_turn(managed_thread_id)
            return JSONResponse(
                status_code=409,
                content={
                    "status": "error",
                    "send_state": "already_in_flight",
                    "reason": "running_turn_exists",
                    "detail": (
                        f"Managed thread {managed_thread_id} already has a running turn"
                    ),
                    "next_step": (
                        "Wait for the running turn to finish, use --watch, "
                        "or subscribe with --notify-on terminal."
                    ),
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": str(
                        (running_turn or {}).get("managed_turn_id") or ""
                    )
                    or None,
                    "backend_thread_id": stored_backend_id or "",
                    "assistant_text": "",
                    "error": "Managed thread already has a running turn",
                },
            )
        except BusyInterruptFailedError as exc:
            return JSONResponse(
                status_code=409,
                content=_interrupt_failure_payload(
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=exc.active_execution_id,
                    backend_thread_id=exc.backend_thread_id or stored_backend_id or "",
                    detail=exc.detail,
                    delivery_payload=delivery_payload,
                ),
            )
        except Exception:
            logger.exception(
                "Managed thread execution setup failed (managed_thread_id=%s)",
                managed_thread_id,
            )
            return {
                "status": "error",
                "send_state": "accepted",
                "execution_state": "completed",
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": None,
                "backend_thread_id": stored_backend_id or "",
                "assistant_text": "",
                "error": MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
                **delivery_payload,
            }
        managed_turn_id = started_execution.execution.execution_id
        if not managed_turn_id:
            raise HTTPException(status_code=500, detail="Failed to create managed turn")
        backend_thread_id = (
            normalize_optional_text(started_execution.thread.backend_thread_id)
            or stored_backend_id
            or ""
        )
        execution_status = str(
            getattr(started_execution.execution, "status", "running") or "running"
        ).strip()
        if execution_status not in {"running", "queued"}:
            detail = _sanitize_managed_thread_result_error(
                started_execution.execution.error
            )
            await notify_managed_thread_terminal_transition(
                request,
                thread=thread,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                to_state="failed",
                reason=detail,
            )
            return {
                "status": "error",
                "send_state": "accepted",
                "execution_state": "completed",
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": managed_turn_id,
                "backend_thread_id": backend_thread_id or "",
                "assistant_text": "",
                "error": detail,
                **delivery_payload,
            }

        notification: Optional[dict[str, Any]] = None
        if notify_on == "terminal":
            automation_client = ManagedThreadAutomationClient(request, lambda: None)
            try:
                notification = await automation_client.create_terminal_followup(
                    managed_thread_id=managed_thread_id,
                    lane_id=notify_lane,
                    notify_once=notify_once,
                    idempotency_key=(
                        f"managed-thread-send-notify:{managed_turn_id}"
                        if notify_once
                        else None
                    ),
                    required=True,
                )
            except ManagedThreadAutomationUnavailable as exc:
                raise HTTPException(
                    status_code=503, detail="Automation action unavailable"
                ) from exc

        def _managed_thread_task_pool() -> set[asyncio.Task[Any]]:
            task_pool = getattr(request.app.state, "pma_managed_thread_tasks", None)
            if not isinstance(task_pool, set):
                task_pool = set()
                request.app.state.pma_managed_thread_tasks = task_pool
            return task_pool

        def _track_task(task: asyncio.Task[Any]) -> None:
            task_pool = _managed_thread_task_pool()
            task_pool.add(task)
            task.add_done_callback(lambda done: task_pool.discard(done))

        async def _run_execution(started: RuntimeThreadExecution) -> dict[str, Any]:
            current_turn_id = started.execution.execution_id
            current_preview = _truncate_text(started.request.message_text, 120)
            current_thread_row = thread_store.get_thread(managed_thread_id) or thread
            current_backend_thread_id = (
                normalize_optional_text(started.thread.backend_thread_id)
                or stored_backend_id
                or ""
            )
            timeline_state = RuntimeThreadRunEventState()
            timeline_events: list[RunEvent] = []
            streamed_raw_events: list[Any] = []
            stream_task: Optional[asyncio.Task[None]] = None
            live_backend_turn_id = str(started.execution.backend_id or "")
            harness = getattr(started, "harness", None)
            if (
                harness is not None
                and callable(getattr(harness, "supports", None))
                and harness_supports_progress_event_stream(harness)
                and current_backend_thread_id
                and live_backend_turn_id
            ):

                async def _collect_timeline() -> None:
                    async for raw_event in harness_progress_event_stream(
                        harness,
                        started.workspace_root,
                        current_backend_thread_id,
                        live_backend_turn_id,
                    ):
                        streamed_raw_events.append(raw_event)
                        timeline_events.extend(
                            await normalize_runtime_thread_raw_event(
                                raw_event,
                                timeline_state,
                                timestamp=now_iso(),
                            )
                        )

                stream_task = asyncio.create_task(_collect_timeline())
            try:
                outcome = await await_runtime_thread_outcome(
                    started,
                    interrupt_event=None,
                    timeout_seconds=PMA_TIMEOUT_SECONDS,
                    execution_error_message=MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
                )
            except Exception:
                logger.exception(
                    "Managed thread execution raised unexpected error (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    current_turn_id,
                )
                outcome = RuntimeThreadOutcome(
                    status="error",
                    assistant_text="",
                    error=MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
                    backend_thread_id=current_backend_thread_id,
                    backend_turn_id=started.execution.backend_id,
                )
            finally:
                if stream_task is not None:
                    stream_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stream_task
            merged_raw_events = merge_runtime_thread_raw_events(
                streamed_raw_events,
                outcome.raw_events,
            )
            if merged_raw_events:
                timeline_events = await _timeline_from_runtime_raw_events(
                    merged_raw_events
                )

            finalized_thread = service.get_thread_target(managed_thread_id)
            resolved_backend_thread_id = (
                normalize_optional_text(
                    finalized_thread.backend_thread_id if finalized_thread else None
                )
                or outcome.backend_thread_id
                or backend_thread_id
            )
            if outcome.status == "ok":
                timeline_events.append(
                    Completed(timestamp=now_iso(), final_message=outcome.assistant_text)
                )
                transcript_metadata = _build_managed_thread_turn_metadata(
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=current_turn_id,
                    thread_row=current_thread_row,
                    backend_thread_id=resolved_backend_thread_id,
                    backend_turn_id=outcome.backend_turn_id,
                    workspace_root=started.workspace_root,
                    model=started.request.model,
                    reasoning=started.request.reasoning,
                    status="ok",
                )
                transcript_turn_id: Optional[str] = None
                try:
                    _persist_managed_thread_timeline(
                        hub_root=hub_root,
                        managed_thread_id=managed_thread_id,
                        managed_turn_id=current_turn_id,
                        thread_row=current_thread_row,
                        metadata=dict(transcript_metadata),
                        events=timeline_events,
                        log_status="ok",
                    )
                    transcripts.write_transcript(
                        turn_id=current_turn_id,
                        metadata=transcript_metadata,
                        assistant_text=outcome.assistant_text,
                    )
                    transcript_turn_id = current_turn_id
                except Exception:
                    logger.exception(
                        "Failed to persist managed-thread transcript (managed_thread_id=%s, managed_turn_id=%s)",
                        managed_thread_id,
                        current_turn_id,
                    )

                try:
                    finalized_execution = service.record_execution_result(
                        managed_thread_id,
                        current_turn_id,
                        status="ok",
                        assistant_text=outcome.assistant_text,
                        error=None,
                        backend_turn_id=outcome.backend_turn_id,
                        transcript_turn_id=transcript_turn_id,
                    )
                except KeyError:
                    finalized_execution = service.get_execution(
                        managed_thread_id, current_turn_id
                    )
                finalized_status = str(
                    (finalized_execution.status if finalized_execution else "")
                ).strip()
                if finalized_status != "ok":
                    detail = MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
                    response_status = "error"
                    transition_state = "failed"
                    if finalized_status == "interrupted":
                        detail = "PMA chat interrupted"
                        response_status = "interrupted"
                        transition_state = "interrupted"
                    elif (
                        finalized_status == "error" and finalized_execution is not None
                    ):
                        detail = _sanitize_managed_thread_result_error(
                            finalized_execution.error
                        )
                    await notify_managed_thread_terminal_transition(
                        request,
                        thread=current_thread_row,
                        managed_thread_id=managed_thread_id,
                        managed_turn_id=current_turn_id,
                        to_state=transition_state,
                        reason=detail,
                    )
                    return {
                        "status": response_status,
                        "managed_thread_id": managed_thread_id,
                        "managed_turn_id": current_turn_id,
                        "backend_thread_id": resolved_backend_thread_id or "",
                        "assistant_text": "",
                        "error": detail,
                        **delivery_payload,
                    }
                thread_store.update_thread_after_turn(
                    managed_thread_id,
                    last_turn_id=current_turn_id,
                    last_message_preview=current_preview,
                )
                await deliver_bound_chat_assistant_output(
                    request,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=current_turn_id,
                    assistant_text=outcome.assistant_text,
                )
                await notify_managed_thread_terminal_transition(
                    request,
                    thread=current_thread_row,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=current_turn_id,
                    to_state="completed",
                    reason="managed_turn_completed",
                )
                return {
                    "status": "ok",
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": current_turn_id,
                    "backend_thread_id": resolved_backend_thread_id or "",
                    "assistant_text": outcome.assistant_text,
                    "error": None,
                    **delivery_payload,
                }

            if outcome.status == "interrupted":
                timeline_events.append(
                    Failed(timestamp=now_iso(), error_message="PMA chat interrupted")
                )
                _persist_managed_thread_timeline(
                    hub_root=hub_root,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=current_turn_id,
                    thread_row=current_thread_row,
                    metadata=_build_managed_thread_turn_metadata(
                        managed_thread_id=managed_thread_id,
                        managed_turn_id=current_turn_id,
                        thread_row=current_thread_row,
                        backend_thread_id=resolved_backend_thread_id,
                        backend_turn_id=outcome.backend_turn_id,
                        workspace_root=started.workspace_root,
                        model=started.request.model,
                        reasoning=started.request.reasoning,
                        status="interrupted",
                    ),
                    events=timeline_events,
                    log_status="interrupted",
                )
                try:
                    service.record_execution_interrupted(
                        managed_thread_id, current_turn_id
                    )
                except KeyError:
                    pass
                detail = "PMA chat interrupted"
                await notify_managed_thread_terminal_transition(
                    request,
                    thread=current_thread_row,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=current_turn_id,
                    to_state="interrupted",
                    reason=detail,
                )
                return {
                    "status": "interrupted",
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": current_turn_id,
                    "backend_thread_id": resolved_backend_thread_id or "",
                    "assistant_text": "",
                    "error": detail,
                    **delivery_payload,
                }

            detail = _sanitize_managed_thread_result_error(outcome.error)
            timeline_events.append(Failed(timestamp=now_iso(), error_message=detail))
            _persist_managed_thread_timeline(
                hub_root=hub_root,
                managed_thread_id=managed_thread_id,
                managed_turn_id=current_turn_id,
                thread_row=current_thread_row,
                metadata=_build_managed_thread_turn_metadata(
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=current_turn_id,
                    thread_row=current_thread_row,
                    backend_thread_id=resolved_backend_thread_id,
                    backend_turn_id=outcome.backend_turn_id,
                    workspace_root=started.workspace_root,
                    model=started.request.model,
                    reasoning=started.request.reasoning,
                    status="error",
                ),
                events=timeline_events,
                log_status="failed",
            )
            try:
                service.record_execution_result(
                    managed_thread_id,
                    current_turn_id,
                    status="error",
                    assistant_text="",
                    error=detail,
                    backend_turn_id=outcome.backend_turn_id,
                    transcript_turn_id=None,
                )
            except KeyError:
                pass
            await notify_managed_thread_terminal_transition(
                request,
                thread=current_thread_row,
                managed_thread_id=managed_thread_id,
                managed_turn_id=current_turn_id,
                to_state="failed",
                reason=detail,
            )
            return {
                "status": "error",
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": current_turn_id,
                "backend_thread_id": resolved_backend_thread_id or "",
                "assistant_text": "",
                "error": detail,
                **delivery_payload,
            }

        def _ensure_queue_worker() -> None:
            task_map = getattr(
                request.app.state, "pma_managed_thread_queue_tasks", None
            )
            if not isinstance(task_map, dict):
                task_map = {}
                request.app.state.pma_managed_thread_queue_tasks = task_map
            existing = task_map.get(managed_thread_id)
            if isinstance(existing, asyncio.Task) and not existing.done():
                return

            worker_task: Optional[asyncio.Task[Any]] = None

            async def _queue_worker() -> None:
                try:
                    while True:
                        if service.get_running_execution(managed_thread_id) is not None:
                            await asyncio.sleep(0.1)
                            continue
                        started = await begin_next_queued_runtime_thread_execution(
                            service, managed_thread_id
                        )
                        if started is None:
                            break
                        await _run_execution(started)
                except Exception:
                    logger.exception(
                        "Managed-thread queue worker failed (managed_thread_id=%s)",
                        managed_thread_id,
                    )
                finally:
                    if (
                        worker_task is not None
                        and task_map.get(managed_thread_id) is worker_task
                    ):
                        task_map.pop(managed_thread_id, None)

            worker_task = asyncio.create_task(_queue_worker())
            task_map[managed_thread_id] = worker_task
            _track_task(worker_task)

        if getattr(started_execution.execution, "status", "running") == "queued":
            running_execution = service.get_running_execution(managed_thread_id)
            queued_payload: dict[str, Any] = {
                "status": "ok",
                "send_state": "queued",
                "execution_state": "queued",
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": managed_turn_id,
                "backend_thread_id": backend_thread_id or "",
                "assistant_text": "",
                "error": None,
                **delivery_payload,
                "queue_depth": service.get_queue_depth(managed_thread_id),
                "active_managed_turn_id": (
                    running_execution.execution_id
                    if running_execution is not None
                    else None
                ),
            }
            if notification is not None:
                queued_payload["notification"] = notification
            _ensure_queue_worker()
            return queued_payload

        accepted_payload: dict[str, Any] = {
            "status": "ok",
            "send_state": "accepted",
            "execution_state": "running",
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "backend_thread_id": backend_thread_id or "",
            "assistant_text": "",
            "error": None,
            **delivery_payload,
        }
        if notification is not None:
            accepted_payload["notification"] = notification

        if defer_execution:

            async def _background_run() -> None:
                try:
                    await _run_execution(started_execution)
                    _ensure_queue_worker()
                except Exception:
                    logger.exception(
                        "Managed-thread background execution failed (managed_thread_id=%s, managed_turn_id=%s)",
                        managed_thread_id,
                        managed_turn_id,
                    )
                    turn = thread_store.get_turn(managed_thread_id, managed_turn_id)
                    if (
                        str((turn or {}).get("status") or "").strip().lower()
                        == "running"
                    ):
                        detail = MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
                        try:
                            service.record_execution_result(
                                managed_thread_id,
                                managed_turn_id,
                                status="error",
                                assistant_text="",
                                error=detail,
                                backend_turn_id=None,
                                transcript_turn_id=None,
                            )
                        except KeyError:
                            pass
                        await notify_managed_thread_terminal_transition(
                            request,
                            thread=thread,
                            managed_thread_id=managed_thread_id,
                            managed_turn_id=managed_turn_id,
                            to_state="failed",
                            reason=detail,
                        )

            _track_task(asyncio.create_task(_background_run()))
            return accepted_payload

        response = await _run_execution(started_execution)
        _ensure_queue_worker()
        response["send_state"] = "accepted"
        response["execution_state"] = "completed"
        if notification is not None:
            response["notification"] = notification
        return response

    @router.post("/threads/{managed_thread_id}/interrupt")
    async def interrupt_managed_thread(
        managed_thread_id: str,
        request: Request,
    ) -> dict[str, Any]:
        return await _interrupt_managed_thread_via_orchestration(
            managed_thread_id=managed_thread_id,
            request=request,
        )


__all__ = [
    "build_managed_thread_runtime_routes",
    "notify_managed_thread_terminal_transition",
]
