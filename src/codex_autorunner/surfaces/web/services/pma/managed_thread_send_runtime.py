from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, NamedTuple, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .....adapters.chat.bound_chat_execution_metadata import (
    merge_bound_chat_execution_metadata,
)
from .....adapters.chat.canonical_turns import build_surface_turn_execution_request
from .....adapters.chat.managed_thread_turns import (
    ManagedThreadCoordinatorHooks,
    build_managed_thread_input_items,
)
from .....core.automation.models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    JOB_RUNNING,
    AutomationChildExecutionEdge,
)
from .....core.automation.store import AutomationStore
from .....core.context_capsule_planner import record_context_capsule_renders
from .....core.managed_thread_store import (
    ManagedThreadAlreadyHasRunningTurnError,
    ManagedThreadNotActiveError,
    ManagedThreadStore,
)
from .....core.orchestration import MessageRequest
from .....core.orchestration.context_capsule_ledger import SQLiteContextCapsuleLedger
from .....core.orchestration.runtime_threads import RuntimeThreadExecution
from .....core.orchestration.service import BusyInterruptFailedError
from .....core.orchestration.sqlite import open_orchestration_sqlite
from .....core.orchestration.turn_execution_contract import TurnExecutionRequest
from .....core.pma.message_options import ManagedThreadMessageOptions
from .....core.pma.outbound_payloads import (
    MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
    build_accepted_send_payload,
    build_archived_thread_payload,
    build_enqueued_send_payload,
    build_execution_setup_error_payload,
    build_interrupt_failure_payload,
    build_not_active_thread_payload,
    build_queued_send_payload,
    build_running_turn_exists_payload,
    build_started_execution_error_payload,
    sanitize_managed_thread_result_error,
)
from ...schemas import ManagedThreadMessageRequest
from ...services.pma.common import normalize_optional_text
from ...services.pma.managed_thread_followup import (
    ManagedThreadAutomationClient,
    ManagedThreadAutomationUnavailable,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagedThreadSendRuntimePorts:
    notify_terminal_transition: Callable[..., Awaitable[None]]
    resolve_surface_targets: Callable[..., tuple[tuple[str, str], ...]]
    run_execution: Callable[..., Awaitable[dict[str, Any]]]
    ensure_queue_worker: Callable[[Any, str], None]
    track_task: Callable[[Any, asyncio.Task[Any]], None]
    runtime_execution_from_started_pair: Callable[..., RuntimeThreadExecution]
    begin_execution: Callable[..., Awaitable[RuntimeThreadExecution]]


@dataclass(frozen=True)
class ManagedThreadQueueRuntimePorts:
    managed_thread_request_for_app: Callable[[Any], Any]
    build_service_for_app: Callable[..., Any]
    resolve_surface_targets: Callable[..., tuple[tuple[str, str], ...]]
    build_queue_execution_controller: Callable[..., Any]
    build_delivery_hooks: Callable[..., Any]
    ensure_queue_worker: Callable[..., None]
    finalize_execution: Callable[..., Awaitable[Any]]
    track_task: Callable[[Any, asyncio.Task[Any]], None]
    recover_bound_progress_execution: Callable[..., Awaitable[bool]]


def _queue_depth(service: Any, managed_thread_id: str) -> int:
    resolver = getattr(service, "get_queue_depth", None)
    if not callable(resolver):
        return 0
    return int(resolver(managed_thread_id))


class _AutomationChildSendPlan(NamedTuple):
    parent_job_id: str
    requested_runtime: dict[str, Any]
    authoritative: bool


def _prepare_automation_child_for_managed_send(
    hub_root: Any,
    automation_child: Optional[dict[str, Any]],
    *,
    turn_request: TurnExecutionRequest,
    thread: Any,
) -> Optional[_AutomationChildSendPlan]:
    """Validate automation parent + payload before a managed turn is launched."""
    if not isinstance(automation_child, dict):
        return None
    parent_job_id = normalize_optional_text(automation_child.get("parent_job_id"))
    if parent_job_id is None:
        raise HTTPException(
            status_code=400,
            detail="automation_child.parent_job_id is required",
        )
    automation_store = AutomationStore(hub_root)
    parent_job = automation_store.get_job(parent_job_id)
    if parent_job is None:
        raise HTTPException(
            status_code=404,
            detail=f"automation parent job not found: {parent_job_id}",
        )
    if parent_job.state != JOB_RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"automation parent job is not running: {parent_job_id}",
        )
    requested_runtime = automation_child.get("requested_runtime")
    if not isinstance(requested_runtime, dict):
        requested_runtime = _runtime_contract_from_turn_request(
            turn_request,
            parent_job_id=parent_job_id,
            thread=thread,
        )
    authoritative = automation_child.get("authoritative_for_parent_completion", True)
    if not isinstance(authoritative, bool):
        raise HTTPException(
            status_code=400,
            detail="automation_child.authoritative_for_parent_completion must be boolean",
        )
    return _AutomationChildSendPlan(
        parent_job_id=parent_job_id,
        requested_runtime=requested_runtime,
        authoritative=authoritative,
    )


