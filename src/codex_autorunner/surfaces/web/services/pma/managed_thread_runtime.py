from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request

from .....adapters.chat.bound_chat_execution_metadata import (
    bound_chat_progress_targets_from_execution_mapping,
)
from .....adapters.chat.bound_live_progress import (
    build_bound_chat_queue_execution_controller,
    cleanup_bound_chat_live_progress_failure,
    cleanup_bound_chat_live_progress_success,
)
from .....adapters.chat.managed_thread_turns import (
    ManagedThreadDurableDeliveryHooks,
    ManagedThreadErrorMessages,
    ManagedThreadExecutionHooks,
    ManagedThreadFinalizationResult,
    ManagedThreadStatus,
    ManagedThreadSurfaceInfo,
    ManagedThreadTurnCoordinator,
    build_managed_thread_delivery_intent,
)
from .....adapters.github.managed_thread_pr_binding import (
    self_claim_and_arm_pr_binding,
)
from .....core.config import ConfigError, load_repo_config
from .....core.managed_thread_store import (
    ManagedThreadStore,
)
from .....core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryOutcome,
    MessageRequest,
    SQLiteManagedThreadDeliveryEngine,
)
from .....core.orchestration.runtime_thread_events import RuntimeThreadRunEventState
from .....core.orchestration.runtime_threads import (
    RuntimeThreadExecution,
    begin_runtime_thread_execution,
)
from .....core.text_utils import _truncate_text
from ...schemas import ManagedThreadMessageRequest, ManagedThreadStartMessageRequest
from ...services.pma.automation import notify_managed_thread_terminal_transition
from ...services.pma.common import normalize_optional_text
from ...services.pma.managed_thread_scope import (
    managed_thread_metadata_for_provisioned_workspace,
    provision_managed_thread_workspace,
    resolve_managed_thread_create_resolution,
)
from ...services.pma.managed_thread_send_runtime import (
    ManagedThreadQueueRuntimePorts,
    ManagedThreadSendRuntimePorts,
    ensure_pma_managed_thread_queue_worker,
    recover_orphaned_pma_managed_thread_executions,
    restart_pma_managed_thread_queue_workers,
    run_managed_thread_message_send,
)
from .managed_thread_orchestration import (
    build_managed_thread_orchestration_service as _shared_managed_thread_orchestration_service,
)
from .managed_thread_orchestration import (
    cleanup_failed_provisioned_worktree,
)
from .managed_thread_runtime_control import (
    deliver_bound_chat_assistant_output,
    ensure_queue_worker,
    interrupt_managed_thread_via_orchestration,
    recover_orphaned_executions,
    restart_queue_workers,
)
from .managed_thread_runtime_payloads import (
    MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
    build_execution_result_payload,
    resolve_managed_thread_message_options,
    sanitize_managed_thread_result_error,
)
from .managed_thread_runtime_payloads import (
    get_live_thread_runtime_binding as _get_live_thread_runtime_binding,
)

logger = logging.getLogger(__name__)

PMA_TURN_IDLE_TIMEOUT_SECONDS = 1800
_DEFAULT_PMA_TURN_IDLE_TIMEOUT_SECONDS = 1800


def _managed_thread_request_for_app(app: Any) -> Any:
    return SimpleNamespace(app=app)


def _build_managed_thread_orchestration_service(
    request: Any, *, thread_store: Any = None
) -> Any:
    return _shared_managed_thread_orchestration_service(request)


def _build_managed_thread_orchestration_service_for_app(
    app: Any,
    *,
    thread_store: Any = None,
) -> Any:
    _ = thread_store
    return _build_managed_thread_orchestration_service(
        _managed_thread_request_for_app(app)
    )


def _pma_turn_idle_timeout_seconds(request: Request) -> float:
    overridden_timeout = globals().get(
        "PMA_TURN_IDLE_TIMEOUT_SECONDS",
        _DEFAULT_PMA_TURN_IDLE_TIMEOUT_SECONDS,
    )
    if overridden_timeout != _DEFAULT_PMA_TURN_IDLE_TIMEOUT_SECONDS:
        return float(overridden_timeout)
    configured_timeout = getattr(
        getattr(request.app.state.config, "pma", None),
        "turn_idle_timeout_seconds",
        None,
    )
    if configured_timeout is None:
        return float(_DEFAULT_PMA_TURN_IDLE_TIMEOUT_SECONDS)
    return float(configured_timeout)


