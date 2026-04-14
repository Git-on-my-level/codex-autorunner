# CAR Plugin API

This document describes the stable public API surface for external plugins.

## Scope

CAR supports plugin loading via Python packaging **entry points**.

Currently supported plugin type:

- **Agent backends**: add a new agent implementation (harness + supervisor).

## Choose The Right CAR Resource

External runtimes do not always map to repos.

- Use repo semantics when the agent's durable identity is a project worktree and
  CAR should execute against that code checkout.
- Use `agent_workspace` semantics when the durable identity is runtime-managed
  memory/config/session state that should live under
  `<hub_root>/.codex-autorunner/runtimes/<runtime>/<workspace_id>/`.

CAR does not install runtimes for plugins. A plugin may detect or launch a
configured binary, but the operator remains responsible for making that runtime
available on the host.

## Versioning

Plugins MUST declare compatibility with the current plugin API version:

- `codex_autorunner.api.CAR_PLUGIN_API_VERSION`

CAR will skip plugins whose declared `plugin_api_version` does not match.

## Agent backend entry point

Entry point group:

- `codex_autorunner.api.CAR_AGENT_ENTRYPOINT_GROUP`
- (currently: `codex_autorunner.agent_backends`)

A plugin package should expose an `AgentDescriptor` object:

```python
from codex_autorunner.api import AgentDescriptor, AgentHarness, CAR_PLUGIN_API_VERSION

def _make(ctx: object) -> AgentHarness:
    raise NotImplementedError

AGENT_BACKEND = AgentDescriptor(
    id="myagent",
    name="My Agent",
    capabilities=frozenset(["threads", "turns"]),
    make_harness=_make,
    plugin_api_version=CAR_PLUGIN_API_VERSION,
)
```

and declare it in `pyproject.toml`:

```toml
[project.entry-points."codex_autorunner.agent_backends"]
myagent = "my_package.my_agent_plugin:AGENT_BACKEND"
```

Notes:

- Plugin ids are normalized to lowercase.
- Plugins cannot override built-in agent ids.
- Plugins SHOULD avoid import-time side effects; do heavy initialization inside `make_harness`.

## Durable-Thread Contract

CAR v1 orchestration requires agent backends to implement a **durable thread/session model**:

1. **Threads persist beyond a single interaction**: Creating a conversation produces a session ID that remains valid across CAR restarts
2. **Threads support resume**: Given a thread/session ID, the agent can resume from where it left off
3. **Turns are atomic**: Each turn has a clear start and terminal state

### Must-Support Core Interface

```python
async def new_conversation(workspace_root: Path, title: Optional[str]) -> ConversationRef
async def resume_conversation(workspace_root: Path, conversation_id: str) -> ConversationRef
async def start_turn(...) -> TurnRef
async def wait_for_turn(...) -> TerminalTurnResult
```

Hermes is the in-tree example of a backend that satisfies this contract through
an external thread/session API while still omitting other optional capabilities
such as `review` and `model_listing`.

### Single-Session Runtimes (Out of Scope)

**Single-session runtimes are explicitly out of scope for CAR v1 orchestration.** These are runtimes that:

- Do not persist conversation state beyond a single request/response cycle
- Cannot resume a previous conversation
- Do not expose a session/conversation ID

If your runtime does not expose a documented public thread/session API, do not
advertise the durable-thread contract unless CAR can prove equivalent
relaunch/resume semantics with a first-class CAR-managed `agent_workspace`.
Hermes is the reference example for the documented repo-backed path: CAR trusts
Hermes durable sessions through ACP when the installed build advertises the ACP
launch contract CAR expects. ZeroClaw is the reference example for the narrower
`agent_workspace` path: CAR proves `durable_threads` and `message_turns` only
for CAR-managed agent workspaces when the installed runtime build advertises the
exact launch contract CAR uses. Current public `zeroclaw 0.2.0` does not
advertise `zeroclaw agent --session-state-file`, so CAR now reports it as
incompatible instead of inferring durability from workspace selection alone.

## Capability Model

### Required Capabilities

All agent backends must declare these core capabilities:

- `durable_threads`: Thread create/resume/list operations
- `message_turns`: Turn execution within threads

### Optional Capabilities

Plugins can optionally declare additional capabilities:

- `model_listing`: Return available models via `model_catalog()`
- `active_thread_discovery`: List existing conversations via `list_conversations()`
- `interrupt`: Interrupt running turns
- `review`: Run code review operations
- `event_streaming`: Stream turn events in real-time
- `transcript_history`: Retrieve conversation transcript
- `approvals`: Support approval/workflow mechanisms
- `structured_event_streaming`: Structured event format support

Capability aliases (legacy → canonical):
- `threads` → `durable_threads`
- `turns` → `message_turns`

### Capability Discovery

CAR supports both static and runtime capability discovery:

1. **Static capabilities**: Declared in `AgentDescriptor.capabilities` at registration
2. **Runtime capabilities**: Reported via `harness.runtime_capability_report()` after initialization

The harness automatically gates optional helper methods:
- Calling `model_catalog()` on an agent without `model_listing` raises `UnsupportedAgentCapabilityError`
- Calling `interrupt()` on an agent without `interrupt` raises `UnsupportedAgentCapabilityError`

## Reference Implementations

- **ZeroClaw**: Detect-only CAR-managed `agent_workspace` adapter. Supports
  `durable_threads`, `message_turns`, `active_thread_discovery`, and
  `event_streaming` for CAR-managed agent workspaces. Caveats remain explicit:
  workspace memory is shared across threads, one active turn is allowed per
  ZeroClaw session, and `interrupt`/`review` are not advertised.
- **Hermes**: ACP-backed repo/worktree adapter. Supports `durable_threads`,
  `message_turns`, `active_thread_discovery`, `interrupt`, `event_streaming`,
  and `approvals`. Caveats remain explicit: Hermes runs against a shared
  `HERMES_HOME`, model catalogs are not advertised, review is unsupported, and
  CAR does not promise transcript-history reconstruction beyond CAR-observed
  turns.
- **Codex**: Full-featured, supports all optional capabilities
- **OpenCode**: Full-featured except `approvals`
