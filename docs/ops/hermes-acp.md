# Hermes ACP Runbook

Use this runbook to install, validate, and operate the Hermes integration in
Codex Autorunner (CAR).

## Overview

Hermes is a repo-backed ACP runtime in CAR. CAR launches Hermes in ACP mode,
stores the Hermes session id as the backend thread binding, and reuses that
binding across PMA, Telegram, Discord, web, and ticket-flow turns.

Hermes is not a CAR-managed `agent_workspace` runtime in v1. CAR does not
create per-thread Hermes homes, and it does not inspect Hermes internal state
files directly.

## Prerequisites

Before enabling Hermes in CAR:

1. Install a Hermes build on the host and make `hermes` available on `PATH`, or
   set an explicit binary path in CAR config.
2. Complete Hermes' own host-side login/provider setup outside CAR.
3. Verify the installed build supports ACP mode:

```bash
hermes --version
hermes acp --help
```

CAR treats Hermes as incompatible only when the binary cannot run `hermes acp`
successfully enough for CAR to confirm ACP mode exists.

## Configuration

Configure Hermes in `codex-autorunner.yml`, `codex-autorunner.override.yml`, or
`.codex-autorunner/config.yml`:

```yaml
agents:
  hermes:
    binary: hermes
```

If Hermes is not on `PATH`, use an absolute path:

```yaml
agents:
  hermes:
    binary: /opt/hermes/bin/hermes
```

CAR does not install Hermes for you. The configured binary must already exist
and be executable on the host that runs CAR.

## Launch Expectations

The CAR Hermes supervisor launches:

```text
<configured hermes binary> acp
```

Compatibility checks expect the installed Hermes build to advertise durable
ACP support through `hermes acp --help`. `car doctor` reports Hermes as ready
when ACP mode is present; Hermes-native session durability remains owned by the
shared `HERMES_HOME` state.

Validate the runtime from CAR:

```bash
car doctor
car pma agents
```

Look for a `Hermes runtime availability` doctor check and a `Hermes (hermes)`
entry in the PMA agent list.

## Shared `HERMES_HOME`

Hermes v1 runs against a shared `HERMES_HOME`.

- CAR does not create per-thread or per-worktree Hermes homes.
- Multiple CAR threads may reuse the same host-level Hermes account state.
- CAR stores its own thread bindings and transcript mirrors, but Hermes remains
  the source of truth for Hermes-native session history.
- Resetting or deleting Hermes state in `HERMES_HOME` can stale existing CAR
  bindings. CAR will clear stale bindings and start a fresh session when resume
  fails.

Treat `HERMES_HOME` as shared operator-managed state, not as an isolated CAR
resource.

## Surface Guidance

Hermes appears in CAR-backed surfaces only after the surface-specific setup is
complete.

- For Telegram, follow `docs/AGENT_SETUP_TELEGRAM_GUIDE.md` for allowlists,
  topics, and collaboration policy.
- For Discord, follow `docs/AGENT_SETUP_DISCORD_GUIDE.md` for bot credentials,
  command registration, and collaboration policy.
- Hermes capability gating still applies in both surfaces: unsupported actions
  such as review or model listing fail explicitly instead of being rewritten
  into a Codex/OpenCode-only path.

## Supported Capabilities

Hermes currently supports:

- Durable thread/session create and resume
- Message turns
- Interrupt
- Event streaming
- Approval requests
- Manual model override strings

Hermes currently does not support:

- Active thread discovery on the current stable ACP surface
- Review mode
- Model catalog listing
- Transcript history as a public CAR contract
- CAR-managed Hermes-home isolation
- File-chat execution

On unsupported actions, CAR should return a capability-driven error rather than
silently falling back.

## PMA Usage

Use Hermes in PMA when you want CAR-managed durable threads backed by Hermes
sessions.

One-off PMA chat:

```bash
car pma chat --agent hermes "Summarize the current ticket state."
```

Managed PMA thread flow:

```bash
car pma threads create --agent hermes --workspace-root /abs/path/to/repo
car pma threads list --agent hermes
car pma threads send --id <thread-id> --message "Investigate the failing test."
car pma threads status --id <thread-id>
car pma threads interrupt --id <thread-id>
```

Notes:

- `car pma models hermes` is expected to fail because Hermes does not advertise
  `model_listing`.
- Use `--model <value>` only when you want to pass a free-form Hermes model
  override on the next turn.

## Ticket-Flow Usage

Hermes is supported in CAR ticket flow. Assign Hermes directly in ticket
frontmatter:

```yaml
---
ticket_id: tkt.example.hermes
title: "Example Hermes ticket"
agent: "hermes"
done: false
---
```

Then run ticket flow normally. CAR routes the turn through the Hermes harness
instead of the Codex or OpenCode backends.

Approval behavior follows ticket-flow policy:

- `ticket_flow.approval_mode: review` maps to `approval_mode=on-request` and
  `sandbox_policy=workspaceWrite`
- `ticket_flow.approval_mode: yolo` maps to `approval_mode=never` and
  `sandbox_policy=dangerFullAccess`

## Approval Behavior

Hermes approval requests are bridged into CAR approval handling.

- `approval_mode=never` auto-accepts Hermes permission requests.
- `approval_mode=on-request` waits for a CAR approval decision.
- If no handler is available, CAR uses the configured default approval
  decision.
- Timeouts, cancellations, and handler failures resolve deterministically and
  are recorded in runtime events as `permission/decision`.

This means Hermes approvals follow the same CAR-level operational policy knobs
used by ticket flow and chat surfaces instead of bypassing them.

## Troubleshooting

### `Hermes binary is not configured`

- Set `agents.hermes.binary` in CAR config.
- Confirm the final config source resolves to the binary you expect.

### `Hermes binary '...' is not available on PATH`

- Install Hermes or point `agents.hermes.binary` at the correct absolute path.
- Re-run `car doctor`.

### `Hermes ACP mode is not supported by this binary`

- Upgrade Hermes to a build that supports `hermes acp`.
- Verify `hermes acp --help` works outside CAR.

### Hermes loses durable session history unexpectedly

- Hermes durability comes from Hermes-native state under the shared
  `HERMES_HOME`, not from a CAR-passed `--session-state-file` flag.
- If session resume starts failing, inspect the Hermes installation/profile in
  use by CAR and verify that the expected `HERMES_HOME` still contains the
  Hermes session database/state for the same operator account.

### A PMA or ticket-flow turn starts fresh instead of resuming

- The stored Hermes session binding is probably stale.
- Check whether `HERMES_HOME` was reset, replaced, or switched to a different
  account/profile.
- Expect CAR to clear the stale binding and create a new Hermes session.

### `model_listing` or review operations fail

- This is expected for Hermes.
- Use manual `--model` overrides when needed.
- Do not route `/review` or `car pma models hermes` into Hermes expecting
  Codex/OpenCode behavior.