def _managed_thread_task_pool(app: Any) -> set[asyncio.Task[Any]]:
    task_pool = getattr(app.state, "managed_thread_tasks", None)
    if not isinstance(task_pool, set):
        task_pool = set()
        app.state.managed_thread_tasks = task_pool
    return task_pool


def _track_managed_thread_task(app: Any, task: asyncio.Task[Any]) -> None:
    task_pool = _managed_thread_task_pool(app)
    task_pool.add(task)
    task.add_done_callback(lambda done: task_pool.discard(done))


async def _create_managed_thread_for_first_message(
    request: Request,
    payload: ManagedThreadStartMessageRequest,
) -> str:
    resolved = resolve_managed_thread_create_resolution(request, payload)
    provisioned_workspace = await asyncio.to_thread(
        provision_managed_thread_workspace,
        request,
        resolution=resolved,
        display_name=normalize_optional_text(payload.name),
    )
    service = _build_managed_thread_orchestration_service(request)
    try:
        metadata = managed_thread_metadata_for_provisioned_workspace(
            resolved,
            provisioned_workspace,
        )
        try:
            if resolved.scope is not None:
                thread = service.create_thread_target(
                    resolved.agent_id,
                    provisioned_workspace.workspace_root,
                    scope=resolved.scope,
                    display_name=normalize_optional_text(payload.name),
                    metadata=metadata,
                )
            else:
                thread = service.create_thread_target(
                    resolved.agent_id,
                    provisioned_workspace.workspace_root,
                    repo_id=resolved.repo_id,
                    resource_kind=resolved.resource_kind,
                    resource_id=resolved.resource_id,
                    display_name=normalize_optional_text(payload.name),
                    metadata=metadata,
                )
        except Exception:
            await cleanup_failed_provisioned_worktree(
                request,
                worktree_repo_id=provisioned_workspace.worktree_repo_id,
            )
            raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return str(thread.thread_target_id)


def _start_payload_to_message_payload(
    payload: ManagedThreadStartMessageRequest,
) -> ManagedThreadMessageRequest:
    return ManagedThreadMessageRequest.model_validate(
        {
            "message": payload.message,
            "attachments": payload.attachments,
            "busy_policy": payload.busy_policy,
            "model": payload.model,
            "reasoning": payload.reasoning,
            "profile": payload.profile,
            "client_turn_id": payload.client_turn_id,
            "defer_execution": payload.defer_execution,
            "wait_for_confirmation": payload.wait_for_confirmation,
            "notify_on": payload.notify_on,
            "terminal_followup": payload.terminal_followup,
            "notify_lane": payload.notify_lane,
            "notify_once": payload.notify_once,
        }
    )


def _archive_new_thread_without_turns(request: Request, managed_thread_id: str) -> None:
    hub_root = request.app.state.config.root
    thread_store = ManagedThreadStore(hub_root)
    if thread_store.list_turns(managed_thread_id, limit=1):
        return
    thread_store.archive_thread(managed_thread_id)


def _runtime_thread_execution_from_started_pair(
    *,
    service: Any,
    prepared: Any,
    started_pair: tuple[Any, Any],
) -> RuntimeThreadExecution:
    started_execution, started_harness = started_pair
    refreshed_thread = service.get_thread_target(prepared.thread.thread_target_id)
    if refreshed_thread is None:
        raise KeyError(
            f"Unknown thread target '{prepared.thread.thread_target_id}' after start"
        )
    return RuntimeThreadExecution(
        service=service,
        harness=started_harness,
        thread=refreshed_thread,
        execution=started_execution,
        workspace_root=prepared.workspace_root,
        request=prepared.request,
    )


