from __future__ import annotations

from typing import Optional

from ..core.app_server_events import AppServerEventBuffer
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from .codex.harness import CodexHarness
from .opencode.harness import OpenCodeHarness
from .opencode.supervisor import OpenCodeSupervisor
from .orchestrator import AgentOrchestrator, CodexOrchestrator, OpenCodeOrchestrator


def create_codex_orchestrator(
    supervisor: WorkspaceAppServerSupervisor,
    events: AppServerEventBuffer,
) -> CodexOrchestrator:
    harness = CodexHarness(supervisor, events)
    return CodexOrchestrator(harness, events)


def create_opencode_orchestrator(
    supervisor: OpenCodeSupervisor,
) -> OpenCodeOrchestrator:
    harness = OpenCodeHarness(supervisor)
    return OpenCodeOrchestrator(harness)


def create_orchestrator(
    agent_id: str,
    codex_supervisor: Optional[WorkspaceAppServerSupervisor] = None,
    codex_events: Optional[AppServerEventBuffer] = None,
    opencode_supervisor: Optional[OpenCodeSupervisor] = None,
) -> AgentOrchestrator:
    if agent_id == "opencode":
        if opencode_supervisor is None:
            raise ValueError("opencode_supervisor required for opencode agent")
        return create_opencode_orchestrator(opencode_supervisor)
    else:
        if codex_supervisor is None or codex_events is None:
            raise ValueError(
                "codex_supervisor and codex_events required for codex agent"
            )
        return create_codex_orchestrator(codex_supervisor, codex_events)


__all__ = [
    "create_codex_orchestrator",
    "create_opencode_orchestrator",
    "create_orchestrator",
]
