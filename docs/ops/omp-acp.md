# OMP ACP Runbook

Use this runbook to install, validate, and operate the OMP (Oh My Pi) integration
in Codex Autorunner (CAR).

## Overview

OMP is a repo-backed ACP runtime in CAR. CAR launches `omp acp`, stores the OMP
session id as the backend thread binding, and reuses that binding across PMA,
Telegram, Discord, web, and ticket-flow turns.

OMP runs against its native agent store (`~/.omp/agent` by default). CAR does not
create per-thread OMP homes; OMP sessions are scoped naturally by the workspace
`cwd` passed to `session/new`.

## Prerequisites

Before enabling OMP in CAR:

1. Install OMP on the host and make `omp` available on `PATH`, or set an explicit
   binary path in CAR config.
2. Complete OMP's own host-side login/provider setup outside CAR (`omp` should
   be able to list models and run a turn on its own).
3. Verify the installed build supports ACP mode:

```bash
omp --version
omp acp --help
```

CAR confirms ACP support by probing `omp acp --help`. OMP advertises ACP protocol
version 1 with `loadSession`, `sessionCapabilities` (`list`, `fork`, `resume`,
`close`), image/embedded-context prompts, and MCP http/sse.

## Configuration

Configure OMP in `codex-autorunner.yml`, `codex-autorunner.override.yml`, or
`.codex-autorunner/config.yml`:

```yaml
agents:
  omp:
    binary: omp
```

If OMP is not on `PATH`, use an absolute path:

```yaml
agents:
  omp:
    binary: /usr/local/bin/omp
```

`agents.omp.binary` defaults to `omp`. CAR does not install OMP for you; the
configured binary must already exist and be executable on the host that runs CAR.

OMP has no profile concept (unlike Hermes); one binary is one runtime identity.

## Launch Expectations

The CAR OMP supervisor launches:

```text
<configured omp binary> acp
```

Validate the runtime from CAR:

```bash
car doctor
car pma agents
```

Look for an OMP runtime availability doctor check and an `OMP (omp)` entry in the
PMA agent list.

## Native session store

- OMP sessions persist under OMP's own store (`~/.omp/agent`), scoped by the
  workspace `cwd`. Multiple CAR workspaces get distinct session sets automatically.
- Auth, model, and memory state in `~/.omp/agent` is shared (same user) — this is
  intended.
- Resetting or deleting OMP state can stale existing CAR bindings; CAR clears a
  stale binding and starts a fresh session when resume fails.

## Supported Capabilities

OMP (verified against `omp acp`, protocol v1) currently supports:

- Durable thread/session create and resume (sparse `session/load` payloads handled)
- Active thread discovery via `session/list`
- Message turns (`session/prompt`, terminal via the RPC response)
- Event streaming (`session/update` with `agent_message_chunk`, plus OMP's
  `usage_update` / `session_info_update` / `available_commands_update` kinds)
- Model catalog listing (parsed from `configOptions`)
- Approval requests (OMP emits `session/request_permission` for tool calls; CAR
  bridges them into CAR approval handling)
- File-chat execution (via the generic harness path)

OMP currently does **not** support (via its ACP surface) and returns a
capability-driven error for:

- Interrupt — OMP rejects `session/cancel`
- Review mode — OMP has no `session/setMode`
- Transcript history — OMP exposes no transcript method
- Per-turn model selection — OMP has no `session/setModel` and ignores model at
  `session/new`; turns use OMP's configured default model

On unsupported actions, CAR returns a capability-driven error rather than
silently falling back to a Codex/OpenCode path.

## Model selection

ACP defines no model-listing RPC; OMP surfaces selectable models through
`configOptions` on session descriptors. CAR parses these into a model catalog
(`model_listing` capability) for display.

OMP's ACP surface has no `session/setModel`: the runtime uses OMP's configured
default model (`~/.omp` settings or `--model` at launch). A per-turn `--model`
override passed through CAR is **not honored** by OMP — `model_listing` is
informational. OMP also loads its model registry asynchronously, so a model
catalog requested immediately after a cold spawn may briefly appear empty; CAR
retries briefly, and the catalog is reliable once OMP's registry has loaded.

## Approval Behavior

OMP emits `session/request_permission` server-requests for tool calls. CAR
bridges them into its approval handling:

- `approval_mode=never` (or unset) auto-accepts OMP permission requests, so tools
  run without prompting.
- `approval_mode=on-request` waits for a CAR approval decision.
- If no handler is available, CAR uses the configured default approval decision.

OMP's permission requests use integer JSON-RPC ids and omit `turnId`; CAR's shared
ACP client rebinds them to the active turn and echoes the original id in its
response (required for OMP to progress multi-step tool turns).

## PMA Usage

Use OMP in PMA when you want CAR-managed durable threads backed by OMP sessions.

One-off PMA chat:

```bash
car pma chat --agent omp "Summarize the current ticket state."
```

Managed PMA thread flow:

```bash
car pma thread create --agent omp --workspace-root /abs/path/to/repo
car pma thread list --agent omp
car pma thread send --id <thread-id> --message "Investigate the failing test."
car pma thread status --id <thread-id>
```

`car pma thread interrupt` against an OMP thread is not supported (capability
error). `car pma models omp` returns the OMP model catalog once the registry has
loaded.

## Ticket-Flow Usage

OMP is supported in CAR ticket flow. Assign OMP directly in ticket frontmatter:

```yaml
---
ticket_id: tkt.example.omp
title: "Example OMP ticket"
agent: "omp"
done: false
---
```

Then run ticket flow normally. CAR routes the turn through the OMP harness.
Durable thread binding persists per ticket and is reused across turns; it resets
when the agent changes or the managed thread is stale/missing.

Approval behavior follows ticket-flow policy: `yolo` auto-accepts OMP permission
requests; `review` waits for a CAR decision.

## Troubleshooting

### `OMP binary is not configured`

- Set `agents.omp.binary` in CAR config.
- Confirm the final config source resolves to the binary you expect.

### `OMP binary '...' is not available on PATH`

- Install OMP or point `agents.omp.binary` at the correct absolute path.
- Re-run `car doctor`.

### `OMP ACP mode is not supported by this binary`

- Upgrade OMP to a build that supports `omp acp`.
- Verify `omp acp --help` works outside CAR.

### Tool turns hang or never complete

- This indicates OMP permission responses are not being acknowledged. Ensure you
  are on a CAR build that echoes OMP's integer permission-request ids (fixed in
  this integration). `approval_mode=never` auto-accepts tool calls.

### Model catalog is empty immediately after startup

- OMP loads its model registry asynchronously. Wait a moment and re-request; the
  catalog populates once the registry loads. This is a cold-start race, not a
  configuration error.
