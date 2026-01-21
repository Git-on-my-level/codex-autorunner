# Adding a New Agent to Codex Autorunner

This guide explains how to add a new AI agent to Codex Autorunner (CAR).

## Overview

CAR supports multiple AI agents through a registry and capability model. Each agent is integrated via:
- **Harness**: Low-level client wrapper for agent's protocol
- **Supervisor**: Manages agent process lifecycle (for agents that run as subprocesses)
- **Orchestrator**: High-level workflow operations (turn execution, streaming, etc.)
- **Registry**: Central registration with capabilities

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

## Step 3: Create the Orchestrator

Create an orchestrator in `src/codex_autorunner/agents/<agent_name>/orchestrator.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..orchestrator import AgentOrchestrator, TurnStatus
from .harness import MyAgentHarness

class MyAgentOrchestrator(AgentOrchestrator):
    def __init__(self, harness: MyAgentHarness):
        self._harness = harness

    async def create_or_resume_conversation(
        self,
        workspace_root: Path,
        agent_id: str,
        *,
        conversation_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> ConversationRef:
        if conversation_id:
            return await self._harness.resume_conversation(workspace_root, conversation_id)
        return await self._harness.new_conversation(workspace_root, title)

    async def run_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        turn_ref = await self._harness.start_turn(
            workspace_root, conversation_id, prompt, model, reasoning,
            approval_mode=approval_mode, sandbox_policy=sandbox_policy,
        )

        # Collect output from events
        output_lines = []
        async for event in self._harness.stream_events(
            workspace_root, turn_ref.conversation_id, turn_ref.turn_id
        ):
            # Parse event and extract output
            output_lines.append(event.get("output", ""))

        return {
            "turn_id": turn_ref.turn_id,
            "conversation_id": turn_ref.conversation_id,
            "status": TurnStatus.COMPLETED,
            "output": "\n".join(output_lines),
        }

    async def stream_turn_events(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        turn_ref = await self._harness.start_turn(
            workspace_root, conversation_id, prompt, model, reasoning,
            approval_mode=approval_mode, sandbox_policy=sandbox_policy,
        )
        yield {"type": "turn_started", "data": {"turn_id": turn_ref.turn_id}}

        async for event in self._harness.stream_events(
            workspace_root, turn_ref.conversation_id, turn_ref.turn_id
        ):
            yield {"type": "event", "data": event}

        yield {"type": "turn_completed", "data": {"turn_id": turn_ref.turn_id}}

    async def interrupt_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str] = None,
        grace_seconds: float = 30.0,
    ) -> bool:
        await self._harness.interrupt(workspace_root, conversation_id, turn_id)
        return True

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: Optional[str] = None,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
    ) -> TurnRef:
        return await self._harness.start_review(
            workspace_root, conversation_id, prompt or "",
            model, reasoning,
            approval_mode=approval_mode, sandbox_policy=sandbox_policy,
        )
```

## Step 4: Register the Agent

Add your agent to `src/codex_autorunner/agents/registry.py`:

```python
# Add import
from .myagent.harness import MyAgentHarness
from .myagent.supervisor import MyAgentSupervisor

def _make_myagent_harness(ctx: Any) -> AgentHarness:
    supervisor = ctx.myagent_supervisor
    if supervisor is None:
        raise RuntimeError("MyAgent harness unavailable: supervisor missing")
    return MyAgentHarness(supervisor)

def _check_myagent_health(ctx: Any) -> bool:
    supervisor = ctx.myagent_supervisor
    return supervisor is not None

# Add to _REGISTERED_AGENTS
_REGISTERED_AGENTS: dict[str, AgentDescriptor] = {
    # ... existing agents ...
    "myagent": AgentDescriptor(
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
    ),
}
```

## Step 5: Add Configuration

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

## Step 6: Add Factory Support (Optional)

Update `src/codex_autorunner/agents/factory.py`:

```python
from .myagent.supervisor import MyAgentSupervisor

def create_myagent_orchestrator(
    supervisor: MyAgentSupervisor
) -> MyAgentOrchestrator:
    harness = MyAgentHarness(supervisor)
    return MyAgentOrchestrator(harness)

def create_orchestrator(
    agent_id: str,
    # ... other params ...
    myagent_supervisor: Optional[MyAgentSupervisor] = None,
) -> AgentOrchestrator:
    if agent_id == "myagent":
        if myagent_supervisor is None:
            raise ValueError("myagent_supervisor required for myagent agent")
        return create_myagent_orchestrator(myagent_supervisor)
    # ... existing logic ...
```

## Step 7: Add Smoke Tests

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
- [ ] Orchestrator implements all `AgentOrchestrator` methods
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
- Orchestrator base class: `src/codex_autorunner/agents/orchestrator.py`
- Registry: `src/codex_autorunner/agents/registry.py`
