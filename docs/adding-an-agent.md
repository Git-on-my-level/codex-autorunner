# Adding a New Agent to Codex Autorunner

This guide explains how to add a new AI agent to Codex Autorunner (CAR).

## Overview

CAR supports multiple AI agents through a registry and capability model. Each agent is integrated via:
- **Harness**: Low-level client wrapper for agent's protocol
- **Supervisor**: Manages agent process lifecycle (for agents that run as subprocesses)
- **Registry**: Central registration with capabilities

**Canonical boundary (Jan 2026):** Agent integrations belong under `src/codex_autorunner/agents/` using the harness + supervisor + registry stack. The experimental `integrations/agents` backend abstraction was removed because it duplicated this seam; avoid adding new agent adapters elsewhere to keep the boundary singular.

## Prerequisites

Before adding a new agent, ensure:
1. The agent binary/CLI is available and callable
2. The agent has a documented protocol or API (JSON-RPC, HTTP, etc.)
3. The agent supports basic operations: conversations, turns, model listing
4. You have tested the agent works independently of CAR

## Step 1: Create the Harness

Create a new module in `src/codex_autorunner/agents/<agent_name>/harness.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..base import AgentHarness
from ..types import AgentId, ConversationRef, ModelCatalog, TurnRef

class MyAgentHarness(AgentHarness):
    agent_id: AgentId = AgentId("myagent")
    display_name = "My Agent"

    def __init__(self, supervisor: Any):
        self._supervisor = supervisor

    async def ensure_ready(self, workspace_root: Path) -> None:
        """Ensure agent is ready to use."""
        await self._supervisor.get_client(workspace_root)

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog:
        """Get available models from the agent."""
        client = await self._supervisor.get_client(workspace_root)
        result = await client.get_models()
        models = [ModelSpec(...) for model in result["models"]]
        return ModelCatalog(default_model=result["default"], models=models)

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        """Create a new conversation/thread."""
        client = await self._supervisor.get_client(workspace_root)
        result = await client.create_conversation(title=title)
        return ConversationRef(agent=self.agent_id, id=result["id"])

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        """List existing conversations."""
        client = await self._supervisor.get_client(workspace_root)
        result = await client.list_conversations()
        return [ConversationRef(agent=self.agent_id, id=c["id"]) for c in result]

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        """Resume an existing conversation."""
        client = await self._supervisor.get_client(workspace_root)
        result = await client.get_conversation(conversation_id)
        return ConversationRef(agent=self.agent_id, id=result["id"])

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> TurnRef:
        """Start a new turn."""
        client = await self._supervisor.get_client(workspace_root)
        result = await client.start_turn(
            conversation_id,
            prompt,
            model=model,
            reasoning=reasoning,
        )
        return TurnRef(conversation_id=conversation_id, turn_id=result["turn_id"])

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> TurnRef:
        """Start a review (if supported)."""
        client = await self._supervisor.get_client(workspace_root)
        result = await client.start_review(conversation_id, prompt)
        return TurnRef(conversation_id=conversation_id, turn_id=result["turn_id"])

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        """Interrupt a running turn."""
        client = await self._supervisor.get_client(workspace_root)
        await client.interrupt_turn(turn_id, conversation_id=conversation_id)

    def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[str]:
        """Stream turn events as SSE-formatted strings."""
        client = self._supervisor.get_client(workspace_root)
        async for event in client.stream_events(conversation_id, turn_id):
            # Format event as SSE: "event: event_type\ndata: {...}\n\n"
            yield format_sse("app-server", event)
```

**Important**: The `AgentHarness` protocol requires all these methods to be implemented.

## Step 2: Create the Supervisor (if subprocess-based)

If your agent runs as a subprocess, create a supervisor in `src/codex_autorunner/agents/<agent_name>/supervisor.py`:

