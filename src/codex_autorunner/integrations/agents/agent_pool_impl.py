from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from ...core.flows.models import FlowEventType
from ...core.ports.run_event import (
    Completed,
    OutputDelta,
    RunEvent,
    Started,
    TokenUsage,
    is_terminal_run_event,
)
from ...core.state import RunnerState
from ...tickets.agent_pool import AgentTurnRequest, AgentTurnResult, EmitEventFn
from ..app_server.ids import extract_turn_id
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


class DefaultAgentPool:
    """Default ticket-flow adapter backed by BackendOrchestrator."""

    def __init__(self, config: Any):
        self._config = config
        repo_root = Path(getattr(config, "root", Path.cwd()))
        self._current_emitter: Optional[EmitEventFn] = None
        self._backend_orchestrator = BackendOrchestrator(
            repo_root=repo_root,
            config=config,
            notification_handler=self._handle_backend_notification,
            logger=logging.getLogger("codex_autorunner.backend"),
        )

    async def _handle_backend_notification(self, message: dict[str, Any]) -> None:
        emitter = self._current_emitter
        if emitter is None:
            return
        turn_id = extract_turn_id(message.get("params"))
        try:
            emitter(
                FlowEventType.APP_SERVER_EVENT,
                {"message": message, "turn_id": turn_id},
            )
        except Exception:
            _logger.exception("Failed emitting backend notification")

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
            if event.delta_type == "assistant_stream" and event.content:
                emit_event(
                    FlowEventType.AGENT_STREAM_DELTA,
                    {"delta": event.content, "turn_id": turn_id},
                )
            if event.delta_type in {"assistant_stream", "log_line"} and event.content:
                emit_event(
                    FlowEventType.APP_SERVER_EVENT,
                    {
                        "message": {
                            "method": "outputDelta",
                            "params": {"delta": event.content, "turnId": turn_id},
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
        await self._backend_orchestrator.close_all()

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
        conversation_id = req.conversation_id or ""
        turn_id = ""
        assistant_parts: list[str] = []
        log_lines: list[str] = []
        token_usage: Optional[dict[str, Any]] = None
        final_status = "unknown"
        final_message = ""
        error: Optional[str] = None

        self._current_emitter = req.emit_event
        try:
            async for event in self._backend_orchestrator.run_turn(
                req.agent_id,
                state,
                prompt,
                model=model,
                reasoning=reasoning,
                session_id=req.conversation_id,
                workspace_root=req.workspace_root,
            ):
                if isinstance(event, Started):
                    conversation_id = event.session_id or conversation_id
                    if event.turn_id:
                        turn_id = event.turn_id
                elif isinstance(event, OutputDelta):
                    if event.delta_type == "assistant_stream" and event.content:
                        assistant_parts.append(event.content)
                    elif event.delta_type == "log_line" and event.content:
                        log_lines.append(event.content)
                elif isinstance(event, TokenUsage):
                    token_usage = event.usage
                elif is_terminal_run_event(event):
                    if isinstance(event, Completed):
                        final_status = "completed"
                        final_message = event.final_message or ""
                    else:
                        final_status = "failed"
                        error = event.error_message

                self._emit_run_event(
                    event,
                    emit_event=req.emit_event,
                    turn_id=turn_id or None,
                )
        finally:
            self._current_emitter = None

        context = self._backend_orchestrator.get_context()
        if context and context.session_id:
            conversation_id = context.session_id

        if not turn_id:
            turn_id = self._backend_orchestrator.get_last_turn_id() or str(uuid.uuid4())

        text = final_message.strip()
        if not text:
            text = "".join(assistant_parts).strip()

        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            text=text,
            error=error,
            raw={
                "final_status": final_status,
                "log_lines": log_lines,
                "token_usage": token_usage,
            },
        )