def _upsert_automation_child_edge_for_send(
    hub_root: Any,
    plan: _AutomationChildSendPlan,
    *,
    managed_turn_id: str,
) -> None:
    automation_store = AutomationStore(hub_root)
    automation_store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id=plan.parent_job_id,
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            child_id=managed_turn_id,
            requested_runtime=plan.requested_runtime,
            actual_runtime=None,
            authoritative_for_parent_completion=plan.authoritative,
        )
    )


def _runtime_contract_from_turn_request(
    request: TurnExecutionRequest, *, parent_job_id: str, thread: Any
) -> dict[str, Any]:
    metadata = dict(request.metadata) if isinstance(request.metadata, dict) else {}
    return {
        "agent": request.agent,
        "model": request.model,
        "profile": request.profile
        or normalize_optional_text(metadata.get("agent_profile")),
        "reasoning": request.reasoning,
        "approval_policy": request.approval_policy,
        "sandbox_policy": (
            request.sandbox_policy
            if isinstance(request.sandbox_policy, str)
            else str(request.sandbox_policy)
        ),
        "prompt_ref": {
            "kind": "turn_execution_request",
            "request_id": request.request_id,
        },
        "input_ref": {
            "kind": "automation_job",
            "job_id": parent_job_id,
        },
        "workspace_scope": {
            "target_kind": request.target_kind,
            "target_id": request.target_id,
            "workspace_root": request.workspace_root,
        },
        "backend_runtime_id": normalize_optional_text(
            getattr(thread, "backend_runtime_instance_id", None)
        ),
        "provider_payload": dict(request.model_payload) or None,
    }