```python
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

@dataclass
class MyAgentHandle:
    workspace_id: str
    workspace_root: Path
    process: Optional[asyncio.subprocess.Process]
    client: Optional[Any]
    start_lock: asyncio.Lock
    started: bool = False
    last_used_at: float = 0.0
    active_turns: int = 0

class MyAgentSupervisor:
    def __init__(
        self,
        command: Sequence[str],
        *,
        logger: Optional[logging.Logger] = None,
        request_timeout: Optional[float] = None,
        max_handles: Optional[int] = None,
        idle_ttl_seconds: Optional[float] = None,
    ):
        self._command = [str(arg) for arg in command]
        self._logger = logger or logging.getLogger(__name__)
        self._request_timeout = request_timeout
        self._max_handles = max_handles
        self._idle_ttl_seconds = idle_ttl_seconds
        self._handles: dict[str, MyAgentHandle] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, workspace_root: Path) -> Any:
        """Get or create a client for the workspace."""
        canonical_root = canonical_workspace_root(workspace_root)
        workspace_id = workspace_id_for_path(canonical_root)
        handle = await self._ensure_handle(workspace_id, canonical_root)
        await self._ensure_started(handle)
        handle.last_used_at = time.monotonic()
        return handle.client

    async def close_all(self) -> None:
        """Close all handles."""
        async with self._lock:
            handles = list(self._handles.values())
            self._handles = {}
        for handle in handles:
            await self._close_handle(handle, reason="close_all")

    # Implement other supervisor methods as needed...
```

Reference existing implementations:
- `src/codex_autorunner/agents/codex/` for JSON-RPC agents
- `src/codex_autorunner/agents/opencode/` for HTTP REST agents

## Step 3: Register the Agent

There are two supported registration paths:

### Option A: In-tree (modify CAR)

## Subagent model configuration (review workloads)

CAR can run review coordinators on one model while spawning cheaper/faster subagents. Configure this in `codex-autorunner.yml`:

```yaml
agents:
  opencode:
    subagent_models:
      subagent: zai-coding-plan/glm-4.7-flashx

repo_defaults:
  review:
    subagent_agent: subagent
    subagent_model: zai-coding-plan/glm-4.7-flashx
```

How it works:
1. CAR ensures `.opencode/agent/subagent.md` exists with the FlashX model before starting review.
2. The review coordinator runs on the full GLM-4.7 model.
3. The coordinator spawns subagents via the `task` tool with `agent="subagent"`, inheriting the configured model.

If you are adding an agent directly to the CAR codebase, register it in:

- `src/codex_autorunner/agents/registry.py` (add to `_BUILTIN_AGENTS`)

Example (in-tree):

```python
# Add import
from .myagent.harness import MyAgentHarness

def _make_myagent_harness(ctx: Any) -> AgentHarness:
    supervisor = ctx.myagent_supervisor
    if supervisor is None:
        raise RuntimeError("MyAgent harness unavailable: supervisor missing")
    return MyAgentHarness(supervisor)

def _check_myagent_health(ctx: Any) -> bool:
    return ctx.myagent_supervisor is not None

# Add to _BUILTIN_AGENTS
_BUILTIN_AGENTS["myagent"] = AgentDescriptor(
    id="myagent",
    name="My Agent",
    capabilities=frozenset([
        "threads",
        "turns",
        "model_listing",
        "event_streaming",
        # Add other capabilities as needed
    ]),
    make_harness=_make_myagent_harness,
    healthcheck=_check_myagent_health,
)
```

### Option B: Out-of-tree plugin (recommended)

This mirrors Takopi’s entrypoint-based plugin approach: publish a Python package
that exposes an `AgentDescriptor` via a standard entry point group.

1) In your plugin package, define an exported descriptor:

```python
# my_package/my_agent_plugin.py
from __future__ import annotations

from codex_autorunner.api import AgentDescriptor, AgentHarness, CAR_PLUGIN_API_VERSION

def _make(ctx: object) -> AgentHarness:
    # construct your harness from ctx (supervisors, settings, etc)
    raise NotImplementedError

AGENT_BACKEND = AgentDescriptor(
    id="myagent",
    name="My Agent",
    capabilities=frozenset(["threads", "turns"]),
    make_harness=_make,
    plugin_api_version=CAR_PLUGIN_API_VERSION,
)
```