async def _recover_pma_bound_chat_execution(
    app: Any,
    *,
    service: Any,
    thread_store: ManagedThreadStore,
    managed_thread_id: str,
    thread: Any,
    execution: Any,
) -> bool:
    workspace_root = normalize_optional_text(getattr(thread, "workspace_root", None))
    execution_id = normalize_optional_text(getattr(execution, "execution_id", None))
    if workspace_root is None or execution_id is None:
        return False
    running_turn = thread_store.get_turn(managed_thread_id, execution_id)
    if running_turn is None:
        return False
    harness_for_thread = getattr(service, "_harness_for_thread", None)
    if not callable(harness_for_thread):
        return False
    metadata = running_turn.get("metadata")
    request_kind = str(running_turn.get("request_kind") or "").strip().lower()
    request = MessageRequest(
        target_id=managed_thread_id,
        target_kind="thread",
        message_text=normalize_optional_text(running_turn.get("prompt")) or "",
        kind="review" if request_kind == "review" else "message",
        model=normalize_optional_text(running_turn.get("model")),
        reasoning=normalize_optional_text(running_turn.get("reasoning")),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )
    started = RuntimeThreadExecution(
        service=service,
        harness=harness_for_thread(thread),
        thread=thread,
        execution=execution,
        workspace_root=Path(workspace_root),
        request=request,
    )
    current_thread_row = thread_store.get_thread(managed_thread_id) or {}

    async def _runner() -> None:
        await _run_managed_thread_execution(
            _managed_thread_request_for_app(app),
            service=service,
            thread_store=thread_store,
            thread=current_thread_row,
            started=started,
            fallback_backend_thread_id=normalize_optional_text(
                getattr(thread, "backend_thread_id", None)
            ),
        )

    _track_managed_thread_task(app, asyncio.create_task(_runner()))
    return True


def _resolve_repo_raw_config_for_workspace(
    request: Request,
    *,
    workspace_root: Path,
) -> Optional[dict[str, Any]]:
    app_config = getattr(request.app.state, "config", None)
    raw_config = getattr(app_config, "raw", {})
    normalized_raw_config = raw_config if isinstance(raw_config, dict) else None
    config_mode = str(getattr(app_config, "mode", "") or "").strip().lower()
    if config_mode != "hub":
        return normalized_raw_config
    config_root = getattr(app_config, "root", None)
    if not isinstance(config_root, Path):
        return normalized_raw_config
    try:
        repo_config = load_repo_config(workspace_root, hub_path=config_root)
    except (ConfigError, OSError, ValueError):
        logger.debug(
            "Failed resolving repo config for SCM polling watch arm (workspace_root=%s)",
            workspace_root,
            exc_info=True,
        )
        return normalized_raw_config
    resolved_raw_config = getattr(repo_config, "raw", None)
    return (
        resolved_raw_config
        if isinstance(resolved_raw_config, dict)
        else normalized_raw_config
    )


def _self_claim_pr_bindings_for_managed_thread(
    request: Request,
    *,
    thread_store: ManagedThreadStore,
    thread: dict[str, Any],
    managed_thread_id: str,
    workspace_root: Path,
    assistant_text: str,
    raw_events: tuple[Any, ...],
) -> None:
    head_branch_hint = thread_store.refresh_thread_head_branch(
        managed_thread_id,
        workspace_root=workspace_root,
    )
    if head_branch_hint is None:
        metadata = thread.get("metadata")
        if isinstance(metadata, dict):
            head_branch_hint = normalize_optional_text(metadata.get("head_branch"))
    normalized_raw_config = _resolve_repo_raw_config_for_workspace(
        request,
        workspace_root=workspace_root,
    )
    self_claim_and_arm_pr_binding(
        hub_root=request.app.state.config.root,
        workspace_root=workspace_root,
        managed_thread_id=managed_thread_id,
        repo_id=normalize_optional_text(thread.get("repo_id")),
        head_branch_hint=head_branch_hint,
        assistant_text=assistant_text,
        raw_events=raw_events,
        raw_config=normalized_raw_config,
        thread_payload=thread,
    )


