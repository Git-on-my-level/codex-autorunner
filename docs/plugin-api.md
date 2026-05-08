# CAR Plugin API

This document is the canonical external contract for CAR agent plugins.

## Scope

CAR supports plugin loading via Python packaging **entry points**.

Currently supported plugin type:

- **Agent backends**: add a new agent implementation (harness + supervisor)

## Choose The Right CAR Resource

External runtimes do not always map to repos.

- Use repo semantics when the agent's durable identity is a project worktree and
  CAR should execute against that code checkout.
- Use filesystem scope semantics when the durable execution root is an explicit
  absolute workspace path that should not be confused with a Git repo entry.
  Hub manifests remain repo-centric; filesystem threads bind orchestration to a
  concrete directory CAR executes under.

CAR does not install runtimes for plugins. A plugin may detect or launch a
configured binary, but the operator remains responsible for making that runtime
available on the host.

## Versioning

Plugins MUST declare compatibility with the current plugin API version:

- `codex_autorunner.api.CAR_PLUGIN_API_VERSION`

CAR accepts or rejects a plugin based on the declared `plugin_api_version`:

- Missing or unparseable version: rejected
- Not equal to `CAR_PLUGIN_API_VERSION`: rejected
- Equal to `CAR_PLUGIN_API_VERSION`: accepted

The compatibility contract is exact-match only. Backwards-incompatible cleanup
ships behind a plugin API version bump, and older plugin contracts are not kept
alive inside the loader.

## Agent backend entry point

Entry point group:

- `codex_autorunner.api.CAR_AGENT_ENTRYPOINT_GROUP`
- currently `codex_autorunner.agent_backends`

A plugin package should expose an `AgentDescriptor` object:

```python
from __future__ import annotations

from codex_autorunner.api import (
    AgentDescriptor,
    AgentHarness,
    CAR_PLUGIN_API_VERSION,
    RuntimeCapability,
)


def _make(ctx: object) -> AgentHarness:
    raise NotImplementedError


def _healthcheck(ctx: object) -> bool:
    _ = ctx
    return True


AGENT_BACKEND = AgentDescriptor(
    id="myagent",
    name="My Agent",
    capabilities=frozenset(
        {
            RuntimeCapability("durable_threads"),
            RuntimeCapability("message_turns"),
            RuntimeCapability("event_streaming"),
        }
    ),
    make_harness=_make,
    healthcheck=_healthcheck,
    backend_factory=None,
    runtime_preflight=None,
    runtime_kind="myagent",
    plugin_api_version=CAR_PLUGIN_API_VERSION,
)
```

And declare it in `pyproject.toml`:

```toml
[project.entry-points."codex_autorunner.agent_backends"]
myagent = "my_package.my_agent_plugin:AGENT_BACKEND"
```

Descriptor fields:

- Required: `id`, `name`, `capabilities`, `make_harness`,
  `plugin_api_version`
- Optional: `healthcheck`, `backend_factory`, `runtime_preflight`,
  `runtime_kind`

Optional field meanings:

- `healthcheck`: report whether the runtime is currently available so CAR can
  hide unavailable agents from operator-facing surfaces
- `backend_factory`: provide runtime-specific backend construction when the
  integration needs more than a harness
- `runtime_preflight`: run targeted readiness checks and return diagnostics
  before CAR starts routing traffic to the runtime
- `runtime_kind`: set a stable runtime family identifier when multiple logical
  agent ids share one backend implementation

Notes:

- Plugin ids are normalized to lowercase.
- Plugins cannot override built-in agent ids.
- Plugins SHOULD avoid import-time side effects; do heavy initialization inside
  `make_harness`.

## Durable-Thread Contract

CAR v1 orchestration requires agent backends to implement a **durable
thread/session model**:

1. Threads persist beyond a single interaction.
2. Threads support resume by thread or session id.
3. Turns are atomic and reach a clear terminal state.

### Must-Support Core Interface

```python
async def new_conversation(workspace_root: Path, title: Optional[str]) -> ConversationRef
async def resume_conversation(workspace_root: Path, conversation_id: str) -> ConversationRef
async def start_turn(...) -> TurnRef
async def wait_for_turn(...) -> TerminalTurnResult
```

Hermes is the in-tree example of a backend that satisfies this contract through
an external thread/session API while still omitting optional capabilities such
as `review` and `model_listing`.

### Single-Session Runtimes (Out of Scope)

**Single-session runtimes are explicitly out of scope for CAR v1 orchestration.**

These are runtimes that:

- Do not persist conversation state beyond a single request or response cycle
- Cannot resume a previous conversation
- Do not expose a session or conversation id

If your runtime does not expose a documented public thread or session API, do
not advertise the durable-thread contract unless CAR can prove equivalent
relaunch and resume semantics end to end.

Hermes is the reference example for the documented repo-backed path: CAR trusts
Hermes durable sessions through ACP when the installed build advertises the ACP
launch contract CAR expects. Codex and OpenCode remain the reference examples
for full-featured repo/worktree execution with their respective capability sets.

## Capability Model

### Required Capabilities

All agent backends must declare these core capabilities:

- `durable_threads`: thread creation and resume for durable conversations
- `message_turns`: turn execution inside durable conversations

### Optional Capabilities

Plugins can optionally declare additional capabilities:

- `active_thread_discovery`: list existing conversations via `list_conversations()`
- `approvals`: support approval or workflow mechanisms
- `event_streaming`: stream turn events in real time
- `interrupt`: interrupt running turns
- `model_listing`: return available models via `model_catalog()`
- `review`: run code review operations
- `transcript_history`: retrieve conversation transcript history

### Capability Discovery

CAR supports both static and runtime capability discovery:

1. **Static capabilities**: declared in `AgentDescriptor.capabilities` at
   registration time
2. **Runtime capabilities**: reported via
   `harness.runtime_capability_report()` after initialization

The harness automatically gates optional helper methods:

- Calling `model_catalog()` on an agent without `model_listing` raises
  `UnsupportedAgentCapabilityError`
- Calling `interrupt()` on an agent without `interrupt` raises
  `UnsupportedAgentCapabilityError`
- Calling `transcript_history()` on an agent without `transcript_history`
  raises `UnsupportedAgentCapabilityError`
- Calling `stream_events()` on an agent without `event_streaming` raises
  `UnsupportedAgentCapabilityError`

## Reference Implementations

- **Hermes**: ACP-backed repo or worktree adapter. Supports `durable_threads`,
  `message_turns`, `active_thread_discovery`, `interrupt`, `event_streaming`,
  and `approvals`. Caveats remain explicit: Hermes runs against a shared
  `HERMES_HOME`, model catalogs are not advertised, review is unsupported, and
  CAR does not promise transcript-history reconstruction beyond CAR-observed
  turns.
- **Codex**: full-featured runtime that advertises every optional capability
- **OpenCode**: full-featured runtime except `approvals`