2) Declare an entry point in your plugin’s `pyproject.toml`:

```toml
[project.entry-points."codex_autorunner.agent_backends"]
myagent = "my_package.my_agent_plugin:AGENT_BACKEND"
```

At runtime, CAR will discover and load the plugin backend automatically.
Conflicting ids are rejected (plugin ids may not override built-ins).


## Step 4: Add Configuration

Update `src/codex_autorunner/core/config.py` to include your agent in defaults:

```python
DEFAULT_REPO_CONFIG: Dict[str, Any] = {
    # ... existing config ...
    "agents": {
        "codex": {"binary": "codex"},
        "opencode": {"binary": "opencode"},
        "myagent": {"binary": "myagent"},  # ADD THIS
    },
}
```

## Step 5: Add Smoke Tests

Create minimal smoke tests in `tests/test_myagent_integration.py`:

```python
import pytest

@pytest.mark.integration
@pytest.mark.skipif(
    not shutil.which("myagent"),
    reason="myagent binary not found"
)
async def test_myagent_smoke():
    """Test basic agent connectivity without credentials."""
    from codex_autorunner.agents.myagent.harness import MyAgentHarness
    from codex_autorunner.agents.myagent.supervisor import MyAgentSupervisor

    supervisor = MyAgentSupervisor(["myagent", "--server"])
    harness = MyAgentHarness(supervisor)

    try:
        await harness.ensure_ready(Path("/tmp"))
        catalog = await harness.model_catalog(Path("/tmp"))
        assert len(catalog.models) > 0, "Should have at least one model"
        assert catalog.default_model, "Should have a default model"
    finally:
        await supervisor.close_all()
```

## Required Capabilities

All agents should support these core capabilities:

- **`threads`**: List, create, and resume conversations
- **`turns`**: Start and execute turns
- **`model_listing`**: Return available models

Optional capabilities:
- **`review`**: Run code review operations
- **`event_streaming`**: Stream turn events in real-time
- **`approvals`**: Support approval/workflow mechanisms

## Protocol Snapshot Gate (Optional)

If your agent exposes a machine-readable protocol spec:

1. Create a script in `scripts/update_<agent_name>_protocol.py`:
   ```python
   async def main():
       spec = await fetch_agent_protocol()
       path = Path("vendor/protocols/<agent_name>.json")
       path.write_text(json.dumps(spec, indent=2))

   if __name__ == "__main__":
       asyncio.run(main())
   ```

2. Update CI workflow to include your agent in drift checks

3. Document how to update the spec when agent protocol changes

## Testing Checklist

Before submitting, verify:

- [ ] Harness implements all `AgentHarness` protocol methods
- [ ] Agent is registered in registry with correct capabilities
- [ ] Configuration defaults include agent binary path
- [ ] Smoke tests pass (binary present, no credentials required)
- [ ] Full turn tests pass (if credentials available)
- [ ] `/api/agents/<agent_id>/models` returns valid model catalog
- [ ] `/api/agents/<agent_id>/threads` returns conversation list
- [ ] Version info is accessible (if agent supports it)

## Troubleshooting

**"Agent not available" error**:
- Check agent is registered in `registry.py`
- Verify healthcheck returns `True`
- Check config has correct binary path

**"Module not found" error**:
- Add `__init__.py` to agent directory: `src/codex_autorunner/agents/<agent_name>/__init__.py`
- Ensure imports are correct in factory/registry

**Smoke tests fail**:
- Verify binary is accessible (`which myagent`)
- Check binary `--help` or equivalent works
- Review supervisor startup logs

## References

- Existing implementations: `src/codex_autorunner/agents/codex/`, `src/codex_autorunner/agents/opencode/`
- Agent harness protocol: `src/codex_autorunner/agents/base.py`
- Registry: `src/codex_autorunner/agents/registry.py`