def _resolve_pma_chat_bound_surface_targets(
    *,
    service: Any,
    managed_thread_id: str,
    started: Any | None = None,
    allow_running_turn_fallback: bool = True,
) -> tuple[tuple[str, str], ...]:
    started_execution = getattr(started, "execution", None)
    started_metadata = getattr(started_execution, "metadata", None)
    if isinstance(started_metadata, dict):
        explicit_targets = bound_chat_progress_targets_from_execution_mapping(
            {"metadata": started_metadata}
        )
        if explicit_targets:
            return explicit_targets
    thread_store = getattr(service, "thread_store", None)
    if allow_running_turn_fallback and thread_store is not None:
        running_turn = thread_store.get_running_turn(managed_thread_id)
        explicit_targets = bound_chat_progress_targets_from_execution_mapping(
            running_turn
        )
        if explicit_targets:
            return explicit_targets
    list_bindings = getattr(service, "list_bindings", None)
    if not callable(list_bindings):
        return ()
    targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for binding in list_bindings(
        thread_target_id=managed_thread_id,
        include_disabled=False,
        limit=1000,
    ):
        surface_kind = normalize_optional_text(getattr(binding, "surface_kind", None))
        surface_key = normalize_optional_text(getattr(binding, "surface_key", None))
        if surface_kind not in {"discord", "telegram"} or surface_key is None:
            continue
        pair = (surface_kind, surface_key)
        if pair in seen:
            continue
        seen.add(pair)
        targets.append(pair)
    return tuple(targets)


def _finalized_with_backend_thread_fallback(
    finalized: ManagedThreadFinalizationResult,
    *,
    started: RuntimeThreadExecution,
    fallback_backend_thread_id: Optional[str] = None,
) -> ManagedThreadFinalizationResult:
    resolved_backend_thread_id = (
        normalize_optional_text(finalized.backend_thread_id)
        or normalize_optional_text(getattr(started.thread, "backend_thread_id", None))
        or normalize_optional_text(fallback_backend_thread_id)
    )
    if resolved_backend_thread_id == normalize_optional_text(
        finalized.backend_thread_id
    ):
        return finalized
    return replace(finalized, backend_thread_id=resolved_backend_thread_id)


