from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ...core.flows.models import FlowEventType
from ...core.pma_thread_store import PmaThreadStore
from ...core.ports.run_event import (
    Completed,
    Failed,
    OutputDelta,
    RunEvent,
    Started,
    TokenUsage,
    is_terminal_run_event,
)
from ...core.state import RunnerState
from ...manifest import ManifestError, load_manifest
from ...tickets.agent_pool import AgentTurnRequest, AgentTurnResult, EmitEventFn
from .backend_orchestrator import BackendOrchestrator

_logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class _ExecutionSpec:
    thread_target_id: str
    execution_id: str
    prompt: str
    model: Optional[str]
    reasoning: Optional[str]


class DefaultAgentPool:
    """Default ticket-flow adapter backed by orchestration-owned thread targets."""

    def __init__(self, config: Any):
        self._config = config
        self._repo_root = Path(getattr(config, "root", Path.cwd())).resolve()
        self._hub_root = _find_hub_root(self._repo_root)
        self._repo_id = self._resolve_repo_id()
        self._thread_store = PmaThreadStore(self._hub_root)
        self._backend_orchestrator: Optional[Any] = None
        self._execution_emitters: dict[str, Optional[EmitEventFn]] = {}
        self._execution_waiters: dict[str, asyncio.Future[AgentTurnResult]] = {}
        self._thread_workers: dict[str, asyncio.Task[None]] = {}
        self._worker_lock: Optional[asyncio.Lock] = None

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

    def _emit_run_event(
        self,
        event: RunEvent,
        *,
        emit_event: Optional[EmitEventFn],
        turn_id: Optional[str],
    ) -> None:
        if emit_event is None:
            return

        if isinstance(event, OutputDelta):
            if (
                event.delta_type in {"assistant_stream", "assistant_message"}
                and event.content
            ):
                emit_event(
                    FlowEventType.AGENT_STREAM_DELTA,
                    {"delta": event.content, "turn_id": turn_id},
                )
            if (
                event.delta_type
                in {"assistant_stream", "assistant_message", "log_line"}
                and event.content
            ):
                emit_event(
                    FlowEventType.APP_SERVER_EVENT,
                    {
                        "message": {
                            "method": "outputDelta",
                            "params": {
                                "delta": event.content,
                                "deltaType": event.delta_type,
                                "turnId": turn_id,
                            },
                        },
                        "turn_id": turn_id,
                    },
                )
            return

        if isinstance(event, TokenUsage):
            emit_event(
                FlowEventType.TOKEN_USAGE,
                {"usage": event.usage, "turn_id": turn_id},
            )

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
        orchestrator = self._backend_orchestrator
        close_all = getattr(orchestrator, "close_all", None)
        if callable(close_all):
            await close_all()

    def _build_execution_orchestrator(self) -> tuple[Any, bool]:
        orchestrator = self._backend_orchestrator
        if orchestrator is not None:
            return orchestrator, False
        return (
            BackendOrchestrator(
                repo_root=self._repo_root,
                config=self._config,
                notification_handler=None,
                logger=logging.getLogger("codex_autorunner.backend"),
            ),
            True,
        )

    def _resolve_thread_record(self, req: AgentTurnRequest) -> dict[str, Any]:
        workspace_root = req.workspace_root.resolve()
        conversation_id = _normalize_optional_text(req.conversation_id)
        if conversation_id:
            existing = self._thread_store.get_thread(conversation_id)
            if existing is not None:
                same_agent = str(existing.get("agent") or "").strip() == req.agent_id
                same_workspace = str(
                    existing.get("workspace_root") or ""
                ).strip() == str(workspace_root)
                active = str(existing.get("status") or "").strip().lower() == "active"
                if same_agent and same_workspace and active:
                    return existing
                conversation_id = None

        return self._thread_store.create_thread(
            req.agent_id,
            workspace_root,
            repo_id=self._repo_id,
            name=f"ticket-flow:{req.agent_id}",
            backend_thread_id=conversation_id,
        )

    @staticmethod
    def _build_execution_spec(record: dict[str, Any]) -> _ExecutionSpec:
        return _ExecutionSpec(
            thread_target_id=str(record["managed_thread_id"]),
            execution_id=str(record["managed_turn_id"]),
            prompt=str(record.get("prompt") or ""),
            model=_normalize_optional_text(record.get("model")),
            reasoning=_normalize_optional_text(record.get("reasoning")),
        )

    async def _ensure_thread_worker(
        self,
        thread_target_id: str,
        *,
        initial: Optional[_ExecutionSpec] = None,
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
        initial: Optional[_ExecutionSpec],
    ) -> None:
        spec = initial
        try:
            while True:
                if spec is None:
                    claimed = self._thread_store.claim_next_queued_turn(
                        thread_target_id
                    )
                    if claimed is None:
                        break
                    queued_execution, _payload = claimed
                    spec = self._build_execution_spec(queued_execution)
                await self._run_execution_spec(spec)
                spec = None
        finally:
            worker_lock = self._ensure_worker_lock()
            async with worker_lock:
                current = self._thread_workers.get(thread_target_id)
                if current is asyncio.current_task():
                    self._thread_workers.pop(thread_target_id, None)

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

    async def _run_execution_spec(self, spec: _ExecutionSpec) -> None:
        thread = self._thread_store.get_thread(spec.thread_target_id)
        if thread is None:
            self._fail_execution(
                spec.execution_id,
                agent_id="unknown",
                thread_target_id=spec.thread_target_id,
                turn_id=spec.execution_id,
                error=f"Unknown thread target '{spec.thread_target_id}'",
            )
            return
        workspace_raw = _normalize_optional_text(thread.get("workspace_root"))
        if workspace_raw is None:
            self._fail_execution(
                spec.execution_id,
                agent_id=str(thread.get("agent") or "unknown"),
                thread_target_id=spec.thread_target_id,
                turn_id=spec.execution_id,
                error="Thread target is missing workspace_root",
            )
            return

        state = self._ticket_flow_runner_state()
        emitter = self._execution_emitters.get(spec.execution_id)
        backend_thread_id = _normalize_optional_text(thread.get("backend_thread_id"))
        backend_turn_id: Optional[str] = None
        assistant_parts: list[str] = []
        log_lines: list[str] = []
        token_usage: Optional[dict[str, Any]] = None
        final_status = "unknown"
        final_message = ""
        error: Optional[str] = None

        orchestrator, owns_orchestrator = self._build_execution_orchestrator()
        try:
            async for event in orchestrator.run_turn(
                str(thread.get("agent") or ""),
                state,
                spec.prompt,
                model=spec.model,
                reasoning=spec.reasoning,
                session_id=backend_thread_id,
                workspace_root=Path(workspace_raw),
            ):
                if isinstance(event, Started):
                    resolved_session_id = _normalize_optional_text(event.session_id)
                    if resolved_session_id and resolved_session_id != backend_thread_id:
                        backend_thread_id = resolved_session_id
                        self._thread_store.set_thread_backend_id(
                            spec.thread_target_id, resolved_session_id
                        )
                    if event.turn_id:
                        backend_turn_id = event.turn_id
                        self._thread_store.set_turn_backend_turn_id(
                            spec.execution_id,
                            backend_turn_id,
                        )
                elif isinstance(event, OutputDelta):
                    if (
                        event.delta_type in {"assistant_stream", "assistant_message"}
                        and event.content
                    ):
                        assistant_parts.append(event.content)
                    elif event.delta_type == "log_line" and event.content:
                        log_lines.append(event.content)
                elif isinstance(event, TokenUsage):
                    token_usage = event.usage
                elif is_terminal_run_event(event):
                    if isinstance(event, Completed):
                        final_status = "completed"
                        final_message = event.final_message or ""
                    elif isinstance(event, Failed):
                        final_status = "failed"
                        error = event.error_message

                self._emit_run_event(
                    event,
                    emit_event=emitter,
                    turn_id=backend_turn_id or spec.execution_id,
                )
        except Exception as exc:
            final_status = "failed"
            error = str(exc).strip() or "Delegated turn failed"
        finally:
            if owns_orchestrator:
                close_all = getattr(orchestrator, "close_all", None)
                if callable(close_all):
                    await close_all()

        context = getattr(orchestrator, "get_context", lambda: None)()
        context_session_id = _normalize_optional_text(
            getattr(context, "session_id", None) if context is not None else None
        )
        if context_session_id and context_session_id != backend_thread_id:
            backend_thread_id = context_session_id
            self._thread_store.set_thread_backend_id(
                spec.thread_target_id, context_session_id
            )

        if backend_turn_id is None:
            last_turn_id = getattr(orchestrator, "get_last_turn_id", lambda: None)()
            backend_turn_id = (
                _normalize_optional_text(last_turn_id) or spec.execution_id
            )
            self._thread_store.set_turn_backend_turn_id(
                spec.execution_id,
                backend_turn_id,
            )

        text = final_message.strip()
        if not text:
            text = "".join(assistant_parts).strip()

        record_error = error
        if final_status not in {"completed", "ok"} and not record_error:
            record_error = "Delegated turn failed"
        record_status = "ok" if not record_error else "error"

        updated = self._thread_store.mark_turn_finished(
            spec.execution_id,
            status=record_status,
            assistant_text=text,
            error=record_error,
            backend_turn_id=backend_turn_id,
            transcript_turn_id=None,
        )
        if not updated:
            _logger.warning(
                "Failed to mark delegated execution finished: thread=%s execution=%s",
                spec.thread_target_id,
                spec.execution_id,
            )

        self._complete_execution(
            spec.execution_id,
            AgentTurnResult(
                agent_id=str(thread.get("agent") or ""),
                conversation_id=spec.thread_target_id,
                turn_id=backend_turn_id,
                text=text,
                error=record_error,
                raw={
                    "final_status": (
                        "completed" if record_status == "ok" else final_status
                    ),
                    "log_lines": log_lines,
                    "token_usage": token_usage,
                    "execution_id": spec.execution_id,
                    "backend_thread_id": backend_thread_id,
                },
            ),
        )

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

        thread = self._resolve_thread_record(req)
        execution = self._thread_store.create_turn(
            str(thread["managed_thread_id"]),
            prompt=prompt,
            busy_policy="queue",
            model=model,
            reasoning=reasoning,
            client_turn_id=None,
            queue_payload={},
        )
        execution_id = str(execution["managed_turn_id"])
        future: asyncio.Future[AgentTurnResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._execution_waiters[execution_id] = future
        self._execution_emitters[execution_id] = req.emit_event

        if str(execution.get("status") or "") == "running":
            await self._ensure_thread_worker(
                str(thread["managed_thread_id"]),
                initial=self._build_execution_spec(execution),
            )
        else:
            await self._ensure_thread_worker(str(thread["managed_thread_id"]))
        return await future
