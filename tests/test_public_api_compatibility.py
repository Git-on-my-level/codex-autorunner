from __future__ import annotations

from codex_autorunner import agents
from codex_autorunner.api import AgentCapability, RuntimeCapability


def test_public_api_preserves_agent_capability_alias() -> None:
    assert AgentCapability is RuntimeCapability
    assert agents.AgentCapability is RuntimeCapability
