from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request

from .....core.chat_bindings import (
    DISCORD_STATE_FILE_DEFAULT,
    TELEGRAM_STATE_FILE_DEFAULT,
)
from .....core.orchestration import OrchestrationBindingStore
from .....core.orchestration.cold_trace_store import ColdTraceStore
from .....core.pma_thread_store import PmaThreadStore
from .....core.time_utils import now_iso
from .....integrations.app_server.event_buffer import AppServerEventBuffer
from .....integrations.chat.bound_chat_execution_metadata import (
    bound_chat_progress_targets_from_execution_mapping,
    execution_mapping_has_chat_surface_origin,
)
from .....integrations.chat.bound_live_progress import (
    build_bound_chat_progress_cleanup_metadata,
)
from .....integrations.chat.managed_thread_turns import (
    ManagedThreadCoordinatorHooks,
    ManagedThreadQueueWorkerHooks,
)
from .....integrations.chat.managed_thread_turns import (
    ensure_managed_thread_queue_worker as ensure_shared_managed_thread_queue_worker,
)
from .....integrations.discord.rendering import format_discord_message
from .....integrations.discord.state import DiscordStateStore
from .....integrations.discord.state import OutboxRecord as DiscordOutboxRecord
from .....integrations.telegram.state import OutboxRecord as TelegramOutboxRecord
from .....integrations.telegram.state import TelegramStateStore, parse_topic_key
from .automation_adapter import (
    first_callable,
    get_automation_store,
    normalize_optional_text,
)
from .publish import enqueue_with_retry, resolve_chat_state_path

logger = logging.getLogger(__name__)

MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR = "Failed to interrupt backend turn"
MANAGED_THREAD_INTERRUPT_FAILED_DETAIL = (
    "Interrupt attempt failed; the active managed turn is still running"
)
BOUND_CHAT_SURFACE_KINDS = frozenset({"discord", "telegram"})


@dataclass(frozen=True)
class BoundChatAssistantDeliveryResult:
    target_count: int = 0
    published_count: int = 0
    covered_targets: tuple[tuple[str, str], ...] = ()


def _interrupt_recovered_lost_backend_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    updated_turn: Optional[dict[str, Any]],
    backend_error: Optional[str],
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


