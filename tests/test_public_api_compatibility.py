from __future__ import annotations

import codex_autorunner.agents as agents
import codex_autorunner.api as api


def test_public_api_exposes_runtime_capability_only() -> None:
    assert hasattr(api, "RuntimeCapability") is True
    assert hasattr(api, "AgentCapability") is False
    assert hasattr(agents, "RuntimeCapability") is True
    assert hasattr(agents, "AgentCapability") is False
