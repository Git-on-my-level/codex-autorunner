from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional, cast

from ...agents.registry import get_registered_agents
from ...core.flows.models import FlowEventType
from ...core.orchestration import (
    MessageRequest,
    build_harness_backed_orchestration_service,
)
from ...core.orchestration.runtime_threads import (
    RuntimeThreadExecution,
    begin_next_queued_runtime_thread_execution,
)
from ...core.pma_thread_store import PmaThreadStore
from ...core.ports.run_event import Completed, Failed, is_terminal_run_event, now_iso
from ...core.sse import parse_sse_lines
from ...core.state import RunnerState
from ...manifest import ManifestError, load_manifest
from ...tickets.agent_pool import AgentTurnRequest, AgentTurnResult, EmitEventFn
from ..app_server.event_buffer import AppServerEventBuffer
from .opencode_supervisor_factory import build_opencode_supervisor_from_repo_config
from .wiring import build_app_server_supervisor_factory

_logger = logging.getLogger(__name__)
_DEFAULT_EXECUTION_ERROR = "Delegated turn failed"


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


def _normalize_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


async def _iter_sse_lines(raw_event: str) -> AsyncIterator[str]:
    for line in raw_event.splitlines():
        yield line
    yield ""


@dataclass
class _RuntimeEventSummary:
    assistant_parts: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    token_usage: Optional[dict[str, Any]] = None
    streamed_live: bool = False


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
        except Exception:
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
        descriptors = self._agent_descriptors_override or get_registered_agents()
        harness_context = self._get_harness_context()

        def _make_harness(agent_id: str) -> Any:
            descriptor = descriptors.get(agent_id)
            if descriptor is None:
                raise KeyError(f"Unknown agent definition '{agent_id}'")
            return descriptor.make_harness(harness_context)

        self._orchestration_service = build_harness_backed_orchestration_service(
            descriptors=cast(Any, descriptors),
            harness_factory=_make_harness,
            pma_thread_store=self._thread_store,
        )
        return self._orchestration_service

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

    async def _decode_runtime_messages(self, raw_event: Any) -> list[dict[str, Any]]:
        if isinstance(raw_event, dict):
            if isinstance(raw_event.get("message"), dict):
                return [dict(raw_event["message"])]
            if isinstance(raw_event.get("method"), str):
                return [dict(raw_event)]
            return []
        if not isinstance(raw_event, str):
            return []
        text = raw_event.strip()
        if not text:
            return []
        if not text.startswith("event:") and not text.startswith("data:"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return []
            return await self._decode_runtime_messages(parsed)

        messages: list[dict[str, Any]] = []
        async for event in parse_sse_lines(_iter_sse_lines(raw_event)):
            if not event.data:
                continue
            try:
                parsed = json.loads(event.data)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                if isinstance(parsed.get("message"), dict):
                    messages.append(dict(parsed["message"]))
                elif isinstance(parsed.get("method"), str):
                    messages.append(dict(parsed))
        return messages

    def _emit_runtime_message(
        self,
        message: dict[str, Any],
        *,
        emit_event: Optional[EmitEventFn],
        turn_id: str,
        summary: _RuntimeEventSummary,
    ) -> None:
        if emit_event is not None:
            emit_event(
                FlowEventType.APP_SERVER_EVENT,
                {"message": message, "turn_id": turn_id},
            )

        method = str(message.get("method") or "").strip()
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}

        usage_raw = params.get("tokenUsage") or params.get("usage")
        if isinstance(usage_raw, dict):
            usage = dict(usage_raw)
            summary.token_usage = usage
            if emit_event is not None:
                emit_event(
                    FlowEventType.TOKEN_USAGE,
                    {"usage": usage, "turn_id": turn_id},
                )

        item = params.get("item")
        if method == "item/completed" and isinstance(item, dict):
            item_type = str(item.get("type") or "").strip()
            if item_type == "agentMessage":
                item_text = _normalize_optional_text(item.get("text"))
                if item_text:
                    summary.assistant_parts.append(item_text)
                    if emit_event is not None:
                        emit_event(
                            FlowEventType.AGENT_STREAM_DELTA,
                            {"delta": item_text, "turn_id": turn_id},
                        )
            return

        delta = _normalize_optional_text(params.get("delta"))
        if delta is None:
            return

        delta_type = _normalize_optional_text(
            params.get("deltaType") or params.get("delta_type")
        )
        if delta_type is None:
            lowered = method.lower()
            if method in {"outputDelta", "item/agentMessage/delta"}:
                delta_type = "assistant_stream"
            elif "reasoning" in lowered:
                delta_type = "thinking"
            elif lowered.endswith("outputdelta"):
                delta_type = "log_line"

        if delta_type in {"assistant_stream", "assistant_message"}:
            summary.assistant_parts.append(delta)
            if emit_event is not None:
                emit_event(
                    FlowEventType.AGENT_STREAM_DELTA,
                    {"delta": delta, "turn_id": turn_id},
                )
            return

        if delta_type == "log_line":
            summary.log_lines.append(delta)

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
        if not started.harness.supports("event_streaming"):
            return
        try:
            async for raw_event in started.harness.stream_events(
                started.workspace_root,
                backend_thread_id,
                backend_turn_id,
            ):
                for message in await self._decode_runtime_messages(raw_event):
                    self._emit_runtime_message(
                        message,
                        emit_event=emit_event,
                        turn_id=backend_turn_id,
                        summary=summary,
                    )
                    summary.streamed_live = True
        except Exception:
            _logger.debug(
                "Delegated execution event stream failed (thread=%s execution=%s)",
                started.thread.thread_target_id,
                started.execution.execution_id,
                exc_info=True,
            )

    async def _run_started_execution(self, started: RuntimeThreadExecution) -> None:
        thread_id = started.thread.thread_target_id
        execution_id = started.execution.execution_id
        emitter = self._execution_emitters.get(execution_id)
        summary = _RuntimeEventSummary()
        stream_task: Optional[asyncio.Task[None]] = None
        backend_turn_id = _normalize_optional_text(started.execution.backend_id)

        if backend_turn_id is not None and started.harness.supports("event_streaming"):
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
            if not summary.streamed_live:
                for raw_event in result.raw_events:
                    for message in await self._decode_runtime_messages(raw_event):
                        self._emit_runtime_message(
                            message,
                            emit_event=emitter,
                            turn_id=backend_turn_id or execution_id,
                            summary=summary,
                        )
            assistant_text = (
                _normalize_optional_text(result.assistant_text)
                or "".join(summary.assistant_parts).strip()
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
        except Exception as exc:
            status = "error"
            error = str(exc).strip() or _DEFAULT_EXECUTION_ERROR
            result_status = "failed"
        finally:
            if stream_task is not None:
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task

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
            else "".join(summary.assistant_parts).strip()
        )
        terminal_event = _final_run_event(
            status=status,
            assistant_text=final_text,
            error=final_error,
        )
        if not is_terminal_run_event(terminal_event):
            raise RuntimeError("Delegated runtime execution did not finalize cleanly")

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
                    "log_lines": list(summary.log_lines),
                    "token_usage": summary.token_usage,
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
                except Exception as exc:
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
        if req.agent_id not in {"codex", "opencode"}:
            raise ValueError(f"Unsupported agent_id: {req.agent_id}")

        options = req.options if isinstance(req.options, dict) else {}
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
        thread = service.resolve_thread_target(
            thread_target_id=_normalize_optional_text(req.conversation_id),
            agent_id=req.agent_id,
            workspace_root=req.workspace_root.resolve(),
            repo_id=self._repo_id,
            display_name=f"ticket-flow:{req.agent_id}",
            backend_thread_id=None,
            metadata={
                "thread_kind": "ticket_flow",
                "flow_type": "ticket_flow",
                "run_id": ticket_flow_run_id,
            },
        )
        harness = service.harness_factory(thread.agent_id)
        request = MessageRequest(
            target_id=thread.thread_target_id,
            target_kind="thread",
            message_text=prompt,
            busy_policy="queue",
            model=model,
            reasoning=reasoning,
            approval_mode=state.autorunner_approval_policy,
            metadata={"execution_error_message": _DEFAULT_EXECUTION_ERROR},
        )
        execution = await service.send_message(
            request,
            sandbox_policy=state.autorunner_sandbox_mode,
            harness=harness,
        )
        execution_id = execution.execution_id
        future: asyncio.Future[AgentTurnResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._execution_waiters[execution_id] = future
        self._execution_emitters[execution_id] = req.emit_event

        if execution.status == "running":
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
