from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .....agents.base import (
    harness_progress_event_stream,
    harness_supports_progress_event_stream,
)
from .....core.config import ConfigError, load_repo_config
from .....core.orchestration import MessageRequest
from .....core.orchestration.cold_trace_store import ColdTraceWriter
from .....core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    merge_runtime_thread_raw_events,
    normalize_runtime_thread_raw_event,
)
from .....core.orchestration.runtime_threads import (
    RuntimeThreadExecution,
    RuntimeThreadOutcome,
    await_runtime_thread_outcome,
    begin_runtime_thread_execution,
)
from .....core.orchestration.service import BusyInterruptFailedError
from .....core.orchestration.turn_timeline import (
    append_turn_events_to_cold_trace,
    persist_turn_timeline,
)
from .....core.pma_thread_store import (
    ManagedThreadAlreadyHasRunningTurnError,
    ManagedThreadNotActiveError,
    PmaThreadStore,
)
from .....core.pma_transcripts import PmaTranscriptStore
from .....core.ports.run_event import Completed, Failed, RunEvent
from .....core.text_utils import _truncate_text
from .....core.time_utils import now_iso
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

PMA_TURN_IDLE_TIMEOUT_SECONDS = 1800
_DEFAULT_PMA_TURN_IDLE_TIMEOUT_SECONDS = 1800


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
    cold_trace_writer: Optional[ColdTraceWriter] = None,
) -> Optional[str]:
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
            cold_trace_writer=cold_trace_writer,
        )
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):  # best-effort timeline persistence
        logger.exception(
            "Failed to persist %s managed-thread timeline (managed_thread_id=%s, managed_turn_id=%s)",
            log_status,
            managed_thread_id,
            managed_turn_id,
        )
    finally:
        if cold_trace_writer is not None:
            cold_trace_writer.close()
    return normalize_optional_text(metadata.get("trace_manifest_id"))


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
    if not managed_thread_id:
        raise RuntimeError("Managed-thread execution is missing thread_target_id")
    current_turn_id = started.execution.execution_id
    current_preview = _truncate_text(started.request.message_text, 120)
    current_thread_row = thread_store.get_thread(managed_thread_id) or thread
    current_backend_thread_id = (
        normalize_optional_text(started.thread.backend_thread_id)
        or normalize_optional_text(fallback_backend_thread_id)
        or ""
    )
    timeline_state = RuntimeThreadRunEventState()
    timeline_events: list[RunEvent] = []
    streamed_raw_events: list[Any] = []
    stream_task: Optional[asyncio.Task[None]] = None
    live_backend_turn_id = str(started.execution.backend_id or "")
    harness = getattr(started, "harness", None)
    hub_root = request.app.state.config.root
    transcripts = PmaTranscriptStore(hub_root)
    response_payload = dict(delivery_payload or {})
    live_timeline_count = 0
    live_timeline_error_logged = False
    cold_trace_writer: Optional[ColdTraceWriter] = None
    live_timeline_metadata = _build_managed_thread_turn_metadata(
        managed_thread_id=managed_thread_id,
        managed_turn_id=current_turn_id,
        thread_row=current_thread_row,
        backend_thread_id=current_backend_thread_id,
        backend_turn_id=started.execution.backend_id,
        workspace_root=started.workspace_root,
        model=started.request.model,
        reasoning=started.request.reasoning,
        status="running",
    )
    try:
        cold_trace_writer = ColdTraceWriter(
            hub_root=hub_root,
            execution_id=current_turn_id,
            backend_thread_id=current_backend_thread_id or None,
            backend_turn_id=started.execution.backend_id,
        ).open()
    except Exception:
        logger.warning(
            "Failed to open managed-thread cold trace writer (managed_thread_id=%s, managed_turn_id=%s)",
            managed_thread_id,
            current_turn_id,
            exc_info=True,
        )

    def _persist_live_timeline_events(events: list[RunEvent]) -> None:
        nonlocal live_timeline_count
        nonlocal live_timeline_error_logged
        if not events:
            return
        if cold_trace_writer is None:
            if not live_timeline_error_logged:
                live_timeline_error_logged = True
                logger.error(
                    "Skipping live managed-thread timeline persistence without a cold trace writer (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    current_turn_id,
                )
            return
        try:
            persist_turn_timeline(
                hub_root,
                execution_id=current_turn_id,
                target_kind="thread_target",
                target_id=managed_thread_id,
                repo_id=normalize_optional_text(current_thread_row.get("repo_id")),
                resource_kind=normalize_optional_text(
                    current_thread_row.get("resource_kind")
                ),
                resource_id=normalize_optional_text(
                    current_thread_row.get("resource_id")
                ),
                metadata=live_timeline_metadata,
                events=events,
                start_index=live_timeline_count + 1,
                cold_trace_writer=cold_trace_writer,
            )
        except Exception:
            if not live_timeline_error_logged:
                live_timeline_error_logged = True
                logger.exception(
                    "Failed to persist live managed-thread timeline (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    current_turn_id,
                )
        else:
            live_timeline_count += len(events)

    def _finalize_live_cold_trace(events: list[RunEvent]) -> Optional[str]:
        nonlocal cold_trace_writer
        if cold_trace_writer is None or live_timeline_count <= 0:
            return None
        try:
            append_turn_events_to_cold_trace(
                cold_trace_writer,
                events=events[live_timeline_count:],
            )
            return cold_trace_writer.finalize().trace_id
        except Exception:
            logger.exception(
                "Failed to finalize live managed-thread cold trace (managed_thread_id=%s, managed_turn_id=%s)",
                managed_thread_id,
                current_turn_id,
            )
            return None
        finally:
            cold_trace_writer.close()
            cold_trace_writer = None

    def _persist_final_timeline_with_cold_trace(
        *,
        metadata: dict[str, Any],
        events: list[RunEvent],
        log_status: str,
    ) -> Optional[str]:
        manifest_id = normalize_optional_text(metadata.get("trace_manifest_id"))
        if manifest_id is not None:
            _persist_managed_thread_timeline(
                hub_root=hub_root,
                managed_thread_id=managed_thread_id,
                managed_turn_id=current_turn_id,
                thread_row=current_thread_row,
                metadata=metadata,
                events=events,
                log_status=log_status,
            )
            return manifest_id

        final_writer: Optional[ColdTraceWriter] = cold_trace_writer
        if final_writer is None:
            try:
                final_writer = ColdTraceWriter(
                    hub_root=hub_root,
                    execution_id=current_turn_id,
                    backend_thread_id=normalize_optional_text(
                        metadata.get("backend_thread_id")
                    ),
                    backend_turn_id=normalize_optional_text(
                        metadata.get("backend_turn_id")
                    ),
                ).open()
            except Exception:
                logger.exception(
                    "Failed to open final managed-thread cold trace writer (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    current_turn_id,
                )
                _persist_managed_thread_timeline(
                    hub_root=hub_root,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=current_turn_id,
                    thread_row=current_thread_row,
                    metadata=metadata,
                    events=events,
                    log_status=log_status,
                )
                return None

        try:
            append_turn_events_to_cold_trace(final_writer, events=events)
            manifest_id = final_writer.finalize().trace_id
        except Exception:
            logger.exception(
                "Failed to persist final managed-thread cold trace (managed_thread_id=%s, managed_turn_id=%s)",
                managed_thread_id,
                current_turn_id,
            )
            manifest_id = None
        finally:
            final_writer.close()

        _persist_managed_thread_timeline(
            hub_root=hub_root,
            managed_thread_id=managed_thread_id,
            managed_turn_id=current_turn_id,
            thread_row=current_thread_row,
            metadata={
                **metadata,
                **({"trace_manifest_id": manifest_id} if manifest_id else {}),
            },
            events=events,
            log_status=log_status,
        )
        return manifest_id

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
                new_events = await normalize_runtime_thread_raw_event(
                    raw_event,
                    timeline_state,
                    timestamp=now_iso(),
                )
                timeline_events.extend(new_events)
                _persist_live_timeline_events(new_events)

        stream_task = asyncio.create_task(_collect_timeline())
    try:
        idle_timeout_seconds = _pma_turn_idle_timeout_seconds(request)
        outcome = await await_runtime_thread_outcome(
            started,
            interrupt_event=None,
            timeout_seconds=None,
            stall_timeout_seconds=idle_timeout_seconds,
            stall_timeout_replaces_wall_clock_timeout=True,
            execution_error_message=MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
        )
    except Exception:  # intentional: top-level error handler for execution outcome
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
            tuple(merged_raw_events)
        )

    finalized_thread = service.get_thread_target(managed_thread_id)
    resolved_backend_thread_id = (
        normalize_optional_text(
            finalized_thread.backend_thread_id if finalized_thread else None
        )
        or outcome.backend_thread_id
        or current_backend_thread_id
    )
    if outcome.status == "ok":
        try:
            _self_claim_pr_bindings_for_managed_thread(
                request,
                thread_store=thread_store,
                thread=current_thread_row,
                managed_thread_id=managed_thread_id,
                workspace_root=started.workspace_root,
                assistant_text=outcome.assistant_text,
                raw_events=tuple(merged_raw_events),
            )
        except Exception:
            logger.exception(
                "Managed-thread PR binding self-claim failed (managed_thread_id=%s, managed_turn_id=%s)",
                managed_thread_id,
                current_turn_id,
            )
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
            final_trace_manifest_id = _finalize_live_cold_trace(timeline_events)
            if final_trace_manifest_id:
                transcript_metadata["trace_manifest_id"] = final_trace_manifest_id
            _persist_final_timeline_with_cold_trace(
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
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):  # best-effort transcript persistence
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
            elif finalized_status == "error" and finalized_execution is not None:
                detail = sanitize_managed_thread_result_error(finalized_execution.error)
            await notify_managed_thread_terminal_transition(
                request,
                thread=current_thread_row,
                managed_thread_id=managed_thread_id,
                managed_turn_id=current_turn_id,
                to_state=transition_state,
                reason=detail,
            )
            return build_execution_result_payload(
                status=response_status,
                managed_thread_id=managed_thread_id,
                managed_turn_id=current_turn_id,
                backend_thread_id=resolved_backend_thread_id or "",
                assistant_text="",
                error=detail,
                response_payload=response_payload,
            )
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
        return build_execution_result_payload(
            status="ok",
            managed_thread_id=managed_thread_id,
            managed_turn_id=current_turn_id,
            backend_thread_id=resolved_backend_thread_id or "",
            assistant_text=outcome.assistant_text,
            error=None,
            response_payload=response_payload,
        )

    if outcome.status == "interrupted":
        timeline_events.append(
            Failed(timestamp=now_iso(), error_message="PMA chat interrupted")
        )
        interrupted_metadata = _build_managed_thread_turn_metadata(
            managed_thread_id=managed_thread_id,
            managed_turn_id=current_turn_id,
            thread_row=current_thread_row,
            backend_thread_id=resolved_backend_thread_id,
            backend_turn_id=outcome.backend_turn_id,
            workspace_root=started.workspace_root,
            model=started.request.model,
            reasoning=started.request.reasoning,
            status="interrupted",
        )
        final_trace_manifest_id = _finalize_live_cold_trace(timeline_events)
        if final_trace_manifest_id:
            interrupted_metadata["trace_manifest_id"] = final_trace_manifest_id
        _persist_final_timeline_with_cold_trace(
            metadata=interrupted_metadata,
            events=timeline_events,
            log_status="interrupted",
        )
        try:
            service.record_execution_interrupted(managed_thread_id, current_turn_id)
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
        return build_execution_result_payload(
            status="interrupted",
            managed_thread_id=managed_thread_id,
            managed_turn_id=current_turn_id,
            backend_thread_id=resolved_backend_thread_id or "",
            assistant_text="",
            error=detail,
            response_payload=response_payload,
        )

    detail = sanitize_managed_thread_result_error(outcome.error)
    timeline_events.append(Failed(timestamp=now_iso(), error_message=detail))
    failed_metadata = _build_managed_thread_turn_metadata(
        managed_thread_id=managed_thread_id,
        managed_turn_id=current_turn_id,
        thread_row=current_thread_row,
        backend_thread_id=resolved_backend_thread_id,
        backend_turn_id=outcome.backend_turn_id,
        workspace_root=started.workspace_root,
        model=started.request.model,
        reasoning=started.request.reasoning,
        status="error",
    )
    final_trace_manifest_id = _finalize_live_cold_trace(timeline_events)
    if final_trace_manifest_id:
        failed_metadata["trace_manifest_id"] = final_trace_manifest_id
    _persist_final_timeline_with_cold_trace(
        metadata=failed_metadata,
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
    return build_execution_result_payload(
        status="error",
        managed_thread_id=managed_thread_id,
        managed_turn_id=current_turn_id,
        backend_thread_id=resolved_backend_thread_id or "",
        assistant_text="",
        error=detail,
        response_payload=response_payload,
    )


def ensure_managed_thread_queue_worker(app: Any, managed_thread_id: str) -> None:
    ensure_queue_worker(
        app,
        managed_thread_id,
        managed_thread_request_for_app=_managed_thread_request_for_app,
        build_service_for_app=_build_managed_thread_orchestration_service_for_app,
        run_managed_thread_execution=_run_managed_thread_execution,
        track_managed_thread_task=_track_managed_thread_task,
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