async def run_managed_thread_message_send(
    *,
    managed_thread_id: str,
    request: Request,
    payload: ManagedThreadMessageRequest,
    thread_store: ManagedThreadStore,
    thread: dict[str, Any],
    service: Any,
    options: ManagedThreadMessageOptions,
    get_runtime_state: Any,
    ports: ManagedThreadSendRuntimePorts,
) -> Any:
    client_turn_id = normalize_optional_text(payload.client_turn_id) or str(
        uuid.uuid4()
    )
    hub_root = request.app.state.config.root

    if payload.profile_explicit:
        meta = thread.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
        prior_profile = normalize_optional_text(
            thread.get("agent_profile") or meta.get("agent_profile")
        )
        next_profile = normalize_optional_text(options.agent_profile)
        if prior_profile != next_profile:
            thread_store.update_thread_metadata(
                managed_thread_id,
                {"agent_profile": options.agent_profile},
            )

    if str(thread.get("lifecycle_status") or "").strip().lower() == "archived":
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

    prepared_execution = None
    automation_child_plan: Optional[_AutomationChildSendPlan] = None
    try:
        progress_targets = ports.resolve_surface_targets(
            service=service,
            managed_thread_id=managed_thread_id,
            allow_running_turn_fallback=False,
        )
        message_request = MessageRequest(
            target_id=managed_thread_id,
            target_kind="thread",
            message_text=options.message,
            busy_policy=options.busy_policy,
            agent_profile=options.agent_profile,
            model=options.model,
            reasoning=options.reasoning,
            approval_mode=options.approval_policy,
            context_profile=options.context_profile,
            input_items=build_managed_thread_input_items(
                options.execution_prompt,
                options.execution_input_items,
            ),
            metadata=merge_bound_chat_execution_metadata(
                {
                    "runtime_prompt": options.execution_prompt,
                    "raw_model_prompt": options.execution_prompt,
                    "user_visible_text": options.message,
                    "title_seed": options.message,
                    "capsule_refs": list(options.capsule_refs),
                    "execution_error_message": MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
                    **(
                        {"attachments": options.delivery_payload["attachments"]}
                        if options.delivery_payload.get("attachments")
                        else {}
                    ),
                },
                origin_kind="surface",
                origin_surface_kind="web",
                origin_surface_key=managed_thread_id,
                progress_targets=progress_targets,
            ),
        )
        canonical_request = build_surface_turn_execution_request(
            message_request,
            request_id=client_turn_id,
            workspace_root=normalize_optional_text(thread.get("workspace_root")) or "",
            surface_kind="web",
            surface_key=managed_thread_id,
            agent=normalize_optional_text(thread.get("agent_id") or thread.get("agent"))
            or "codex",
            approval_policy=options.approval_policy or "never",
            sandbox_policy=options.sandbox_policy or "dangerFullAccess",
            profile=options.agent_profile,
            client_request_id=client_turn_id,
            configured_default_model=options.model,
        )
        automation_child_plan = _prepare_automation_child_for_managed_send(
            hub_root,
            payload.automation_child,
            turn_request=canonical_request,
            thread=thread,
        )
        if payload.wait_for_confirmation:
            started_execution = await ports.begin_execution(
                service,
                canonical_request,
                client_request_id=client_turn_id,
                sandbox_policy=options.sandbox_policy,
            )
        else:
            prepared_execution = await service.prepare_thread_execution(
                canonical_request,
                client_request_id=client_turn_id,
                sandbox_policy=options.sandbox_policy,
            )
            started_execution = None
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
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Managed thread execution setup failed (managed_thread_id=%s)",
            managed_thread_id,
        )
        return build_execution_setup_error_payload(
            managed_thread_id=managed_thread_id,
            backend_thread_id=options.live_backend_thread_id,
            delivery_payload=options.delivery_payload,
        )

    if prepared_execution is not None:
        execution = prepared_execution.execution
        thread_after_send = prepared_execution.thread
    else:
        assert started_execution is not None
        execution = started_execution.execution
        thread_after_send = started_execution.thread
    managed_turn_id = execution.execution_id
    if not managed_turn_id:
        raise HTTPException(status_code=500, detail="Failed to create managed turn")
    if automation_child_plan is not None:
        _upsert_automation_child_edge_for_send(
            hub_root, automation_child_plan, managed_turn_id=managed_turn_id
        )
    backend_thread_id = (
        normalize_optional_text(thread_after_send.backend_thread_id)
        or options.live_backend_thread_id
        or ""
    )
    execution_status = str(getattr(execution, "status", "running") or "running").strip()
    if execution_status not in {"running", "queued"}:
        detail = sanitize_managed_thread_result_error(execution.error)
        await ports.notify_terminal_transition(
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
    if options.capsule_render_plans:
        try:
            with open_orchestration_sqlite(hub_root) as conn:
                record_context_capsule_renders(
                    SQLiteContextCapsuleLedger(conn),
                    options.capsule_render_plans,
                )
        except Exception:
            logger.warning(
                "Failed to record managed-thread context capsule renders",
                extra={"managed_thread_id": managed_thread_id},
                exc_info=True,
            )

    notification: Optional[dict[str, Any]] = None
    if options.notify_on == "terminal":
        automation_client = ManagedThreadAutomationClient(request, get_runtime_state)
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
        return await ports.run_execution(
            request,
            service=service,
            thread_store=thread_store,
            thread=thread,
            started=started,
            fallback_backend_thread_id=options.live_backend_thread_id,
            delivery_payload=options.delivery_payload,
        )

    if not payload.wait_for_confirmation:
        running_execution = service.get_running_execution(managed_thread_id)
        if execution_status == "queued":
            queued_payload = build_enqueued_send_payload(
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                backend_thread_id=backend_thread_id or "",
                delivery_payload=options.delivery_payload,
                execution_state="queued",
                queue_depth=_queue_depth(service, managed_thread_id),
                active_managed_turn_id=(
                    running_execution.execution_id
                    if running_execution is not None
                    else None
                ),
                notification=notification,
            )
            ports.ensure_queue_worker(request.app, managed_thread_id)
            return queued_payload

        enqueued_payload = build_enqueued_send_payload(
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            backend_thread_id=backend_thread_id or "",
            delivery_payload=options.delivery_payload,
            execution_state="running",
            notification=notification,
        )

        async def _background_enqueue_only_run() -> None:
            try:
                started_pair = await service.start_prepared_thread_execution(
                    prepared_execution
                )
                runtime_execution = ports.runtime_execution_from_started_pair(
                    service=service,
                    prepared=prepared_execution,
                    started_pair=started_pair,
                )
                await _run_execution(runtime_execution)
                if _queue_depth(service, managed_thread_id) > 0:
                    ports.ensure_queue_worker(request.app, managed_thread_id)
            except BaseException:
                logger.exception(
                    "Managed-thread enqueue-only background execution failed (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    managed_turn_id,
                )
                turn = thread_store.get_turn(managed_thread_id, managed_turn_id)
                if str((turn or {}).get("status") or "").strip().lower() == "running":
                    await ports.notify_terminal_transition(
                        request,
                        thread=thread,
                        managed_thread_id=managed_thread_id,
                        managed_turn_id=managed_turn_id,
                        to_state="failed",
                        reason=MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
                    )
                raise

        ports.track_task(
            request.app, asyncio.create_task(_background_enqueue_only_run())
        )
        return enqueued_payload

    assert started_execution is not None
    if getattr(started_execution.execution, "status", "running") == "queued":
        running_execution = service.get_running_execution(managed_thread_id)
        queued_payload = build_queued_send_payload(
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            backend_thread_id=backend_thread_id or "",
            delivery_payload=options.delivery_payload,
            queue_depth=_queue_depth(service, managed_thread_id),
            active_managed_turn_id=(
                running_execution.execution_id
                if running_execution is not None
                else None
            ),
            notification=notification,
        )
        ports.ensure_queue_worker(request.app, managed_thread_id)
        return queued_payload

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
                if _queue_depth(service, managed_thread_id) > 0:
                    ports.ensure_queue_worker(request.app, managed_thread_id)
            except BaseException:
                logger.exception(
                    "Managed-thread background execution failed (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    managed_turn_id,
                )
                turn = thread_store.get_turn(managed_thread_id, managed_turn_id)
                if str((turn or {}).get("status") or "").strip().lower() == "running":
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
                    await ports.notify_terminal_transition(
                        request,
                        thread=thread,
                        managed_thread_id=managed_thread_id,
                        managed_turn_id=managed_turn_id,
                        to_state="failed",
                        reason=detail,
                    )
                raise

        ports.track_task(request.app, asyncio.create_task(_background_run()))
        return accepted_payload

    response = await _run_execution(started_execution)
    if _queue_depth(service, managed_thread_id) > 0:
        ports.ensure_queue_worker(request.app, managed_thread_id)
    response["send_state"] = "accepted"
    response["execution_state"] = "completed"
    if notification is not None:
        response["notification"] = notification
    return response


def ensure_pma_managed_thread_queue_worker(
    app: Any,
    managed_thread_id: str,
    *,
    ports: ManagedThreadQueueRuntimePorts,
) -> None:
    request = ports.managed_thread_request_for_app(app)
    thread_store = ManagedThreadStore(app.state.config.root)
    current_thread_row = thread_store.get_thread(managed_thread_id) or {}

    def _resolve_surface_targets(_started: Any) -> tuple[tuple[str, str], ...]:
        service = ports.build_service_for_app(app)
        return ports.resolve_surface_targets(
            service=service,
            managed_thread_id=managed_thread_id,
            started=_started,
        )

    queue_progress = ports.build_queue_execution_controller(
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

    ports.ensure_queue_worker(
        app,
        managed_thread_id,
        managed_thread_request_for_app=ports.managed_thread_request_for_app,
        build_service_for_app=ports.build_service_for_app,
        finalize_managed_thread_execution=ports.finalize_execution,
        track_managed_thread_task=ports.track_task,
        hooks=ManagedThreadCoordinatorHooks(
            durable_delivery=ports.build_delivery_hooks(
                request,
                thread_store=thread_store,
                thread=current_thread_row,
                managed_thread_id=managed_thread_id,
                queue_progress=queue_progress,
            ),
            queue_execution_hooks=queue_progress.hooks,
        ),
    )


async def restart_pma_managed_thread_queue_workers(
    app: Any,
    *,
    ensure_queue_worker_callback: Callable[[Any, str], None],
    restart_queue_workers: Callable[..., Awaitable[None]],
) -> None:
    await restart_queue_workers(
        app,
        ensure_queue_worker_callback=ensure_queue_worker_callback,
    )


async def recover_orphaned_pma_managed_thread_executions(
    app: Any,
    *,
    ports: ManagedThreadQueueRuntimePorts,
    recover_orphaned_executions: Callable[..., Awaitable[None]],
) -> None:
    await recover_orphaned_executions(
        app,
        build_service_for_app=ports.build_service_for_app,
        recover_bound_progress_execution=ports.recover_bound_progress_execution,
    )


__all__ = [
    "ManagedThreadQueueRuntimePorts",
    "ManagedThreadSendRuntimePorts",
    "ensure_pma_managed_thread_queue_worker",
    "recover_orphaned_pma_managed_thread_executions",
    "restart_pma_managed_thread_queue_workers",
    "run_managed_thread_message_send",
]
