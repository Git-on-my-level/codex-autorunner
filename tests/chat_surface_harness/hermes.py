from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import codex_autorunner.agents.registry as registry_module
from codex_autorunner.agents.hermes.harness import HermesHarness
from codex_autorunner.agents.hermes.supervisor import HermesSupervisor
from codex_autorunner.agents.registry import AgentDescriptor

FAKE_ACP_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "fake_acp_server.py"
)
SESSION_STATUS_IDLE_COMPLETION_GAP = "session_status_idle_completion_gap"


def fake_acp_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FAKE_ACP_FIXTURE_PATH), "--scenario", scenario]


@dataclass
class HermesFixtureRuntime:
    harness: HermesHarness
    supervisor: HermesSupervisor

    async def close(self) -> None:
        await self.supervisor.close_all()


def patch_hermes_registry(
    monkeypatch: Any,
    *,
    scenario: str,
    targets: Sequence[Any],
) -> HermesFixtureRuntime:
    supervisor = HermesSupervisor(fake_acp_command(scenario))
    harness = HermesHarness(supervisor)
    descriptor = AgentDescriptor(
        id="hermes",
        name="Hermes",
        capabilities=harness.capabilities,
        make_harness=lambda _ctx: harness,
    )

    def _registry(_context: Any = None) -> dict[str, AgentDescriptor]:
        return {"hermes": descriptor}

    monkeypatch.setattr(registry_module, "get_registered_agents", _registry)
    for target in targets:
        monkeypatch.setattr(target, "get_registered_agents", _registry)
    return HermesFixtureRuntime(harness=harness, supervisor=supervisor)


def logger_for(name: str) -> logging.Logger:
    return logging.getLogger(f"test.chat_surface_harness.{name}")
