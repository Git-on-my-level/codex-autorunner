from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Optional, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .....core.config import ConfigError, load_repo_config
from .....core.orchestration import MessageRequest
from .....core.orchestration.runtime_thread_events import RuntimeThreadRunEventState
from .....core.orchestration.runtime_threads import (
    RuntimeThreadExecution,
    begin_runtime_thread_execution,
)
from .....core.orchestration.service import BusyInterruptFailedError
from .....core.pma_thread_store import (
    ManagedThreadAlreadyHasRunningTurnError,
    ManagedThreadNotActiveError,
    PmaThreadStore,
)
from .....core.text_utils import _truncate_text
from .....integrations.chat.bound_live_progress import (
    build_bound_chat_queue_execution_controller,
    cleanup_bound_chat_live_progress_failure,
    cleanup_bound_chat_live_progress_success,
)
from .....integrations.chat.managed_thread_startup_recovery import (
    find_surface_key_for_running_execution,
)
from .....integrations.chat.managed_thread_turns import (
    ManagedThreadCoordinatorHooks,
    ManagedThreadErrorMessages,
    ManagedThreadExecutionHooks,
    ManagedThreadFinalizationResult,
    ManagedThreadSurfaceInfo,
    ManagedThreadTurnCoordinator,
)
from .....integrations.github.managed_thread_pr_binding import (
    self_claim_and_arm_pr_binding,
)
from ...schemas import PmaManagedThreadMessageRequest
from ...services.pma.managed_thread_followup import (
    ManagedThreadAutomationClient,
    ManagedThreadAutomationUnavailable,
)
from .automation_adapter import normalize_optional_text
from .managed_thread_runtime_control import (
    deliver_bound_chat_assistant_output,
    ensure_queue_worker,
    interrupt_managed_thread_via_orchestration,
    notify_managed_thread_terminal_transition,
    recover_orphaned_executions,
    restart_queue_workers,
)
from .managed_thread_runtime_payloads import (
    MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
    build_accepted_send_payload,
    build_archived_thread_payload,
    build_execution_result_payload,
    build_execution_setup_error_payload,
    build_interrupt_failure_payload,
    build_not_active_thread_payload,
    build_queued_send_payload,
    build_running_turn_exists_payload,
    build_started_execution_error_payload,
    resolve_managed_thread_message_options,
    sanitize_managed_thread_result_error,
    sync_zeroclaw_context_if_needed,
)
from .managed_thread_runtime_payloads import (
    get_live_thread_runtime_binding as _get_live_thread_runtime_binding,
)
from .managed_threads import (
    build_managed_thread_orchestration_service as _shared_managed_thread_orchestration_service,
)

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

PMA_TIMEOUT_SECONDS = 7200
_DEFAULT_PMA_TIMEOUT_SECONDS = 7200


def _build_managed_thread_orchestration_service(
    request: Request, *, thread_store: Optional[PmaThreadStore] = None
):
    _ = thread_store
    return _shared_managed_thread_orchestration_service(request)


def _managed_thread_request_for_app(app: Any) -> Request:
    return cast(Request, SimpleNamespace(app=app))


def _build_managed_thread_orchestration_service_for_app(
    app: Any, *, thread_store: Optional[PmaThreadStore] = None
):
    return _build_managed_thread_orchestration_service(
        _managed_thread_request_for_app(app),
        thread_store=thread_store,
    )


def _pma_turn_timeout_seconds(request: Request) -> float:
    overridden_timeout = globals().get(
        "PMA_TIMEOUT_SECONDS",
        _DEFAULT_PMA_TIMEOUT_SECONDS,
    )
    if overridden_timeout != _DEFAULT_PMA_TIMEOUT_SECONDS:
        return float(overridden_timeout)
    configured_timeout = getattr(
        getattr(request.app.state.config, "pma", None),
        "turn_timeout_seconds",
        None,
    )
    if configured_timeout is None:
        return float(_DEFAULT_PMA_TIMEOUT_SECONDS)
    return float(configured_timeout)


def _managed_thread_task_pool(app: Any) -> set[asyncio.Task[Any]]:
    task_pool = getattr(app.state, "pma_managed_thread_tasks", None)
    if not isinstance(task_pool, set):
        task_pool = set()
        app.state.pma_managed_thread_tasks = task_pool
    return task_pool