async def _run_managed_thread_execution(
    request: Request,
    *,
    service: Any,
    thread_store: ManagedThreadStore,
    thread: dict[str, Any],
    started: RuntimeThreadExecution,
    fallback_backend_thread_id: Optional[str] = None,
    delivery_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    managed_thread_id = (
        normalize_optional_text(getattr(started.thread, "thread_target_id", None))
        or normalize_optional_text(thread.get("managed_thread_id"))
        or normalize_optional_text(thread.get("thread_target_id"))
        or ""
    )
    queue_progress = build_bound_chat_queue_execution_controller(
        hub_root=request.app.state.config.root,
        raw_config=(
            request.app.state.config.raw
            if isinstance(getattr(request.app.state.config, "raw", None), dict)
            else {}
        ),
        managed_thread_id=managed_thread_id,
        surface_target_resolver=lambda _started: _resolve_pma_chat_bound_surface_targets(
            service=service,
            managed_thread_id=managed_thread_id,
            started=_started,
        ),
        retain_completed_surface_targets=True,
    )
    coordinator = _build_managed_thread_coordinator(
        request,
        service=service,
        managed_thread_id=managed_thread_id,
        message_text=started.request.message_text,
    )
    finalized = _finalized_with_backend_thread_fallback(
        await coordinator.run_started_execution(
            started,
            hooks=queue_progress.hooks,
            runtime_event_state=RuntimeThreadRunEventState(),
        ),
        started=started,
        fallback_backend_thread_id=fallback_backend_thread_id,
    )
    return await _deliver_managed_thread_execution_result(
        request,
        thread_store=thread_store,
        thread=thread,
        finalized=finalized,
        response_payload=dict(delivery_payload or {}),
        progress_targets=queue_progress.surface_targets_for(finalized.managed_turn_id),
        clear_progress_targets=queue_progress.clear_surface_targets,
    )


def _pma_finalization_errors(request: Request) -> ManagedThreadErrorMessages:
    timeout_seconds = _pma_turn_idle_timeout_seconds(request)
    return ManagedThreadErrorMessages(
        public_execution_error=MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
        timeout_error="PMA chat timed out",
        interrupted_error="PMA chat interrupted",
        timeout_seconds=timeout_seconds,
        stall_timeout_seconds=timeout_seconds,
        idle_timeout_only=True,
    )


def _build_managed_thread_coordinator(
    request: Request,
    *,
    service: Any,
    managed_thread_id: str,
    message_text: str,
) -> ManagedThreadTurnCoordinator:
    return ManagedThreadTurnCoordinator(
        orchestration_service=service,
        state_root=request.app.state.config.root,
        surface=ManagedThreadSurfaceInfo(
            log_label="PMA",
            surface_kind="web",
            surface_key=managed_thread_id,
        ),
        errors=_pma_finalization_errors(request),
        logger=logger,
        turn_preview=_truncate_text(message_text, 120),
        hub_client=getattr(request.app.state, "hub_client", None),
        raw_config=(
            request.app.state.config.raw
            if isinstance(getattr(request.app.state.config, "raw", None), dict)
            else {}
        ),
    )


async def _finalize_managed_thread_execution(
    request: Request,
    *,
    service: Any,
    thread_store: ManagedThreadStore,
    thread: dict[str, Any],
    started: RuntimeThreadExecution,
    fallback_backend_thread_id: Optional[str] = None,
    on_progress_event: Optional[Any] = None,
) -> ManagedThreadFinalizationResult:
    managed_thread_id = (
        normalize_optional_text(getattr(started.thread, "thread_target_id", None))
        or normalize_optional_text(thread.get("managed_thread_id"))
        or normalize_optional_text(thread.get("thread_target_id"))
        or ""
    )
    if not managed_thread_id:
        raise RuntimeError("Managed-thread execution is missing thread_target_id")
    _ = thread_store
    coordinator = _build_managed_thread_coordinator(
        request,
        service=service,
        managed_thread_id=managed_thread_id,
        message_text=started.request.message_text,
    )
    finalized = _finalized_with_backend_thread_fallback(
        await coordinator.run_started_execution(
            started,
            hooks=ManagedThreadExecutionHooks(on_progress_event=on_progress_event),
            runtime_event_state=RuntimeThreadRunEventState(),
        ),
        started=started,
        fallback_backend_thread_id=fallback_backend_thread_id,
    )
    return finalized


async def _deliver_managed_thread_execution_result(
    request: Request,
    *,
    thread_store: ManagedThreadStore,
    thread: dict[str, Any],
    finalized: ManagedThreadFinalizationResult,
    response_payload: dict[str, Any],
    progress_targets: tuple[tuple[str, str], ...] = (),
    clear_progress_targets: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    finalized_result = finalized
    managed_thread_id = finalized.managed_thread_id
    managed_turn_id = finalized.managed_turn_id
    managed_thread_id = finalized_result.managed_thread_id
    managed_turn_id = finalized_result.managed_turn_id
    current_thread_row = thread_store.get_thread(managed_thread_id) or thread
    if finalized_result.status == "ok":
        thread_store.update_thread_after_turn(
            managed_thread_id,
            last_turn_id=managed_turn_id,
            last_message_preview=normalize_optional_text(
                current_thread_row.get("last_message_preview")
            ),
        )
        workspace_root_text = normalize_optional_text(
            current_thread_row.get("workspace_root")
        )
        if workspace_root_text:
            try:
                _self_claim_pr_bindings_for_managed_thread(
                    request,
                    thread_store=thread_store,
                    thread=current_thread_row,
                    managed_thread_id=managed_thread_id,
                    workspace_root=Path(workspace_root_text),
                    assistant_text=finalized_result.assistant_text,
                    raw_events=(),
                )
            except (
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ):  # best-effort PR self-claim and watch arm
                logger.exception(
                    "Failed to self-claim managed-thread PR binding (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    managed_turn_id,
                )
        try:
            delivery_result = await deliver_bound_chat_assistant_output(
                request,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                assistant_text=finalized_result.assistant_text,
            )
            await notify_managed_thread_terminal_transition(
                request,
                thread=current_thread_row,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                to_state="completed",
                reason="managed_turn_completed",
            )
        except Exception:
            await _cleanup_progress_targets_after_delivery_failure(
                request,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                progress_targets=progress_targets,
                clear_progress_targets=clear_progress_targets,
            )
            raise
        covered_targets = {
            (str(surface_kind), str(surface_key))
            for surface_kind, surface_key in getattr(
                delivery_result, "covered_targets", ()
            )
        }
        for surface_kind, surface_key in progress_targets:
            if (surface_kind, surface_key) in covered_targets:
                continue
            try:
                await cleanup_bound_chat_live_progress_success(
                    hub_root=request.app.state.config.root,
                    raw_config=(
                        request.app.state.config.raw
                        if isinstance(
                            getattr(request.app.state.config, "raw", None), dict
                        )
                        else {}
                    ),
                    surface_kind=surface_kind,
                    surface_key=surface_key,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=managed_turn_id,
                )
            except Exception:
                logger.exception(
                    "Failed to retire uncovered bound chat live progress target (managed_thread_id=%s, managed_turn_id=%s, surface_kind=%s, surface_key=%s)",
                    managed_thread_id,
                    managed_turn_id,
                    surface_kind,
                    surface_key,
                )
        if clear_progress_targets is not None:
            clear_progress_targets(managed_turn_id)
        return build_execution_result_payload(
            status="ok",
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            backend_thread_id=finalized_result.backend_thread_id or "",
            assistant_text=finalized_result.assistant_text,
            error=None,
            response_payload=response_payload,
        )

    if finalized_result.status == "interrupted":
        detail = sanitize_managed_thread_result_error(finalized_result.error)
        await notify_managed_thread_terminal_transition(
            request,
            thread=current_thread_row,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            to_state="interrupted",
            reason=detail,
        )
        return build_execution_result_payload(
            status="interrupted",
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            backend_thread_id=finalized_result.backend_thread_id or "",
            assistant_text="",
            error=detail,
            response_payload=response_payload,
        )

    detail = sanitize_managed_thread_result_error(finalized_result.error)
    await notify_managed_thread_terminal_transition(
        request,
        thread=current_thread_row,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        to_state="failed",
        reason=detail,
    )
    return build_execution_result_payload(
        status="error",
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        backend_thread_id=finalized_result.backend_thread_id or "",
        assistant_text="",
        error=detail,
        response_payload=response_payload,
    )


def _build_pma_queue_delivery_hooks(
    request: Request,
    *,
    thread_store: ManagedThreadStore,
    thread: dict[str, Any],
    managed_thread_id: str,
    queue_progress: Any,
) -> ManagedThreadDurableDeliveryHooks:
    surface = ManagedThreadSurfaceInfo(
        log_label="PMA",
        surface_kind="web",
        surface_key=managed_thread_id,
    )

    class _PmaQueueDeliveryAdapter:
        @property
        def adapter_key(self) -> str:
            return "web"

        async def deliver_managed_thread_record(
            self, record: Any, *, claim: Any
        ) -> ManagedThreadDeliveryAttemptResult:
            _ = claim
            envelope = getattr(record, "envelope", None)
            raw_status = str(getattr(envelope, "final_status", "") or "").strip()
            if raw_status == "ok":
                status: ManagedThreadStatus = "ok"
            elif raw_status == "interrupted":
                status = "interrupted"
            else:
                status = "error"
            finalized = ManagedThreadFinalizationResult(
                status=status,
                assistant_text=str(getattr(envelope, "assistant_text", "") or ""),
                error=normalize_optional_text(getattr(envelope, "error_text", None)),
                managed_thread_id=str(getattr(record, "managed_thread_id", "") or ""),
                managed_turn_id=str(getattr(record, "managed_turn_id", "") or ""),
                backend_thread_id=normalize_optional_text(
                    getattr(envelope, "backend_thread_id", None)
                ),
                token_usage=(
                    dict(getattr(envelope, "token_usage", {}) or {})
                    if getattr(envelope, "token_usage", None)
                    else None
                ),
                session_notice=normalize_optional_text(
                    getattr(envelope, "session_notice", None)
                ),
            )
            current_thread_row = thread_store.get_thread(managed_thread_id) or thread
            try:
                await _deliver_managed_thread_execution_result(
                    request,
                    thread_store=thread_store,
                    thread=current_thread_row,
                    finalized=finalized,
                    response_payload={},
                    progress_targets=queue_progress.surface_targets_for(
                        finalized.managed_turn_id
                    ),
                    clear_progress_targets=queue_progress.clear_surface_targets,
                )
            except Exception as exc:
                return ManagedThreadDeliveryAttemptResult(
                    outcome=ManagedThreadDeliveryOutcome.FAILED,
                    error=str(exc) or exc.__class__.__name__,
                )
            return ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.DELIVERED,
            )

    return ManagedThreadDurableDeliveryHooks(
        engine=SQLiteManagedThreadDeliveryEngine(request.app.state.config.root),
        adapter=_PmaQueueDeliveryAdapter(),
        build_delivery_intent=lambda finalized: build_managed_thread_delivery_intent(
            finalized,
            surface=surface,
        ),
    )


async def _cleanup_progress_targets_after_delivery_failure(
    request: Request,
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    progress_targets: tuple[tuple[str, str], ...],
    clear_progress_targets: Optional[Callable[[str], None]],
) -> None:
    for surface_kind, surface_key in progress_targets:
        try:
            await cleanup_bound_chat_live_progress_failure(
                hub_root=request.app.state.config.root,
                raw_config=(
                    request.app.state.config.raw
                    if isinstance(getattr(request.app.state.config, "raw", None), dict)
                    else {}
                ),
                surface_kind=surface_kind,
                surface_key=surface_key,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                failure_message="Final response delivery failed. Please retry.",
            )
        except Exception:
            logger.exception(
                "Failed to retire bound chat live progress target after delivery failure (managed_thread_id=%s, managed_turn_id=%s, surface_kind=%s, surface_key=%s)",
                managed_thread_id,
                managed_turn_id,
                surface_kind,
                surface_key,
            )
    if clear_progress_targets is not None:
        clear_progress_targets(managed_turn_id)


def ensure_managed_thread_queue_worker(app: Any, managed_thread_id: str) -> None:
    ensure_pma_managed_thread_queue_worker(
        app,
        managed_thread_id,
        ports=_pma_queue_runtime_ports(),
    )


async def restart_managed_thread_queue_workers(app: Any) -> None:
    await restart_pma_managed_thread_queue_workers(
        app,
        ensure_queue_worker_callback=ensure_managed_thread_queue_worker,
        restart_queue_workers=restart_queue_workers,
    )


async def recover_orphaned_managed_thread_executions(app: Any) -> None:
    await recover_orphaned_pma_managed_thread_executions(
        app,
        ports=_pma_queue_runtime_ports(),
        recover_orphaned_executions=recover_orphaned_executions,
    )


def _pma_queue_runtime_ports() -> ManagedThreadQueueRuntimePorts:
    return ManagedThreadQueueRuntimePorts(
        managed_thread_request_for_app=_managed_thread_request_for_app,
        build_service_for_app=_build_managed_thread_orchestration_service_for_app,
        resolve_surface_targets=_resolve_pma_chat_bound_surface_targets,
        build_queue_execution_controller=build_bound_chat_queue_execution_controller,
        build_delivery_hooks=_build_pma_queue_delivery_hooks,
        ensure_queue_worker=ensure_queue_worker,
        finalize_execution=_finalize_managed_thread_execution,
        track_task=_track_managed_thread_task,
        recover_bound_progress_execution=_recover_pma_bound_chat_execution,
    )


async def _interrupt_managed_thread_via_orchestration(
    *,
    managed_thread_id: str,
    request: Request,
) -> dict[str, Any]:
    return await interrupt_managed_thread_via_orchestration(
        managed_thread_id=managed_thread_id,
        request=request,
        build_service=_build_managed_thread_orchestration_service,
        get_live_thread_runtime_binding=_get_live_thread_runtime_binding,
        notify_transition=notify_managed_thread_terminal_transition,
    )


async def send_managed_thread_message_runtime(
    managed_thread_id: str,
    request: Request,
    payload: ManagedThreadMessageRequest,
    *,
    get_runtime_state: Any,
) -> Any:
    hub_root = request.app.state.config.root
    thread_store = ManagedThreadStore(hub_root)
    thread = thread_store.get_thread(managed_thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Managed thread not found")
    service = _build_managed_thread_orchestration_service(
        request,
        thread_store=thread_store,
    )
    options = resolve_managed_thread_message_options(
        request,
        payload,
        managed_thread_id=managed_thread_id,
        thread=thread,
        service=service,
    )
    return await run_managed_thread_message_send(
        managed_thread_id=managed_thread_id,
        request=request,
        payload=payload,
        thread_store=thread_store,
        thread=thread,
        service=service,
        options=options,
        get_runtime_state=get_runtime_state,
        ports=ManagedThreadSendRuntimePorts(
            notify_terminal_transition=notify_managed_thread_terminal_transition,
            resolve_surface_targets=_resolve_pma_chat_bound_surface_targets,
            run_execution=_run_managed_thread_execution,
            ensure_queue_worker=ensure_managed_thread_queue_worker,
            track_task=_track_managed_thread_task,
            runtime_execution_from_started_pair=(
                _runtime_thread_execution_from_started_pair
            ),
            begin_execution=begin_runtime_thread_execution,
        ),
    )


async def start_managed_thread_message_runtime(
    request: Request,
    payload: ManagedThreadStartMessageRequest,
    *,
    get_runtime_state: Any,
) -> Any:
    managed_thread_id = await _create_managed_thread_for_first_message(
        request,
        payload,
    )
    try:
        return await send_managed_thread_message_runtime(
            managed_thread_id,
            request,
            _start_payload_to_message_payload(payload),
            get_runtime_state=get_runtime_state,
        )
    except Exception:
        _archive_new_thread_without_turns(request, managed_thread_id)
        raise


async def interrupt_managed_thread_runtime(
    managed_thread_id: str,
    request: Request,
) -> dict[str, Any]:
    return await _interrupt_managed_thread_via_orchestration(
        managed_thread_id=managed_thread_id,
        request=request,
    )


def build_managed_thread_runtime_routes(
    router: APIRouter,
    get_runtime_state: Any,
) -> None:
    """Build managed-thread runtime routes (send message, interrupt)."""

    @router.post("/threads/{managed_thread_id}/messages")
    async def send_managed_thread_message(
        managed_thread_id: str,
        request: Request,
        payload: ManagedThreadMessageRequest,
    ) -> Any:
        return await send_managed_thread_message_runtime(
            managed_thread_id,
            request,
            payload,
            get_runtime_state=get_runtime_state,
        )

    @router.post("/thread-starts")
    async def start_managed_thread_message(
        request: Request,
        payload: ManagedThreadStartMessageRequest,
    ) -> Any:
        return await start_managed_thread_message_runtime(
            request,
            payload,
            get_runtime_state=get_runtime_state,
        )

    @router.post("/threads/{managed_thread_id}/interrupt")
    async def interrupt_managed_thread(
        managed_thread_id: str,
        request: Request,
    ) -> dict[str, Any]:
        return await interrupt_managed_thread_runtime(
            managed_thread_id,
            request,
        )


__all__ = [
    "ManagedThreadFinalizationResult",
    "ManagedThreadTurnCoordinator",
    "_deliver_managed_thread_execution_result",
    "_finalize_managed_thread_execution",
    "_finalized_with_backend_thread_fallback",
    "_managed_thread_request_for_app",
    "_pma_queue_runtime_ports",
    "_recover_pma_bound_chat_execution",
    "_build_managed_thread_orchestration_service",
    "_resolve_pma_chat_bound_surface_targets",
    "build_managed_thread_runtime_routes",
    "cleanup_bound_chat_live_progress_failure",
    "cleanup_bound_chat_live_progress_success",
    "deliver_bound_chat_assistant_output",
    "ensure_managed_thread_queue_worker",
    "interrupt_managed_thread_runtime",
    "notify_managed_thread_terminal_transition",
    "recover_orphaned_managed_thread_executions",
    "restart_managed_thread_queue_workers",
    "send_managed_thread_message_runtime",
    "start_managed_thread_message_runtime",
]
