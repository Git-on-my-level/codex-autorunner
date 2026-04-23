from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, cast

from ...agents.base import (
    harness_progress_event_stream,
    harness_supports_progress_event_stream,
)
from ...agents.registry import (
    get_registered_agents,
    resolve_agent_runtime,
    wrap_requested_agent_context,
)
from ...core.flows.models import FlowEventType
from ...core.orchestration import (
    MessageRequest,
    build_harness_backed_orchestration_service,
)
from ...core.orchestration.cold_trace_store import ColdTraceWriter
from ...core.orchestration.runtime_thread_events import (
    RuntimeEventDriver,
    decode_runtime_raw_messages,
    merge_runtime_thread_raw_events,
)
from ...core.orchestration.runtime_threads import (
    RuntimeThreadExecution,
    begin_next_queued_runtime_thread_execution,
)
from ...core.orchestration.turn_timeline import (
    append_turn_events_to_cold_trace,
    persist_turn_timeline,
)
from ...core.pma_thread_store import PmaThreadStore
from ...core.ports.run_event import (
    Completed,
    Failed,
    OutputDelta,
    RunEvent,
    TokenUsage,
    is_terminal_run_event,
    now_iso,
)
from ...core.state import RunnerState
from ...core.text_utils import _normalize_optional_text
from ...manifest import ManifestError, load_manifest
from ...tickets.agent_pool import AgentTurnRequest, AgentTurnResult, EmitEventFn
from ..app_server.event_buffer import AppServerEventBuffer
from .opencode_supervisor_factory import build_opencode_supervisor_from_repo_config
from .wiring import build_app_server_supervisor_factory

_logger = logging.getLogger(__name__)
_DEFAULT_EXECUTION_ERROR = "Delegated turn failed"
_TICKET_FLOW_REQUIRED_CAPABILITIES = ("durable_threads", "message_turns")


def _normalize_model(model: Any) -> Optional[str]:
    if isinstance(model, str):
        stripped = model.strip()
        return stripped or None
    if isinstance(model, dict):
        provider = model.get("providerID") or model.get("providerId")
        model_id = model.get("modelID") or model.get("modelId")
        if isinstance(provider, str) and isinstance(model_id, str):
            provider = provider.strip()
            model_id = model_id.strip()
            if provider and model_id:
                return f"{provider}/{model_id}"
    return None


def _find_hub_root(repo_root: Path) -> Path:
    current = repo_root.resolve()
    for _ in range(5):
        manifest_path = current / ".codex-autorunner" / "manifest.yml"
        if manifest_path.exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return repo_root.resolve()


@dataclass
class _RuntimeEventSummary:
    driver: RuntimeEventDriver = field(default_factory=RuntimeEventDriver)
    streamed_live: bool = False
    streamed_raw_events: list[Any] = field(default_factory=list)

    @property
    def assistant_parts(self) -> list[str]:
        return self.driver.assistant_parts

    @property
    def log_lines(self) -> list[str]:
        return self.driver.log_lines

    @property
    def token_usage(self) -> Optional[dict[str, Any]]:
        return self.driver.token_usage

    @token_usage.setter
    def token_usage(self, value: Optional[dict[str, Any]]) -> None:
        self.driver.token_usage = value

    @property
    def timeline_state(self):
        return self.driver.state

    @property
    def timeline_events(self) -> list[RunEvent]:
        return self.driver.run_events


def _final_run_event(
    *,
    status: str,
    assistant_text: str,
    error: Optional[str],
) -> Completed | Failed:
    if status == "ok":
        return Completed(timestamp=now_iso(), final_message=assistant_text)
    return Failed(
        timestamp=now_iso(),
        error_message=error or _DEFAULT_EXECUTION_ERROR,
    )


def _raw_message_has_explicit_delta(message: dict[str, Any]) -> bool:
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    direct_delta = params.get("delta")
    if isinstance(direct_delta, str) and direct_delta:
        return True
    if isinstance(direct_delta, dict):
        for key in ("text", "content"):
            value = direct_delta.get(key)
            if isinstance(value, str) and value:
                return True
    properties = params.get("properties")
    if not isinstance(properties, dict):
        return False
    nested_delta = properties.get("delta")
    if isinstance(nested_delta, str) and nested_delta:
        return True
    if isinstance(nested_delta, dict):
        for key in ("text", "content"):
            value = nested_delta.get(key)
            if isinstance(value, str) and value:
                return True
    return False


