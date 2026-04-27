# CAR Plugin API

This document is the canonical plugin API spec for external CAR integrations.
`docs/adding-an-agent.md` is the implementation guide; this file owns the
contract.

## Scope

CAR supports plugin loading via Python packaging entry points.

Currently supported plugin type:

- **Agent backends**: add a new agent implementation (harness + supervisor).

## Choose The Right CAR Resource

External runtimes do not always map to repos.

- Use repo semantics when the agent's durable identity is a project worktree and
  CAR should execute against that code checkout.
- Use `agent_workspace` semantics when the durable identity is runtime-managed
  memory, config, or session state that should live under
  `<hub_root>/.codex-autorunner/runtimes/<runtime>/<workspace_id>/`.

CAR does not install runtimes for plugins. A plugin may detect or launch a
configured binary, but the operator remains responsible for making that runtime
available on the host.

## Versioning

Plugins MUST declare `plugin_api_version`, typically by setting it to
`codex_autorunner.api.CAR_PLUGIN_API_VERSION`.

CAR currently handles declared versions with three outcomes:

- `None` or an unparseable value: rejected.
- Greater than `CAR_PLUGIN_API_VERSION`: rejected because the plugin requires a
  newer CAR core.
- Less than `CAR_PLUGIN_API_VERSION`: accepted with an info log.
- Equal to `CAR_PLUGIN_API_VERSION`: accepted.

Contract:

- Older plugins are accepted on a best-effort basis.
- Newer plugins are rejected.
- Plugins MUST declare a version.

## Agent Backend Entry Point

Entry point group:

- `codex_autorunner.api.CAR_AGENT_ENTRYPOINT_GROUP`
- Current value: `codex_autorunner.agent_backends`

A plugin package should expose an `AgentDescriptor` object:

```python
from __future__ import annotations

from typing import Any

from codex_autorunner.api import (
    AgentDescriptor,
    AgentHarness,
    CAR_PLUGIN_API_VERSION,
    RuntimeCapability,
)


def _make(ctx: Any) -> AgentHarness:
    raise NotImplementedError


AGENT_BACKEND = AgentDescriptor(
    id="myagent",  # required
    name="My Agent",  # required
    capabilities=frozenset(  # required
        [
            RuntimeCapability("durable_threads"),
            RuntimeCapability("message_turns"),
            RuntimeCapability("event_streaming"),
        ]
    ),
    make_harness=_make,  # required
    healthcheck=None,  # optional
    backend_factory=None,  # optional
    runtime_preflight=None,  # optional
    runtime_kind="myagent",  # optional
    plugin_api_version=CAR_PLUGIN_API_VERSION,  # required
)
```

`AgentDescriptor` fields:

- Required: `id`, `name`, `capabilities`, `make_harness`,
  `plugin_api_version`.
- Optional: `healthcheck`, `backend_factory`, `runtime_preflight`,
  `runtime_kind`.

Optional field meanings:

- `healthcheck`: return whether the backend is currently available in a given
  CAR app context.
- `backend_factory`: build a richer backend adapter when a surface needs more
  than the harness abstraction.
- `runtime_preflight`: run runtime-specific readiness checks before CAR tries to
  execute work.
- `runtime_kind`: override the default runtime identity CAR derives from the
  agent id.

Declare the descriptor in `pyproject.toml`:

```toml
[project.entry-points."codex_autorunner.agent_backends"]
myagent = "my_package.my_agent_plugin:AGENT_BACKEND"
```

Notes:

- Plugin ids are normalized to lowercase.
- Plugins cannot override built-in agent ids.
- Plugins SHOULD avoid import-time side effects; do heavy initialization inside
  `make_harness`.

## Durable-Thread Contract

CAR v1 orchestration requires agent backends to implement a durable
thread/session model:

1. Threads persist beyond a single interaction.
2. Threads support resume by stable conversation or session id.
3. Turns have a clear start and terminal state.

Must-support core interface:

```python
async def new_conversation(workspace_root: Path, title: Optional[str]) -> ConversationRef
async def resume_conversation(workspace_root: Path, conversation_id: str) -> ConversationRef
async def start_turn(...) -> TurnRef
async def wait_for_turn(...) -> TerminalTurnResult
```

Hermes is the in-tree example of a backend that satisfies this contract through
an external thread/session API while still omitting optional capabilities such
as `review`, `model_listing`, and `transcript_history`.

## Single-Session Runtimes (Out of Scope)

Single-session runtimes are out of scope for CAR v1 orchestration. If a runtime
does not expose a documented public thread or session API, do not advertise the
durable-thread contract unless CAR can prove equivalent relaunch and resume
semantics with a first-class CAR-managed `agent_workspace`.

Hermes is the reference example for the documented repo-backed path. ZeroClaw
is the reference example for the narrower `agent_workspace` path: CAR only
proves `durable_threads` and `message_turns` there when the installed runtime
build advertises the launch contract CAR depends on. Current public
`zeroclaw 0.2.0` does not advertise
`zeroclaw agent --session-state-file`, so CAR reports it as incompatible rather
than inferring durability from workspace selection alone.

## Capability Model

Capability names are canonical. The plugin API does not support alias strings
such as `threads`, `turns`, `session_resume`, or `turn_control`.

### Required Capabilities

All agent backends must declare these capabilities:

- `durable_threads`: create and resume durable conversations or sessions.
- `message_turns`: execute turns within those conversations or sessions.

### Optional Capabilities

Plugins may additionally declare:

- `active_thread_discovery`: implement `list_conversations()`.
- `approvals`: support approval or workflow controls.
- `event_streaming`: stream turn events in real time.
- `interrupt`: stop a running turn.
- `model_listing`: implement `model_catalog()`.
- `review`: implement review-oriented turns.
- `transcript_history`: expose transcript history retrieval.

### Capability Discovery

CAR supports both static and runtime capability discovery:

1. Static capabilities: declared in `AgentDescriptor.capabilities`.
2. Runtime capabilities: reported via `harness.runtime_capability_report()`
   after initialization.

The harness automatically gates optional helpers:

- Calling `model_catalog()` without `model_listing` raises
  `UnsupportedAgentCapabilityError`.
- Calling `interrupt()` without `interrupt` raises
  `UnsupportedAgentCapabilityError`.
- Calling `transcript_history()` without `transcript_history` raises
  `UnsupportedAgentCapabilityError`.
- Calling `stream_events()` without `event_streaming` raises
  `UnsupportedAgentCapabilityError`.

## Reference Implementations

- **ZeroClaw**: detect-only CAR-managed `agent_workspace` adapter. Supports
  `durable_threads`, `message_turns`, `active_thread_discovery`, and
  `event_streaming` for CAR-managed agent workspaces. Caveats remain explicit:
  workspace memory is shared across threads, one active turn is allowed per
  ZeroClaw session, and `interrupt` and `review` are not advertised.
- **Hermes**: ACP-backed repo/worktree adapter. Supports `durable_threads`,
  `message_turns`, `active_thread_discovery`, `interrupt`, `event_streaming`,
  and `approvals`. Caveats remain explicit: Hermes runs against a shared
  `HERMES_HOME`, model catalogs are not advertised, review is unsupported, and
  CAR does not promise transcript-history reconstruction beyond CAR-observed
  turns.
- **Codex**: full-featured implementation covering every current optional
  capability.
- **OpenCode**: full-featured implementation except `approvals`.
