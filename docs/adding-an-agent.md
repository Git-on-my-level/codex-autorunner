# Adding a New Agent to Codex Autorunner

This guide explains how to implement and wire a new agent backend in CAR.
`docs/plugin-api.md` is the canonical spec for the public plugin contract.

## Overview

CAR supports multiple AI agents through a registry and capability model. Each
agent integration typically includes:

- **Harness**: low-level client wrapper for the agent protocol.
- **Supervisor**: process or client lifecycle manager when the runtime is not
  purely in-process.
- **Registry/config wiring**: in-tree registration and config defaults when you
  are modifying CAR itself.

Reference points in-tree today:

- **Codex**: full-featured repo/worktree runtime.
- **OpenCode**: full-featured repo/worktree runtime without approvals.
- **Hermes**: ACP-backed repo/worktree runtime with durable threads,
  approvals, and event streaming.
- **ZeroClaw**: narrower `agent_workspace` runtime with detect-only durability
  requirements.

## Choose The Right Resource Model

Decide what CAR is managing before you start coding. For the contract and
resource-model rules, see [plugin-api.md](./plugin-api.md#choose-the-right-car-resource)
and [plugin-api.md](./plugin-api.md#durable-thread-contract).

Hermes is the reference example of a repo/worktree-backed runtime that exposes
its own durable thread API. ZeroClaw is the reference example of the narrower
`agent_workspace` path.

## Prerequisites

Before adding a new agent, make sure:

1. The agent binary or API is available and callable.
2. The agent satisfies CAR's durable-thread contract, or CAR can prove the
   narrower first-class CAR-managed `agent_workspace` contract.
3. You have validated the runtime outside CAR first.

For the contract details and capability vocabulary, use
[plugin-api.md](./plugin-api.md#durable-thread-contract),
[plugin-api.md](./plugin-api.md#single-session-runtimes-out-of-scope), and
[plugin-api.md](./plugin-api.md#capability-model).

## Step 1: Create the Harness

Create a new module in `src/codex_autorunner/agents/<agent_name>/harness.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..base import AgentHarness
from ..types import AgentId, ConversationRef, ModelCatalog, ModelSpec, TurnRef


class MyAgentHarness(AgentHarness):
    agent_id: AgentId = AgentId("myagent")
    display_name = "My Agent"

    def __init__(self, supervisor: Any):
        self._supervisor = supervisor

    async def ensure_ready(self, workspace_root: Path) -> None:
        await self._supervisor.get_client(workspace_root)

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog:
        client = await self._supervisor.get_client(workspace_root)
        result = await client.get_models()
        models = [ModelSpec(...) for model in result["models"]]
        return ModelCatalog(default_model=result["default"], models=models)

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        client = await self._supervisor.get_client(workspace_root)
        result = await client.create_conversation(title=title)
        return ConversationRef(agent=self.agent_id, id=result["id"])

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        client = await self._supervisor.get_client(workspace_root)
        result = await client.list_conversations()
        return [ConversationRef(agent=self.agent_id, id=item["id"]) for item in result]

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
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
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> TurnRef:
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
        client = await self._supervisor.get_client(workspace_root)
        result = await client.start_review(conversation_id, prompt)
        return TurnRef(conversation_id=conversation_id, turn_id=result["turn_id"])

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        client = await self._supervisor.get_client(workspace_root)
        await client.interrupt_turn(turn_id, conversation_id=conversation_id)

    def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[str]:
        client = self._supervisor.get_client(workspace_root)
        async for event in client.stream_events(conversation_id, turn_id):
            yield event
```

For the required contract surface and capability names, use
[plugin-api.md](./plugin-api.md#durable-thread-contract) and
[plugin-api.md](./plugin-api.md#capability-model).

## Step 2: Create the Supervisor

If your agent runs as a subprocess, create a supervisor in
`src/codex_autorunner/agents/<agent_name>/supervisor.py`:

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
        canonical_root = workspace_root.resolve()
        workspace_id = canonical_root.name
        handle = await self._ensure_handle(workspace_id, canonical_root)
        await self._ensure_started(handle)
        handle.last_used_at = time.monotonic()
        return handle.client

    async def close_all(self) -> None:
        async with self._lock:
            handles = list(self._handles.values())
            self._handles = {}
        for handle in handles:
            await self._close_handle(handle, reason="close_all")
```

Reference existing implementations:

- `src/codex_autorunner/agents/codex/` for JSON-RPC agents.
- `src/codex_autorunner/agents/opencode/` for HTTP/SSE agents.
- `src/codex_autorunner/agents/hermes/` for ACP-backed runtime wiring.

## Step 3: Register the Agent

### Option A: In-tree (modify CAR)

Register the harness in `src/codex_autorunner/agents/registry.py` and add any
needed imports and health checks:

```python
from .myagent.harness import MyAgentHarness


def _make_myagent_harness(ctx: Any) -> AgentHarness:
    supervisor = ctx.myagent_supervisor
    if supervisor is None:
        raise RuntimeError("MyAgent harness unavailable: supervisor missing")
    return MyAgentHarness(supervisor)


def _check_myagent_health(ctx: Any) -> bool:
    return ctx.myagent_supervisor is not None


_BUILTIN_AGENTS["myagent"] = AgentDescriptor(
    id="myagent",
    name="My Agent",
    capabilities=frozenset(
        [
            "durable_threads",
            "message_turns",
            "model_listing",
            "event_streaming",
        ]
    ),
    make_harness=_make_myagent_harness,
    healthcheck=_check_myagent_health,
)
```

### Option B: Out-of-tree plugin

For the canonical entry-point contract, required `AgentDescriptor` fields, and
versioning rules, use [plugin-api.md](./plugin-api.md#agent-backend-entry-point)
and [plugin-api.md](./plugin-api.md#versioning).

## Step 4: Add Configuration

If you are changing CAR itself, update
`src/codex_autorunner/core/config.py` so the agent has a default binary entry:

```python
DEFAULT_REPO_CONFIG: Dict[str, Any] = {
    "agents": {
        "codex": {"binary": "codex"},
        "opencode": {"binary": "opencode"},
        "zeroclaw": {"binary": "zeroclaw"},
        "hermes": {"binary": "hermes"},
        "myagent": {"binary": "myagent"},
    },
}
```

## Step 5: Add Smoke Tests

Create minimal smoke tests in `tests/test_myagent_integration.py`:

```python
import shutil
from pathlib import Path

import pytest


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("myagent"), reason="myagent binary not found")
async def test_myagent_smoke() -> None:
    from codex_autorunner.agents.myagent.harness import MyAgentHarness
    from codex_autorunner.agents.myagent.supervisor import MyAgentSupervisor

    supervisor = MyAgentSupervisor(["myagent", "--server"])
    harness = MyAgentHarness(supervisor)

    try:
        await harness.ensure_ready(Path("/tmp"))
        assert await harness.new_conversation(Path("/tmp"))
        if harness.supports("model_listing"):
            catalog = await harness.model_catalog(Path("/tmp"))
            assert len(catalog.models) > 0
    finally:
        await supervisor.close_all()
```

## Protocol Snapshot Gate (Optional)

If your agent exposes a machine-readable protocol spec:

1. Create a refresh script such as `scripts/update_<agent_name>_protocol.py`.
2. Add CI coverage for protocol drift.
3. Document the refresh command and the check command together.

The current `agent-compatibility` flow is the reference pattern:

- `make agent-compatibility-refresh`
- `make agent-compatibility-check`

## ACP Integration Documentation Checklist

For ACP-backed runtimes, do not stop at harness and supervisor wiring.

- **Architecture contract**: capture capability boundaries and non-goals before
  implementation if the runtime introduces a new resource model or partial
  capability surface.
- **Operator runbook**: add `docs/ops/<agent>-acp.md` covering prerequisites,
  launch contract, config, shared-state model, supported and unsupported
  capabilities, PMA/chat/ticket-flow usage, and troubleshooting.
- **README**: update supported-agent lists and any PMA/workflow guidance that
  should call out the new runtime.
- **Base setup guide**: update `docs/AGENT_SETUP_GUIDE.md` so the runtime is
  discoverable from the default onboarding path.
- **Surface setup guides**: update `docs/AGENT_SETUP_TELEGRAM_GUIDE.md` and
  `docs/AGENT_SETUP_DISCORD_GUIDE.md` when those surfaces can route to the new
  agent.
- **Capability/reference docs**: update broader reference docs only when the
  new agent becomes a canonical example there.

## ACP Integration Search Sweep

After wiring the runtime, run a targeted doc sweep for stale hardcoded
assumptions:

```bash
rg -n "CAR currently supports|supported agents|Agent not found|PMA|Telegram|Discord" README.md docs -g '!vendor/**'
rg -n "Codex|OpenCode|opencode|codex" README.md docs -g '!vendor/**'
rg -n "review|model listing|transcript|approvals|interrupt" README.md docs -g '!vendor/**'
```

Then classify each hit as:

- intentionally backend-specific documentation,
- user-facing docs that should mention the new runtime, or
- surface docs that should mention capability-gated unsupported actions instead
  of implying universal support.

## Testing Checklist

Before submitting, verify:

- [ ] Harness implements the required `AgentHarness` contract.
- [ ] Agent registration uses the correct canonical capability names.
- [ ] Configuration defaults include the agent binary path when changing CAR.
- [ ] Smoke tests pass when the binary is present.
- [ ] Full turn tests pass when credentials or runtime dependencies are present.
- [ ] If `model_listing` is advertised, `/api/agents/<agent_id>/models` returns
  a valid model catalog; otherwise it returns a capability error.
- [ ] If `active_thread_discovery` is advertised, conversation listing works
  through the relevant CAR surface.
- [ ] Unsupported actions fail with capability-driven errors rather than
  pretending to work.
- [ ] README, setup docs, and runtime runbooks are updated where relevant.
- [ ] A doc/search sweep was run for stale backend assumptions.
- [ ] Protocol drift refresh/check commands stay aligned if you maintain them.

## Troubleshooting

**"Agent not available"**

- Check that the agent is registered.
- Verify the health check returns `True`.
- Check config and binary paths.

**"Module not found"**

- Add `__init__.py` to `src/codex_autorunner/agents/<agent_name>/`.
- Verify imports in the harness factory and registry wiring.

**Smoke tests fail**

- Verify the binary is accessible with `which myagent`.
- Check the binary help output.
- Review supervisor startup logs.

## References

- Existing implementations: `src/codex_autorunner/agents/codex/`,
  `src/codex_autorunner/agents/opencode/`,
  `src/codex_autorunner/agents/hermes/`,
  `src/codex_autorunner/agents/zeroclaw/`
- Agent harness protocol: `src/codex_autorunner/agents/base.py`
- Registry: `src/codex_autorunner/agents/registry.py`