async def interrupt_managed_thread_via_orchestration(
    *,
    managed_thread_id: str,
    request: Request,
    build_service: Any,
    get_live_thread_runtime_binding: Any,
    notify_transition: Any,
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
                    detail=(
                        f"Agent '{agent}' does not support interrupt "
                        "(missing capability: interrupt)"
                    ),
                )

    running_turn = store.get_running_turn(managed_thread_id)
    if running_turn is None:
        raise HTTPException(
            status_code=409, detail="Managed thread has no running turn"
        )
    managed_turn_id = str(running_turn.get("managed_turn_id") or "")
    if not managed_turn_id:
        raise HTTPException(status_code=500, detail="Running turn is missing id")

    service = build_service(request, thread_store=store)
    runtime_binding = get_live_thread_runtime_binding(service, managed_thread_id)
    backend_thread_id = normalize_optional_text(
        getattr(runtime_binding, "backend_thread_id", None)
    )
    backend_turn_id = normalize_optional_text(running_turn.get("backend_turn_id"))
    backend_error: Optional[str] = None
    try:
        stop_outcome = await service.stop_thread(managed_thread_id)
    except Exception:  # intentional: top-level error handler for thread interrupt
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
        recovered_lost_backend = bool(stop_outcome.recovered_lost_backend)
        await notify_transition(
            request,
            thread=thread,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            to_state="interrupted",
            reason="managed_turn_interrupted",
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
                    "backend_interrupt_attempted": bool(backend_thread_id)
                    and not recovered_lost_backend,
                    "backend_error": backend_error,
                    "recovered_lost_backend": recovered_lost_backend,
                },
                ensure_ascii=True,
            ),
        )
        updated_turn = store.get_turn(managed_thread_id, managed_turn_id)
        return {
            "status": "ok",
            "interrupt_state": (
                "recovered_lost_backend" if recovered_lost_backend else "succeeded"
            ),
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "turn": updated_turn,
            "backend_error": backend_error,
            "recovered_lost_backend": recovered_lost_backend,
        }

    if stop_outcome and stop_outcome.recovered_lost_backend:
        await notify_transition(
            request,
            thread=thread,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            to_state="interrupted",
            reason="managed_turn_interrupted",
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
                    "backend_interrupt_attempted": False,
                    "backend_error": None,
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
            backend_error=None,
            backend_interrupt_attempted=False,
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


def ensure_queue_worker(
    app: Any,
    managed_thread_id: str,
    *,
    managed_thread_request_for_app: Any,
    build_service_for_app: Any,
    finalize_managed_thread_execution: Any,
    deliver_managed_thread_execution_result: Any,
    track_managed_thread_task: Any,
    hooks: Optional[
        ManagedThreadQueueWorkerHooks | ManagedThreadCoordinatorHooks
    ] = None,
) -> None:
    task_map = getattr(app.state, "pma_managed_thread_queue_tasks", None)
    if not isinstance(task_map, dict):
        task_map = {}
        app.state.pma_managed_thread_queue_tasks = task_map
    request = managed_thread_request_for_app(app)
    thread_store = PmaThreadStore(app.state.config.root)
    service = build_service_for_app(
        app,
        thread_store=thread_store,
    )

    async def _finalize_started_execution(started: Any) -> Any:
        current_thread_row = thread_store.get_thread(managed_thread_id) or {}
        return await finalize_managed_thread_execution(
            request,
            service=service,
            thread_store=thread_store,
            thread=current_thread_row,
            started=started,
        )

    async def _deliver_result(finalized: Any) -> None:
        current_thread_row = thread_store.get_thread(managed_thread_id) or {}
        await deliver_managed_thread_execution_result(
            request,
            thread_store=thread_store,
            thread=current_thread_row,
            finalized=finalized,
            response_payload={},
        )

    resolved_hooks: Optional[ManagedThreadQueueWorkerHooks]
    if hooks is None:
        resolved_hooks = None
    elif isinstance(hooks, ManagedThreadCoordinatorHooks):
        resolved_hooks = hooks.queue_worker_hooks()
    else:
        resolved_hooks = hooks

    shared_kwargs = dict(
        task_map=task_map,
        managed_thread_id=managed_thread_id,
        orchestration_service=service,
        spawn_task=lambda coro: _spawn_and_track_managed_thread_task(
            app,
            coro,
            track_managed_thread_task=track_managed_thread_task,
        ),
        finalize_started_execution=_finalize_started_execution,
        poll_interval_seconds=0.1,
    )
    ensure_shared_managed_thread_queue_worker(
        **shared_kwargs,
        durable_delivery=(
            None if resolved_hooks is None else resolved_hooks.durable_delivery
        ),
        deliver_result=(
            _deliver_result
            if resolved_hooks is None
            else (resolved_hooks.deliver_result or _deliver_result)
        ),
        run_with_indicator=(
            None if resolved_hooks is None else resolved_hooks.run_with_indicator
        ),
        execution_hooks=(
            None if resolved_hooks is None else resolved_hooks.execution_hooks
        ),
    )


def _spawn_and_track_managed_thread_task(
    app: Any,
    coro: Any,
    *,
    track_managed_thread_task: Any,
) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro)
    track_managed_thread_task(app, task)
    return task


async def restart_queue_workers(
    app: Any,
    *,
    ensure_queue_worker_callback: Any,
) -> None:
    thread_store = PmaThreadStore(app.state.config.root)
    for managed_thread_id in thread_store.list_thread_ids_with_pending_queue(
        limit=None
    ):
        ensure_queue_worker_callback(app, managed_thread_id)


def _has_owning_bound_chat_surface(
    binding_store: OrchestrationBindingStore,
    managed_thread_id: str,
    *,
    thread_store: PmaThreadStore,
) -> bool:
    running_turn = thread_store.get_running_turn(managed_thread_id)
    progress_targets = set(
        bound_chat_progress_targets_from_execution_mapping(running_turn)
    )
    if progress_targets:
        for binding in binding_store.list_bindings(
            thread_target_id=managed_thread_id,
            include_disabled=False,
            limit=1000,
        ):
            surface_kind = normalize_optional_text(
                getattr(binding, "surface_kind", None)
            )
            surface_key = normalize_optional_text(getattr(binding, "surface_key", None))
            if surface_kind is None or surface_key is None:
                continue
            if (surface_kind, surface_key) in progress_targets:
                return True
        return False
    return any(
        normalize_optional_text(getattr(binding, "surface_kind", None))
        in BOUND_CHAT_SURFACE_KINDS
        for binding in binding_store.list_bindings(
            thread_target_id=managed_thread_id,
            include_disabled=False,
            limit=1000,
        )
    )


