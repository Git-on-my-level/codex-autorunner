from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, cast

from ..agents.opencode.runtime import collect_opencode_output
from ..agents.opencode.supervisor import OpenCodeSupervisor
from ..core.config import RepoConfig
from ..core.flows.models import FlowEventType
from ..core.utils import build_opencode_supervisor
from ..integrations.app_server.client import CodexAppServerClient
from ..integrations.app_server.env import build_app_server_env
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor

_logger = logging.getLogger(__name__)

EmitEventFn = Callable[[FlowEventType, dict[str, Any]], None]


@dataclass(frozen=True)
class AgentTurnRequest:
    agent_id: str  # "codex" | "opencode"
    prompt: str
    workspace_root: Path
    conversation_id: Optional[str] = None
    # Optional, agent-specific extras.
    options: Optional[dict[str, Any]] = None
    # Optional flow event emitter (for live streaming).
    emit_event: Optional[EmitEventFn] = None


@dataclass(frozen=True)
class AgentTurnResult:
    agent_id: str
    conversation_id: str
    turn_id: str
    text: str
    error: Optional[str] = None
    raw: Optional[dict[str, Any]] = None


class AgentPool:
    """Minimal agent execution facade.

    The pool is intentionally small: it can run either the Codex app-server or
    OpenCode server for a single prompt.
    """

    def __init__(self, config: RepoConfig):
        self._config = config
        self._app_server_supervisor: Optional[WorkspaceAppServerSupervisor] = None
        self._opencode_supervisor: Optional[OpenCodeSupervisor] = None
        self._active_emitters: dict[str, EmitEventFn] = {}

    @staticmethod
    def _extract_turn_id(params: Any) -> Optional[str]:
        if not isinstance(params, dict):
            return None
        for key in ("turnId", "turn_id", "id"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        turn = params.get("turn")
        if isinstance(turn, dict):
            for key in ("turnId", "turn_id", "id"):
                value = turn.get(key)
                if isinstance(value, str) and value:
                    return value
        item = params.get("item")
        if isinstance(item, dict):
            for key in ("turnId", "turn_id", "id"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    async def _handle_app_server_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        turn_id = self._extract_turn_id(params)
        if not turn_id:
            return
        emitter = self._active_emitters.get(turn_id)
        if emitter is None:
            return

        # Forward the raw app-server event for richer UI rendering (tools, files, commands, etc.)
        try:
            emitter(
                FlowEventType.APP_SERVER_EVENT,
                {"message": message, "turn_id": turn_id},
            )
        except Exception:
            _logger.exception("Failed emitting app-server event for turn %s", turn_id)

        if method in ("item/agentMessage/delta", "turn/streamDelta"):
            delta = None
            if isinstance(params, dict):
                raw = params.get("delta") or params.get("text")
                if isinstance(raw, str):
                    delta = raw
            if delta:
                emitter(
                    FlowEventType.AGENT_STREAM_DELTA,
                    {"delta": delta, "turn_id": turn_id, "method": method},
                )

    def _ensure_app_server_supervisor(self) -> WorkspaceAppServerSupervisor:
        if self._app_server_supervisor is not None:
            return self._app_server_supervisor

        app_server_cfg = self._config.app_server
        ticket_flow_cfg = cast(dict[str, Any], getattr(self._config, "ticket_flow", {}))
        default_approval_decision = ticket_flow_cfg.get(
            "default_approval_decision", "accept"
        )

        def _env_builder(
            workspace_root: Path, workspace_id: str, state_dir: Path
        ) -> dict[str, str]:
            # env is deterministic and purely derived from workspace/state dirs.
            return build_app_server_env(
                command=app_server_cfg.command,
                workspace_root=workspace_root,
                state_dir=state_dir,
                logger=logging.getLogger("codex_autorunner.app_server"),
                event_prefix=f"tickets.{workspace_id}",
                base_env=None,
            )

        # Default approval decision is "accept" to keep the loop KISS.
        self._app_server_supervisor = WorkspaceAppServerSupervisor(
            app_server_cfg.command,
            state_root=app_server_cfg.state_root,
            env_builder=_env_builder,
            logger=logging.getLogger("codex_autorunner.app_server"),
            notification_handler=self._handle_app_server_notification,
            auto_restart=app_server_cfg.auto_restart,
            max_handles=app_server_cfg.max_handles,
            idle_ttl_seconds=app_server_cfg.idle_ttl_seconds,
            request_timeout=app_server_cfg.request_timeout,
            turn_stall_timeout_seconds=app_server_cfg.turn_stall_timeout_seconds,
            turn_stall_poll_interval_seconds=app_server_cfg.turn_stall_poll_interval_seconds,
            turn_stall_recovery_min_interval_seconds=app_server_cfg.turn_stall_recovery_min_interval_seconds,
            max_message_bytes=app_server_cfg.client.max_message_bytes,
            oversize_preview_bytes=app_server_cfg.client.oversize_preview_bytes,
            max_oversize_drain_bytes=app_server_cfg.client.max_oversize_drain_bytes,
            restart_backoff_initial_seconds=app_server_cfg.client.restart_backoff_initial_seconds,
            restart_backoff_max_seconds=app_server_cfg.client.restart_backoff_max_seconds,
            restart_backoff_jitter_ratio=app_server_cfg.client.restart_backoff_jitter_ratio,
            default_approval_decision=default_approval_decision,
        )
        return self._app_server_supervisor

    def _ensure_opencode_supervisor(self) -> OpenCodeSupervisor:
        if self._opencode_supervisor is not None:
            return self._opencode_supervisor

        app_server_cfg = self._config.app_server
        opencode_command = self._config.agent_serve_command("opencode")
        opencode_binary = None
        try:
            opencode_binary = self._config.agent_binary("opencode")
        except Exception:
            opencode_binary = None

        agent_cfg = self._config.agents.get("opencode")
        subagent_models = agent_cfg.subagent_models if agent_cfg else None

        supervisor = build_opencode_supervisor(
            opencode_command=opencode_command,
            opencode_binary=opencode_binary,
            workspace_root=self._config.root,
            logger=logging.getLogger("codex_autorunner.opencode"),
            request_timeout=app_server_cfg.request_timeout,
            max_handles=app_server_cfg.max_handles,
            idle_ttl_seconds=app_server_cfg.idle_ttl_seconds,
            session_stall_timeout_seconds=self._config.opencode.session_stall_timeout_seconds,
            base_env=None,
            subagent_models=subagent_models,
        )
        if supervisor is None:
            raise RuntimeError(
                "OpenCode supervisor unavailable (missing opencode command/binary)."
            )
        self._opencode_supervisor = supervisor
        return supervisor

    async def close(self) -> None:
        if self._app_server_supervisor is not None:
            try:
                await self._app_server_supervisor.close_all()
            except Exception:
                _logger.exception("Failed closing app-server supervisor")
            self._app_server_supervisor = None
        if self._opencode_supervisor is not None:
            try:
                await self._opencode_supervisor.close_all()
            except Exception:
                _logger.exception("Failed closing opencode supervisor")
            self._opencode_supervisor = None

    async def run_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        if req.agent_id == "codex":
            return await self._run_codex_turn(req)
        if req.agent_id == "opencode":
            return await self._run_opencode_turn(req)
        raise ValueError(f"Unsupported agent_id: {req.agent_id}")

    async def _run_codex_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        supervisor = self._ensure_app_server_supervisor()
        handle = await supervisor.get_client(req.workspace_root)
        client: CodexAppServerClient = handle

        approval_mode = (
            cast(dict[str, Any], getattr(self._config, "ticket_flow", {})).get(
                "approval_mode", "yolo"
            )
            or "yolo"
        ).strip()
        approval_policy = "never" if approval_mode == "yolo" else "on-request"
        sandbox = "danger-full-access" if approval_mode == "yolo" else "workspace-write"

        thread_id = req.conversation_id
        if thread_id:
            await client.thread_resume(thread_id)
        else:
            thread = await client.thread_start(
                cwd=str(req.workspace_root),
                approvalPolicy=approval_policy,
                sandbox=sandbox,
            )
            thread_id = thread.get("id") or thread.get("thread", {}).get("id")
            if not thread_id:
                raise RuntimeError("Codex thread_start returned no thread id")

        _logger.info(
            "Starting turn for thread %s with prompt length %d",
            thread_id,
            len(req.prompt),
        )
        # Extract model/reasoning from options if provided.
        turn_kwargs: dict[str, Any] = {}
        if req.options:
            if req.options.get("model"):
                turn_kwargs["model"] = req.options["model"]
            if req.options.get("reasoning"):
                turn_kwargs["effort"] = req.options["reasoning"]
        turn_handle = await client.turn_start(thread_id, req.prompt, **turn_kwargs)
        if req.emit_event is not None:
            self._active_emitters[turn_handle.turn_id] = req.emit_event
        try:
            result = await turn_handle.wait()
        finally:
            if req.emit_event is not None:
                self._active_emitters.pop(turn_handle.turn_id, None)
        text = "\n\n".join(result.agent_messages or []).strip()
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=thread_id,
            turn_id=result.turn_id,
            text=text,
            error=result.errors[0] if result.errors else None,
            raw={
                "status": result.status,
            },
        )

    async def _run_opencode_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        supervisor = self._ensure_opencode_supervisor()
        handle = await supervisor.get_client(req.workspace_root)
        client = handle
        directory = str(req.workspace_root)

        session_id = req.conversation_id
        if not session_id:
            created = await client.create_session(title="ticket", directory=directory)
            session_id = created.get("id") or created.get("session", {}).get("id")
            if not session_id:
                raise RuntimeError("OpenCode create_session returned no session id")

        prompt_response = await client.prompt_async(session_id, message=req.prompt)

        import uuid

        turn_id = str(
            prompt_response.get("id") if isinstance(prompt_response, dict) else ""
        )
        if not turn_id:
            turn_id = str(uuid.uuid4())
        text_item_id = f"text-{turn_id}"

        async def _part_handler(
            part_type: str, part: dict[str, Any], delta: Optional[str]
        ) -> None:
            if req.emit_event is None:
                return
            if part_type == "text" and isinstance(delta, str) and delta:
                req.emit_event(
                    FlowEventType.AGENT_STREAM_DELTA,
                    {"delta": delta, "turn_id": turn_id, "part_type": part_type},
                )
                # Also emit app-server event for summary view
                message = {
                    "method": "outputDelta",
                    "params": {
                        "delta": delta,
                        "turnId": turn_id,
                        "itemId": text_item_id,
                    },
                }
                req.emit_event(
                    FlowEventType.APP_SERVER_EVENT,
                    {"message": message, "turn_id": turn_id},
                )
            elif part_type == "reasoning" and isinstance(delta, str) and delta:
                # Emit reasoning as app-server event for summary view
                # Use item/reasoning/summaryTextDelta for merging behavior
                message = {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {
                        "delta": delta,
                        "turnId": turn_id,
                        "itemId": f"reasoning-{turn_id}",
                    },
                }
                req.emit_event(
                    FlowEventType.APP_SERVER_EVENT,
                    {"message": message, "turn_id": turn_id},
                )
            elif part_type == "usage":
                req.emit_event(
                    FlowEventType.TOKEN_USAGE,
                    {"usage": part, "turn_id": turn_id},
                )

        output = await collect_opencode_output(
            client,
            session_id=session_id,
            workspace_path=directory,
            part_handler=_part_handler if req.emit_event is not None else None,
        )

        if req.emit_event is not None and output.text:
            # Emit item/completed for the full text to ensure final state is correct
            message = {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "text": output.text,
                        "id": text_item_id,
                    },
                    "turnId": turn_id,
                },
            }
            req.emit_event(
                FlowEventType.APP_SERVER_EVENT,
                {"message": message, "turn_id": turn_id},
            )

        if output.error:
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id=session_id,
                turn_id=turn_id,
                text=output.text,
                error=output.error,
            )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=session_id,
            turn_id=turn_id,
            text=output.text,
        )