def _raw_message_should_emit_agent_delta(message: dict[str, Any]) -> bool:
    method = str(message.get("method") or "").strip()
    if method in {"message.part.updated", "message.part.delta"}:
        return _raw_message_has_explicit_delta(message)
    return True


class DefaultAgentPool:
    """Default ticket-flow adapter backed by orchestration-owned thread targets."""

    def __init__(self, config: Any):
        self._config = config
        self._repo_root = Path(getattr(config, "root", Path.cwd())).resolve()
        self._hub_root = _find_hub_root(self._repo_root)
        self._repo_id = self._resolve_repo_id()
        self._thread_store = PmaThreadStore(self._hub_root)
        self._execution_emitters: dict[str, Optional[EmitEventFn]] = {}
        self._execution_waiters: dict[str, asyncio.Future[AgentTurnResult]] = {}
        self._thread_workers: dict[str, asyncio.Task[None]] = {}
        self._worker_lock: Optional[asyncio.Lock] = None
        self._runtime_context: Optional[Any] = None
        self._orchestration_service: Optional[Any] = None
        self._agent_descriptors_override: Optional[dict[str, Any]] = None
        self._harness_context_override: Optional[Any] = None

    def _resolve_repo_id(self) -> Optional[str]:
        manifest_path = self._hub_root / ".codex-autorunner" / "manifest.yml"
        try:
            manifest = load_manifest(manifest_path, self._hub_root)
        except ManifestError:
            return None
        entry = manifest.get_by_path(self._hub_root, self._repo_root)
        if entry is None:
            return None
        return _normalize_optional_text(entry.id)

    def _ensure_worker_lock(self) -> asyncio.Lock:
        if self._worker_lock is None:
            self._worker_lock = asyncio.Lock()
        return self._worker_lock

    def _ticket_flow_runner_state(self) -> RunnerState:
        approval_mode = self._config.ticket_flow.approval_mode

        if approval_mode == "yolo":
            approval_policy = "never"
            sandbox_mode = "dangerFullAccess"
        else:
            approval_policy = "on-request"
            sandbox_mode = "workspaceWrite"

        return RunnerState(
            last_run_id=None,
            status="idle",
            last_exit_code=None,
            last_run_started_at=None,
            last_run_finished_at=None,
            autorunner_approval_policy=approval_policy,
            autorunner_sandbox_mode=sandbox_mode,
        )

    def _get_harness_context(self) -> Any:
        if self._harness_context_override is not None:
            return self._harness_context_override
        if self._runtime_context is not None:
            return self._runtime_context

        app_server_events = AppServerEventBuffer()
        context = SimpleNamespace(
            config=self._config,
            logger=logging.getLogger("codex_autorunner.backend"),
            app_server_supervisor=None,
            app_server_events=app_server_events,
            opencode_supervisor=None,
        )
        app_server_config = getattr(self._config, "app_server", None)
        if (
            app_server_config is not None
            and getattr(app_server_config, "command", None) is not None
        ):

            async def _handle_notification(message: dict[str, object]) -> None:
                await app_server_events.handle_notification(
                    cast(dict[str, Any], message)
                )

            factory = build_app_server_supervisor_factory(
                self._config,
                logger=logging.getLogger("codex_autorunner.app_server"),
            )
            context.app_server_supervisor = factory(
                "autorunner",
                cast(Any, _handle_notification),
            )
        try:
            context.opencode_supervisor = build_opencode_supervisor_from_repo_config(
                self._config,
                workspace_root=self._repo_root,
                logger=logging.getLogger("codex_autorunner.backend"),
                base_env=None,
                command_override=None,
            )
        except (RuntimeError, ValueError, OSError, TypeError):
            _logger.debug(
                "OpenCode supervisor unavailable for agent pool runtime context.",
                exc_info=True,
            )
            context.opencode_supervisor = None
        self._runtime_context = context
        return context

    def _get_orchestration_service(self) -> Any:
        if self._orchestration_service is not None:
            return self._orchestration_service
        descriptors = self._agent_descriptors_override or get_registered_agents(
            self._config
        )
        harness_context = self._get_harness_context()

        def _make_harness(agent_id: str, profile: Optional[str] = None) -> Any:
            resolution = resolve_agent_runtime(
                agent_id,
                profile,
                context=harness_context,
            )
            descriptor = descriptors.get(resolution.runtime_agent_id)
            if descriptor is None:
                raise KeyError(
                    f"Unknown agent definition '{resolution.runtime_agent_id}'"
                )
            return descriptor.make_harness(
                wrap_requested_agent_context(
                    harness_context,
                    agent_id=resolution.runtime_agent_id,
                    profile=resolution.runtime_profile,
                )
            )

        self._orchestration_service = build_harness_backed_orchestration_service(
            descriptors=cast(Any, descriptors),
            harness_factory=_make_harness,
            pma_thread_store=self._thread_store,
        )
        return self._orchestration_service

    def _resolve_ticket_flow_agent_id(self, agent_id: str) -> str:
        service = self._get_orchestration_service()
        definition = service.get_agent_definition(agent_id)
        if definition is None:
            raise ValueError(f"Unknown agent_id: {agent_id}")
        resolved_agent_id = cast(str, definition.agent_id)
        for capability in _TICKET_FLOW_REQUIRED_CAPABILITIES:
            if capability not in definition.capabilities:
                raise ValueError(
                    "Agent "
                    f"'{resolved_agent_id}' does not support ticket-flow execution "
                    f"(missing capability: {capability})"
                )
        return resolved_agent_id

    async def close_all(self) -> None:
        worker_lock = self._ensure_worker_lock()
        async with worker_lock:
            tasks = list(self._thread_workers.values())
            self._thread_workers.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        for future in list(self._execution_waiters.values()):
            if not future.done():
                future.cancel()
        self._execution_waiters.clear()
        self._execution_emitters.clear()

        context = self._runtime_context
        self._orchestration_service = None
        self._runtime_context = None
        if context is None:
            return
        for supervisor in {
            getattr(context, "app_server_supervisor", None),
            getattr(context, "opencode_supervisor", None),
            getattr(context, "hermes_supervisor", None),
            getattr(context, "zeroclaw_supervisor", None),
        }:
            close_all = getattr(supervisor, "close_all", None)
            if callable(close_all):
                await close_all()

    def _complete_execution(
        self,
        execution_id: str,
        result: AgentTurnResult,
    ) -> None:
        future = self._execution_waiters.pop(execution_id, None)
        self._execution_emitters.pop(execution_id, None)
        if future is not None and not future.done():
            future.set_result(result)

    def _fail_execution(
        self,
        execution_id: str,
        *,
        agent_id: str,
        thread_target_id: str,
        turn_id: str,
        error: str,
    ) -> None:
        self._complete_execution(
            execution_id,
            AgentTurnResult(
                agent_id=agent_id,
                conversation_id=thread_target_id,
                turn_id=turn_id,
                text="",
                error=error,
                raw={
                    "final_status": "failed",
                    "log_lines": [],
                    "token_usage": None,
                    "execution_id": execution_id,
                },
            ),
        )

    async def _emit_runtime_raw_event(
        self,
        raw_event: Any,
        *,
        emit_event: Optional[EmitEventFn],
        turn_id: str,
        summary: _RuntimeEventSummary,
        timestamp: Optional[str] = None,
    ) -> None:
        run_events = await summary.driver.consume_raw_event(
            raw_event,
            timestamp=timestamp,
            store_raw_event=False,
        )
        raw_messages = await decode_runtime_raw_messages(raw_event)
        if emit_event is not None:
            emit_agent_delta = any(
                _raw_message_should_emit_agent_delta(message)
                for message in raw_messages
            )
            for message in raw_messages:
                emit_event(
                    FlowEventType.APP_SERVER_EVENT,
                    {"message": message, "turn_id": turn_id},
                )
            for run_event in run_events:
                if (
                    isinstance(run_event, OutputDelta)
                    and run_event.delta_type
                    in {
                        "assistant_stream",
                        "assistant_message",
                    }
                    and emit_agent_delta
                ):
                    emit_event(
                        FlowEventType.AGENT_STREAM_DELTA,
                        {"delta": run_event.content, "turn_id": turn_id},
                    )
                elif isinstance(run_event, TokenUsage) and isinstance(
                    run_event.usage, dict
                ):
                    emit_event(
                        FlowEventType.TOKEN_USAGE,
                        {"usage": dict(run_event.usage), "turn_id": turn_id},
                    )

    async def _stream_execution_events(
        self,
        started: RuntimeThreadExecution,
        *,
        emit_event: Optional[EmitEventFn],
        summary: _RuntimeEventSummary,
    ) -> None:
        backend_thread_id = _normalize_optional_text(started.thread.backend_thread_id)
        backend_turn_id = _normalize_optional_text(started.execution.backend_id)
        if backend_thread_id is None or backend_turn_id is None:
            return
        if not harness_supports_progress_event_stream(started.harness):
            return
        try:
            async for raw_event in harness_progress_event_stream(
                started.harness,
                started.workspace_root,
                backend_thread_id,
                backend_turn_id,
            ):
                summary.streamed_raw_events.append(raw_event)
                await self._emit_runtime_raw_event(
                    raw_event,
                    emit_event=emit_event,
                    turn_id=backend_turn_id,
                    summary=summary,
                    timestamp=now_iso(),
                )
                summary.streamed_live = True
        except (
            RuntimeError,
            OSError,
            TypeError,
            ValueError,
        ):  # harness stream must not crash
            _logger.debug(
                "Delegated execution event stream failed (thread=%s execution=%s)",
                started.thread.thread_target_id,
                started.execution.execution_id,
                exc_info=True,
            )

    async def _replay_runtime_raw_events(
        self,
        raw_events: list[Any] | tuple[Any, ...],
        *,
        emit_event: Optional[EmitEventFn],
        turn_id: str,
        summary: _RuntimeEventSummary,
    ) -> None:
        for raw_event in raw_events:
            await self._emit_runtime_raw_event(
                raw_event,
                emit_event=emit_event,
                turn_id=turn_id,
                summary=summary,
                timestamp=now_iso(),
            )

    async def _summarize_runtime_raw_events(
        self,
        raw_events: list[Any] | tuple[Any, ...],
        *,
        turn_id: str,
    ) -> _RuntimeEventSummary:
        summary = _RuntimeEventSummary()
        await self._replay_runtime_raw_events(
            raw_events,
            emit_event=None,
            turn_id=turn_id,
            summary=summary,
        )
        return summary

    async def _run_started_execution(self, started: RuntimeThreadExecution) -> None:
        thread_id = started.thread.thread_target_id
        execution_id = started.execution.execution_id
        emitter = self._execution_emitters.get(execution_id)
        summary = _RuntimeEventSummary()
        stream_task: Optional[asyncio.Task[None]] = None
        backend_turn_id = _normalize_optional_text(started.execution.backend_id)
        result_raw_events: tuple[Any, ...] = ()

        if backend_turn_id is not None and harness_supports_progress_event_stream(
            started.harness
        ):
            stream_task = asyncio.create_task(
                self._stream_execution_events(
                    started,
                    emit_event=emitter,
                    summary=summary,
                )
            )

        status = "error"
        error: Optional[str] = None
        assistant_text = ""
        result_status = "failed"
        try:
            result = await started.harness.wait_for_turn(
                started.workspace_root,
                str(started.thread.backend_thread_id or ""),
                backend_turn_id,
                timeout=None,
            )
            result_raw_events = tuple(getattr(result, "raw_events", ()) or ())
            if not summary.streamed_live:
                await self._replay_runtime_raw_events(
                    result_raw_events,
                    emit_event=emitter,
                    turn_id=backend_turn_id or execution_id,
                    summary=summary,
                )
            assistant_text = (
                _normalize_optional_text(result.assistant_text)
                or summary.driver.best_assistant_text()
            )
            normalized_status = str(result.status or "").strip().lower()
            if result.errors:
                status = "error"
                error = (
                    " ".join(
                        str(item).strip() for item in result.errors if str(item).strip()
                    )
                    or _DEFAULT_EXECUTION_ERROR
                )
                result_status = "failed"
            elif normalized_status in {
                "",
                "ok",
                "completed",
                "complete",
                "done",
                "success",
            }:
                status = "ok"
                error = None
                result_status = "completed"
            elif normalized_status in {
                "interrupted",
                "cancelled",
                "canceled",
                "aborted",
            }:
                status = "interrupted"
                error = _DEFAULT_EXECUTION_ERROR
                result_status = "interrupted"
            else:
                status = "error"
                error = _DEFAULT_EXECUTION_ERROR
                result_status = normalized_status or "failed"
        except (
            RuntimeError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:  # harness execution boundary
            status = "error"
            error = str(exc).strip() or _DEFAULT_EXECUTION_ERROR
            result_status = "failed"
        finally:
            if stream_task is not None:
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
        effective_summary = summary
        merged_raw_events = merge_runtime_thread_raw_events(
            summary.streamed_raw_events,
            result_raw_events,
        )
        if merged_raw_events:
            effective_summary = await self._summarize_runtime_raw_events(
                merged_raw_events,
                turn_id=backend_turn_id or execution_id,
            )

        refreshed_thread = started.service.get_thread_target(thread_id)
        backend_thread_id = (
            _normalize_optional_text(
                refreshed_thread.backend_thread_id
                if refreshed_thread is not None
                else None
            )
            or _normalize_optional_text(started.thread.backend_thread_id)
            or ""
        )

        finalized: Optional[Any] = None
        try:
            if status == "ok":
                finalized = started.service.record_execution_result(
                    thread_id,
                    execution_id,
                    status="ok",
                    assistant_text=assistant_text,
                    error=None,
                    backend_turn_id=backend_turn_id,
                    transcript_turn_id=None,
                )
            elif status == "interrupted":
                finalized = started.service.record_execution_interrupted(
                    thread_id,
                    execution_id,
                )
            else:
                finalized = started.service.record_execution_result(
                    thread_id,
                    execution_id,
                    status="error",
                    assistant_text=assistant_text,
                    error=error or _DEFAULT_EXECUTION_ERROR,
                    backend_turn_id=backend_turn_id,
                    transcript_turn_id=None,
                )
        except KeyError:
            finalized = started.service.get_execution(thread_id, execution_id)

        final_turn_id = (
            _normalize_optional_text(
                finalized.backend_id if finalized is not None else None
            )
            or backend_turn_id
            or execution_id
        )
        final_error = None if status == "ok" else (error or _DEFAULT_EXECUTION_ERROR)
        final_text = (
            assistant_text
            if assistant_text
            else effective_summary.driver.best_assistant_text()
        )
        terminal_event = _final_run_event(
            status=status,
            assistant_text=final_text,
            error=final_error,
        )
        effective_summary.timeline_events.append(terminal_event)
        if not is_terminal_run_event(terminal_event):
            raise RuntimeError("Delegated runtime execution did not finalize cleanly")

        thread_row = self._thread_store.get_thread(thread_id) or {}
        thread_metadata_value = thread_row.get("metadata")
        thread_metadata = (
            thread_metadata_value if isinstance(thread_metadata_value, dict) else {}
        )
        try:
            trace_writer = ColdTraceWriter(
                hub_root=self._hub_root,
                execution_id=execution_id,
                backend_thread_id=backend_thread_id or None,
                backend_turn_id=final_turn_id or None,
            ).open()
            trace_manifest_id: Optional[str] = None
            try:
                append_turn_events_to_cold_trace(
                    trace_writer,
                    events=effective_summary.timeline_events,
                )
                trace_manifest_id = trace_writer.finalize().trace_id
            finally:
                trace_writer.close()
            persist_turn_timeline(
                self._hub_root,
                execution_id=execution_id,
                target_kind="thread_target",
                target_id=thread_id,
                repo_id=(
                    _normalize_optional_text(thread_row.get("repo_id")) or self._repo_id
                ),
                run_id=_normalize_optional_text(thread_metadata.get("run_id")),
                resource_kind=_normalize_optional_text(thread_row.get("resource_kind")),
                resource_id=_normalize_optional_text(thread_row.get("resource_id")),
                metadata={
                    "agent": started.thread.agent_id,
                    "execution_id": execution_id,
                    "thread_target_id": thread_id,
                    "backend_thread_id": backend_thread_id or None,
                    "backend_turn_id": final_turn_id or None,
                    "model": started.request.model,
                    "reasoning": started.request.reasoning,
                    "request_kind": started.request.kind,
                    "trace_manifest_id": trace_manifest_id,
                },
                events=effective_summary.timeline_events,
            )
        except (sqlite3.Error, OSError, ValueError, TypeError):
            _logger.exception(
                "Failed to persist delegated turn timeline (thread=%s execution=%s)",
                thread_id,
                execution_id,
            )

        self._complete_execution(
            execution_id,
            AgentTurnResult(
                agent_id=started.thread.agent_id,
                conversation_id=thread_id,
                turn_id=final_turn_id,
                text=final_text,
                error=final_error,
                raw={
                    "final_status": "completed" if status == "ok" else result_status,
                    "log_lines": list(effective_summary.log_lines),
                    "token_usage": effective_summary.token_usage,
                    "execution_id": execution_id,
                    "backend_thread_id": backend_thread_id,
                },
            ),
        )

    async def _ensure_thread_worker(
        self,
        thread_target_id: str,
        *,
        initial: Optional[RuntimeThreadExecution] = None,
    ) -> None:
        worker_lock = self._ensure_worker_lock()
        async with worker_lock:
            existing = self._thread_workers.get(thread_target_id)
            if existing is not None and not existing.done():
                return
            task = asyncio.create_task(
                self._drain_thread_queue(thread_target_id, initial=initial)
            )
            self._thread_workers[thread_target_id] = task

    async def _drain_thread_queue(
        self,
        thread_target_id: str,
        *,
        initial: Optional[RuntimeThreadExecution],
    ) -> None:
        started = initial
        service = self._get_orchestration_service()
        try:
            while True:
                if started is None:
                    started = await begin_next_queued_runtime_thread_execution(
                        service,
                        thread_target_id,
                    )
                    if started is None:
                        break
                try:
                    await self._run_started_execution(started)
                except (
                    RuntimeError,
                    OSError,
                    TypeError,
                    ValueError,
                ) as exc:  # worker loop must not crash
                    _logger.exception(
                        "Delegated execution drain failed (thread=%s execution=%s)",
                        started.thread.thread_target_id,
                        started.execution.execution_id,
                    )
                    self._fail_execution(
                        started.execution.execution_id,
                        agent_id=started.thread.agent_id,
                        thread_target_id=started.thread.thread_target_id,
                        turn_id=started.execution.execution_id,
                        error=str(exc).strip() or _DEFAULT_EXECUTION_ERROR,
                    )
                started = None
        finally:
            worker_lock = self._ensure_worker_lock()
            async with worker_lock:
                current = self._thread_workers.get(thread_target_id)
                if current is asyncio.current_task():
                    self._thread_workers.pop(thread_target_id, None)

    async def run_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        agent_id = self._resolve_ticket_flow_agent_id(req.agent_id)
        options = req.options if isinstance(req.options, dict) else {}
        agent_profile = _normalize_optional_text(
            options.get("profile") or options.get("agent_profile")
        )
        model = _normalize_model(options.get("model"))
        reasoning = (
            options.get("reasoning")
            if isinstance(options.get("reasoning"), str)
            else None
        )

        if req.additional_messages:
            merged: list[str] = [req.prompt]
            for msg in req.additional_messages:
                if not isinstance(msg, dict):
                    continue
                text = msg.get("text")
                if isinstance(text, str) and text.strip():
                    merged.append(text)
            prompt = "\n\n".join(merged)
        else:
            prompt = req.prompt

        state = self._ticket_flow_runner_state()
        service = self._get_orchestration_service()
        ticket_flow_run_id = _normalize_optional_text(options.get("ticket_flow_run_id"))
        ticket_id = _normalize_optional_text(options.get("ticket_id"))
        ticket_path = _normalize_optional_text(options.get("ticket_path"))
        display_name = f"ticket-flow:{agent_id}"
        if agent_profile:
            display_name = f"{display_name}@{agent_profile}"
        thread = service.resolve_thread_target(
            thread_target_id=_normalize_optional_text(req.conversation_id),
            agent_id=agent_id,
            workspace_root=req.workspace_root.resolve(),
            repo_id=self._repo_id,
            display_name=display_name,
            backend_thread_id=None,
            metadata={
                "agent_profile": agent_profile,
                "thread_kind": "ticket_flow",
                "flow_type": "ticket_flow",
                "run_id": ticket_flow_run_id,
                "ticket_id": ticket_id,
                "ticket_path": ticket_path,
            },
        )
        request = MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text=prompt,
            busy_policy="queue",
            agent_profile=agent_profile,
            model=model,
            reasoning=reasoning,
            approval_mode=state.autorunner_approval_policy,
            metadata={"execution_error_message": _DEFAULT_EXECUTION_ERROR},
        )
        execution, harness = await service.send_message_with_started_harness(
            request,
            sandbox_policy=state.autorunner_sandbox_mode,
        )
        execution_id = execution.execution_id
        future: asyncio.Future[AgentTurnResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._execution_waiters[execution_id] = future
        self._execution_emitters[execution_id] = req.emit_event

        if execution.status == "running":
            if harness is None:
                raise RuntimeError("Runtime thread execution started without a harness")
            refreshed_thread = service.get_thread_target(thread.thread_target_id)
            if refreshed_thread is None or not refreshed_thread.workspace_root:
                raise RuntimeError("Thread target is missing workspace_root")
            await self._ensure_thread_worker(
                thread.thread_target_id,
                initial=RuntimeThreadExecution(
                    service=service,
                    harness=harness,
                    thread=refreshed_thread,
                    execution=execution,
                    workspace_root=Path(refreshed_thread.workspace_root),
                    request=request,
                ),
            )
        else:
            await self._ensure_thread_worker(thread.thread_target_id)
        return await future