def _is_chat_origin_running_execution(
    thread_store: PmaThreadStore, managed_thread_id: str
) -> bool:
    running_turn = thread_store.get_running_turn(managed_thread_id)
    return execution_mapping_has_chat_surface_origin(running_turn)


async def recover_orphaned_executions(
    app: Any,
    *,
    build_service_for_app: Any,
    recover_bound_progress_execution: Any | None = None,
) -> None:
    thread_store = PmaThreadStore(app.state.config.root)
    checkpoint_store = ColdTraceStore(app.state.config.root)
    binding_store = OrchestrationBindingStore(app.state.config.root)
    service = build_service_for_app(
        app,
        thread_store=thread_store,
    )
    app_server_events = getattr(app.state, "app_server_events", None)
    for managed_thread_id in thread_store.list_thread_ids_with_running_executions(
        limit=None,
    ):
        try:
            thread = service.get_thread_target(managed_thread_id)
            execution = service.get_running_execution(managed_thread_id)
            if thread is None or execution is None:
                continue
            checkpoint = checkpoint_store.load_checkpoint(execution.execution_id)
            if checkpoint is not None:
                restored_backend_thread_id = normalize_optional_text(
                    checkpoint.backend_thread_id
                )
                restored_backend_turn_id = normalize_optional_text(
                    checkpoint.backend_turn_id
                )
                if (
                    restored_backend_thread_id is not None
                    and not normalize_optional_text(thread.backend_thread_id)
                ):
                    thread_store.set_thread_backend_id(
                        managed_thread_id,
                        restored_backend_thread_id,
                    )
                    thread = service.get_thread_target(managed_thread_id) or thread
                if (
                    restored_backend_turn_id is not None
                    and not normalize_optional_text(execution.backend_id)
                ):
                    thread_store.set_turn_backend_turn_id(
                        execution.execution_id,
                        restored_backend_turn_id,
                    )
                    execution = (
                        service.get_running_execution(managed_thread_id) or execution
                    )
            if _has_owning_bound_chat_surface(
                binding_store,
                managed_thread_id,
                thread_store=thread_store,
            ):
                if _is_chat_origin_running_execution(thread_store, managed_thread_id):
                    continue
                if (
                    recover_bound_progress_execution is not None
                    and await recover_bound_progress_execution(
                        app,
                        service=service,
                        thread_store=thread_store,
                        managed_thread_id=managed_thread_id,
                        thread=thread,
                        execution=execution,
                    )
                ):
                    continue
            has_turn = getattr(app_server_events, "has_turn", None)
            if (
                callable(has_turn)
                and thread.backend_thread_id
                and execution.backend_id
                and await has_turn(thread.backend_thread_id, execution.backend_id)
            ):
                continue
            if app_server_events is not None and not isinstance(
                app_server_events, AppServerEventBuffer
            ):
                if callable(
                    getattr(app_server_events, "list_events", None)
                ) or callable(getattr(app_server_events, "stream_entries", None)):
                    continue
            if (
                str(thread.agent_id or "").strip().lower() == "zeroclaw"
                and thread.workspace_root
                and thread.backend_thread_id
                and execution.backend_id
            ):
                zeroclaw_supervisor = getattr(app.state, "zeroclaw_supervisor", None)
                list_turn_events = getattr(
                    zeroclaw_supervisor, "list_turn_events", None
                )
                if callable(list_turn_events):
                    try:
                        live_events = await list_turn_events(
                            Path(str(thread.workspace_root)),
                            thread.backend_thread_id,
                            execution.backend_id,
                        )
                    except (
                        AttributeError,
                        TypeError,
                        RuntimeError,
                    ):  # intentional: defensive fallback for dynamic supervisor call
                        live_events = []
                    if live_events:
                        continue
            service.recover_running_execution_after_restart(managed_thread_id)
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
        ):  # intentional: top-level error handler for thread recovery loop
            logger.exception(
                "Managed-thread running execution recovery failed (managed_thread_id=%s)",
                managed_thread_id,
            )