def _track_managed_thread_task(app: Any, task: asyncio.Task[Any]) -> None:
    task_pool = _managed_thread_task_pool(app)
    task_pool.add(task)
    task.add_done_callback(lambda done: task_pool.discard(done))


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
    thread_store: PmaThreadStore,
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
) -> tuple[tuple[str, str], ...]:
    thread_store = getattr(service, "thread_store", None)
    if thread_store is None:
        return ()
    targets: list[tuple[str, str]] = []
    for surface_kind in ("discord", "telegram"):
        surface_key = find_surface_key_for_running_execution(
            service,
            thread_store,
            managed_thread_id=managed_thread_id,
            surface_kind=surface_kind,
            limit=1000,
        )
        if surface_key is not None:
            targets.append((surface_kind, surface_key))
    if len(targets) > 1:
        logger.warning(
            "Ambiguous PMA chat-bound surface ownership; suppressing live progress fanout (managed_thread_id=%s, targets=%s)",
            managed_thread_id,
            targets,
        )
        return ()
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
    thread_store: PmaThreadStore,
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
        ),
        retain_completed_surface_targets=True,
    )
    coordinator = _build_pma_managed_thread_coordinator(
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
    return ManagedThreadErrorMessages(
        public_execution_error=MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
        timeout_error="PMA chat timed out",
        interrupted_error="PMA chat interrupted",
        timeout_seconds=_pma_turn_timeout_seconds(request),
    )


def _build_pma_managed_thread_coordinator(
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
            surface_kind="pma_web",
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
    thread_store: PmaThreadStore,
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
    coordinator = _build_pma_managed_thread_coordinator(
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
    thread_store: PmaThreadStore,
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
            last_message_preview=_truncate_text(finalized_result.assistant_text, 120),
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
    def _resolve_surface_targets(_started: Any) -> tuple[tuple[str, str], ...]:
        service = _build_managed_thread_orchestration_service_for_app(app)
        return _resolve_pma_chat_bound_surface_targets(
            service=service,
            managed_thread_id=managed_thread_id,
        )

    queue_progress = build_bound_chat_queue_execution_controller(
        hub_root=app.state.config.root,
        raw_config=(
            app.state.config.raw
            if isinstance(getattr(app.state.config, "raw", None), dict)
            else {}
        ),
        managed_thread_id=managed_thread_id,
        surface_target_resolver=_resolve_surface_targets,
        retain_completed_surface_targets=True,
    )

    async def _deliver_with_progress_targets(*args: Any, **kwargs: Any) -> Any:
        finalized = kwargs.get("finalized")
        managed_turn_id = str(getattr(finalized, "managed_turn_id", "") or "").strip()
        return await _deliver_managed_thread_execution_result(
            *args,
            **kwargs,
            progress_targets=queue_progress.surface_targets_for(managed_turn_id),
            clear_progress_targets=queue_progress.clear_surface_targets,
        )

    ensure_queue_worker(
        app,
        managed_thread_id,
        managed_thread_request_for_app=_managed_thread_request_for_app,
        build_service_for_app=_build_managed_thread_orchestration_service_for_app,
        finalize_managed_thread_execution=_finalize_managed_thread_execution,
        deliver_managed_thread_execution_result=_deliver_with_progress_targets,
        track_managed_thread_task=_track_managed_thread_task,
        hooks=ManagedThreadCoordinatorHooks(
            queue_execution_hooks=queue_progress.hooks,
        ),
    )


async def restart_managed_thread_queue_workers(app: Any) -> None:
    await restart_queue_workers(
        app,
        ensure_queue_worker_callback=ensure_managed_thread_queue_worker,
    )


async def recover_orphaned_managed_thread_executions(app: Any) -> None:
    await recover_orphaned_executions(
        app,
        build_service_for_app=_build_managed_thread_orchestration_service_for_app,
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


def build_managed_thread_runtime_routes(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build managed-thread runtime routes (send message, interrupt)."""

    @router.post("/threads/{managed_thread_id}/messages")
    async def send_managed_thread_message(
        managed_thread_id: str,
        request: Request,
        payload: PmaManagedThreadMessageRequest,
    ) -> Any:
        hub_root = request.app.state.config.root
        thread_store = PmaThreadStore(hub_root)
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

        if (thread.get("status") or "") == "archived":
            return JSONResponse(
                status_code=409,
                content=build_archived_thread_payload(
                    managed_thread_id=managed_thread_id,
                    backend_thread_id=normalize_optional_text(
                        thread.get("backend_thread_id")
                    )
                    or "",
                ),
            )
        sync_zeroclaw_context_if_needed(thread=thread, options=options)
        try:
            started_execution = await begin_runtime_thread_execution(
                service,
                MessageRequest(
                    target_id=managed_thread_id,
                    target_kind="thread",
                    message_text=options.message,
                    busy_policy=options.busy_policy,
                    agent_profile=options.agent_profile,
                    model=options.model,
                    reasoning=options.reasoning,
                    approval_mode=options.approval_policy,
                    context_profile=options.context_profile,
                    metadata={
                        "runtime_prompt": options.execution_prompt,
                        "execution_error_message": MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
                    },
                ),
                sandbox_policy=options.sandbox_policy,
            )
        except ManagedThreadNotActiveError as exc:
            return JSONResponse(
                status_code=409,
                content=build_not_active_thread_payload(
                    managed_thread_id=managed_thread_id,
                    backend_thread_id=options.live_backend_thread_id,
                    exc=exc,
                ),
            )
        except ManagedThreadAlreadyHasRunningTurnError:
            running_turn = thread_store.get_running_turn(managed_thread_id)
            return JSONResponse(
                status_code=409,
                content=build_running_turn_exists_payload(
                    managed_thread_id=managed_thread_id,
                    backend_thread_id=options.live_backend_thread_id,
                    running_turn=running_turn,
                ),
            )
        except BusyInterruptFailedError as exc:
            return JSONResponse(
                status_code=409,
                content=build_interrupt_failure_payload(
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=exc.active_execution_id,
                    backend_thread_id=exc.backend_thread_id
                    or options.live_backend_thread_id
                    or "",
                    detail=exc.detail,
                    delivery_payload=options.delivery_payload,
                ),
            )
        except Exception:  # intentional: top-level error handler for execution setup
            logger.exception(
                "Managed thread execution setup failed (managed_thread_id=%s)",
                managed_thread_id,
            )
            return build_execution_setup_error_payload(
                managed_thread_id=managed_thread_id,
                backend_thread_id=options.live_backend_thread_id,
                delivery_payload=options.delivery_payload,
            )
        managed_turn_id = started_execution.execution.execution_id
        if not managed_turn_id:
            raise HTTPException(status_code=500, detail="Failed to create managed turn")
        backend_thread_id = (
            normalize_optional_text(started_execution.thread.backend_thread_id)
            or options.live_backend_thread_id
            or ""
        )
        execution_status = str(
            getattr(started_execution.execution, "status", "running") or "running"
        ).strip()
        if execution_status not in {"running", "queued"}:
            detail = sanitize_managed_thread_result_error(
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
            return build_started_execution_error_payload(
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                backend_thread_id=backend_thread_id or "",
                error=detail,
                delivery_payload=options.delivery_payload,
            )

        notification: Optional[dict[str, Any]] = None
        if options.notify_on == "terminal":
            automation_client = ManagedThreadAutomationClient(
                request,
                get_runtime_state,
            )
            try:
                notification = await automation_client.create_terminal_followup(
                    managed_thread_id=managed_thread_id,
                    lane_id=options.notify_lane,
                    notify_once=options.notify_once,
                    idempotency_key=(
                        f"managed-thread-send-notify:{managed_turn_id}"
                        if options.notify_once
                        else None
                    ),
                    required=options.notify_required,
                )
            except ManagedThreadAutomationUnavailable as exc:
                raise HTTPException(
                    status_code=503, detail="Automation action unavailable"
                ) from exc

        async def _run_execution(started: RuntimeThreadExecution) -> dict[str, Any]:
            return await _run_managed_thread_execution(
                request,
                service=service,
                thread_store=thread_store,
                thread=thread,
                started=started,
                fallback_backend_thread_id=options.live_backend_thread_id,
                delivery_payload=options.delivery_payload,
            )

        if getattr(started_execution.execution, "status", "running") == "queued":
            running_execution = service.get_running_execution(managed_thread_id)
            return build_queued_send_payload(
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                backend_thread_id=backend_thread_id or "",
                delivery_payload=options.delivery_payload,
                queue_depth=service.get_queue_depth(managed_thread_id),
                active_managed_turn_id=(
                    running_execution.execution_id
                    if running_execution is not None
                    else None
                ),
                notification=notification,
            )

        accepted_payload = build_accepted_send_payload(
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            backend_thread_id=backend_thread_id or "",
            delivery_payload=options.delivery_payload,
            notification=notification,
        )

        if options.defer_execution:

            async def _background_run() -> None:
                try:
                    await _run_execution(started_execution)
                    if service.get_queue_depth(managed_thread_id) > 0:
                        ensure_managed_thread_queue_worker(
                            request.app,
                            managed_thread_id,
                        )
                except BaseException:
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
                            logger.warning(
                                "Failed to record error for cancelled managed thread turn "
                                "(managed_thread_id=%s, managed_turn_id=%s)",
                                managed_thread_id,
                                managed_turn_id,
                            )
                        await notify_managed_thread_terminal_transition(
                            request,
                            thread=thread,
                            managed_thread_id=managed_thread_id,
                            managed_turn_id=managed_turn_id,
                            to_state="failed",
                            reason=detail,
                        )
                    else:
                        # Turn is no longer running (e.g. send completed, then
                        # ensure_managed_thread_queue_worker failed). No store cleanup.
                        pass
                    # Must stay outside the ``if``: always propagate CancelledError etc.
                    raise

            _track_managed_thread_task(
                request.app, asyncio.create_task(_background_run())
            )
            return accepted_payload

        response = await _run_execution(started_execution)
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
    "ensure_managed_thread_queue_worker",
    "notify_managed_thread_terminal_transition",
    "recover_orphaned_managed_thread_executions",
    "restart_managed_thread_queue_workers",
]
