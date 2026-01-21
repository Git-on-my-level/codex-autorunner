from __future__ import annotations

from typing import Optional, cast

from ..core.app_server_events import AppServerEventBuffer
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from .codex.harness import CodexHarness
from .opencode.harness import OpenCodeHarness
from .opencode.supervisor import OpenCodeSupervisor
from .orchestrator import AgentOrchestrator, CodexOrchestrator, OpenCodeOrchestrator
from .registry import get_agent_descriptor


def create_orchestrator(
    agent_id: str,
    codex_supervisor: Optional[WorkspaceAppServerSupervisor] = None,
    codex_events: Optional[AppServerEventBuffer] = None,
    opencode_supervisor: Optional[OpenCodeSupervisor] = None,
) -> AgentOrchestrator:
    descriptor = get_agent_descriptor(agent_id)
    if descriptor is None:
        raise ValueError(f"Unknown agent: {agent_id}")

    class _AppContext:
        def __init__(
            self,
            app_server_supervisor: Optional[WorkspaceAppServerSupervisor] = None,
            app_server_events: Optional[AppServerEventBuffer] = None,
            opencode_supervisor: Optional[OpenCodeSupervisor] = None,
        ):
            self.app_server_supervisor = app_server_supervisor
            self.app_server_events = app_server_events
            self.opencode_supervisor = opencode_supervisor

    app_ctx = _AppContext(codex_supervisor, codex_events, opencode_supervisor)
    harness = descriptor.make_harness(app_ctx)

    if agent_id == "codex":
        if not isinstance(harness, CodexHarness):
            raise RuntimeError(f"Expected CodexHarness but got {type(harness)}")
        return CodexOrchestrator(harness, cast(AppServerEventBuffer, codex_events))
    elif agent_id == "opencode":
        if not isinstance(harness, OpenCodeHarness):
            raise RuntimeError(f"Expected OpenCodeHarness but got {type(harness)}")
        return OpenCodeOrchestrator(harness)
    else:
        raise RuntimeError(f"No orchestrator implementation for agent: {agent_id}")


__all__ = [
    "create_orchestrator",
]