async def deliver_bound_chat_assistant_output(
    request: Request,
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    assistant_text: str,
) -> BoundChatAssistantDeliveryResult:
    message = str(assistant_text or "").strip()
    if not message:
        return BoundChatAssistantDeliveryResult()

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
        return BoundChatAssistantDeliveryResult()

    discord_store: Optional[DiscordStateStore] = None
    telegram_store: Optional[TelegramStateStore] = None
    created_at = now_iso()
    target_count = 0
    published_count = 0
    covered_targets: list[tuple[str, str]] = []
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
                    target_count += 1
                    digest = hashlib.sha256(
                        f"managed-thread:{managed_turn_id}:discord:{channel_id}".encode(
                            "utf-8"
                        )
                    ).hexdigest()[:24]
                    record_id = f"managed-thread:{digest}"
                    if await discord_store.get_outbox(record_id) is not None:
                        covered_targets.append(("discord", channel_id))
                        continue
                    record = DiscordOutboxRecord(
                        record_id=record_id,
                        channel_id=channel_id,
                        message_id=None,
                        operation="send",
                        payload_json={
                            "content": format_discord_message(message),
                            "_codex_autorunner_cleanup": (
                                build_bound_chat_progress_cleanup_metadata(
                                    surface_kind="discord",
                                    surface_key=channel_id,
                                    managed_thread_id=managed_thread_id,
                                    managed_turn_id=managed_turn_id,
                                )
                            ),
                        },
                        created_at=created_at,
                    )
                    store = discord_store
                    await enqueue_with_retry(
                        lambda record=record, store=store: store.enqueue_outbox(record)
                    )
                    covered_targets.append(("discord", channel_id))
                    published_count += 1
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
                except ValueError:
                    logger.warning(
                        "Failed to parse telegram bound-chat surface key for managed-thread delivery: %s",
                        surface_key,
                    )
                    continue
                target_count += 1
                digest = hashlib.sha256(
                    (
                        f"managed-thread:{managed_turn_id}:telegram:{chat_id}:{thread_id or 'root'}"
                    ).encode("utf-8")
                ).hexdigest()[:24]
                record_id = f"managed-thread:{digest}"
                if await telegram_store.get_outbox(record_id) is not None:
                    covered_targets.append(("telegram", surface_key))
                    continue
                outbox_key = f"managed-thread:{managed_turn_id}:{chat_id}:{thread_id or 'root'}:send"
                telegram_record = TelegramOutboxRecord(
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
                    delivery_metadata=build_bound_chat_progress_cleanup_metadata(
                        surface_kind="telegram",
                        surface_key=surface_key,
                        managed_thread_id=managed_thread_id,
                        managed_turn_id=managed_turn_id,
                    ),
                )
                telegram_delivery_store = telegram_store
                await enqueue_with_retry(
                    lambda record=telegram_record, store=telegram_delivery_store: store.enqueue_outbox(
                        record
                    )
                )
                covered_targets.append(("telegram", surface_key))
                published_count += 1
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                KeyError,
            ):  # intentional: best-effort per-binding delivery
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
            except (
                sqlite3.Error,
                OSError,
                RuntimeError,
            ):  # intentional: best-effort store close
                logger.exception("Failed to close discord bound-chat delivery store")
        if telegram_store is not None:
            try:
                await telegram_store.close()
            except (
                sqlite3.Error,
                OSError,
                RuntimeError,
            ):  # intentional: best-effort store close
                logger.exception("Failed to close telegram bound-chat delivery store")
    return BoundChatAssistantDeliveryResult(
        target_count=target_count,
        published_count=published_count,
        covered_targets=tuple(covered_targets),
    )


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
            "idempotency_key": f"managed_turn:{managed_turn_id}:{normalized_to_state}",
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

    method = first_callable(store, ("notify_transition",))
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
    except (
        AttributeError,
        TypeError,
        RuntimeError,
    ):  # intentional: defensive catch for dynamic automation call
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
        except (
            AttributeError,
            RuntimeError,
        ):  # intentional: defensive catch for dynamic supervisor call
            logger.exception("Failed immediate PMA automation processing")
    except (
        AttributeError,
        RuntimeError,
    ):  # intentional: defensive catch for dynamic supervisor call
        logger.exception("Failed immediate PMA automation processing")


__all__ = [
    "MANAGED_THREAD_INTERRUPT_FAILED_DETAIL",
    "MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR",
    "deliver_bound_chat_assistant_output",
    "ensure_queue_worker",
    "interrupt_managed_thread_via_orchestration",
    "notify_managed_thread_terminal_transition",
    "recover_orphaned_executions",
    "restart_queue_workers",
]
