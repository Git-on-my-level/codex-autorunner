from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from ..agents.opencode.runtime import collect_opencode_output
from ..agents.opencode.supervisor import OpenCodeSupervisor
from ..core.config import RepoConfig
from ..core.utils import build_opencode_supervisor
from ..integrations.app_server.client import CodexAppServerClient
from ..integrations.app_server.env import build_app_server_env
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentTurnRequest:
    agent_id: str  # "codex" | "opencode"
    prompt: str
    workspace_root: Path
    conversation_id: Optional[str] = None
    # Optional, agent-specific extras.
    options: Optional[dict[str, Any]] = None


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
            notification_handler=None,
            max_handles=app_server_cfg.max_handles,
            idle_ttl_seconds=app_server_cfg.idle_ttl_seconds,
            request_timeout=app_server_cfg.request_timeout,
            turn_stall_timeout_seconds=app_server_cfg.turn_stall_timeout_seconds,
            turn_stall_poll_interval_seconds=app_server_cfg.turn_stall_poll_interval_seconds,
            turn_stall_recovery_min_interval_seconds=app_server_cfg.turn_stall_recovery_min_interval_seconds,
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
        client: CodexAppServerClient = handle.client

        approval_mode = (
            cast(dict[str, Any], getattr(self._config, "ticket_flow", {})).get(
                "approval_mode", "yolo"
            )
            or "yolo"
        ).strip()
        approval_policy = "never" if approval_mode == "yolo" else "on-request"
        sandbox = "workspace-write"

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

        turn_handle = await client.turn_start(thread_id, message=req.prompt)
        result = await turn_handle.wait()
        text = "\n\n".join(result.agent_messages or []).strip()
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=thread_id,
            turn_id=result.turn_id,
            text=text,
            error=result.error,
            raw={
                "status": result.status,
                "duration_seconds": result.duration_seconds,
                "usage": result.token_usage,
            },
        )

    async def _run_opencode_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        supervisor = self._ensure_opencode_supervisor()
        handle = await supervisor.get_client(req.workspace_root)
        client = handle.client
        directory = str(req.workspace_root)

        session_id = req.conversation_id
        if not session_id:
            created = await client.create_session(title="ticket", directory=directory)
            session_id = created.get("id") or created.get("session", {}).get("id")
            if not session_id:
                raise RuntimeError("OpenCode create_session returned no session id")

        prompt_response = await client.prompt_async(
            session_id, req.prompt, directory=directory
        )
        output = await collect_opencode_output(
            client,
            session_id,
            prompt_response,
            directory=directory,
            logger=_logger,
            event_prefix="tickets",
        )
        if output.error:
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id=session_id,
                turn_id=str(
                    prompt_response.get("id")
                    if isinstance(prompt_response, dict)
                    else ""
                ),
                text=output.text,
                error=output.error,
            )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=session_id,
            turn_id=str(
                prompt_response.get("id") if isinstance(prompt_response, dict) else ""
            ),
            text=output.text,
        )
