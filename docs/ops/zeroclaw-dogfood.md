# ZeroClaw Host Dogfood

This is the opt-in local-host path for validating the CAR-managed ZeroClaw
contract that CAR can actually prove.

ZeroClaw support in CAR is detect-only:

- CAR expects a usable `zeroclaw` binary to already be installed on the host.
- CAR does not install or bootstrap ZeroClaw for you.
- The supported v1 path is a first-class CAR-managed `agent_workspace`, not a
  volatile wrapper around ad hoc user state.

## Compatibility Contract

CAR only treats ZeroClaw as a durable `agent_workspace` runtime when the
installed binary advertises a documented launch surface that CAR can verify.

Today that means:

- `zeroclaw --version` must succeed.
- `zeroclaw agent --help` must advertise
  `zeroclaw agent --session-state-file`.

If that flag is missing, CAR fails fast instead of guessing at a workspace-only
or wrapper-only launch mode.

On the current host example from issue #966:

- `zeroclaw 0.2.0` is installed
- `zeroclaw agent --help` does not advertise `--session-state-file`
- CAR therefore reports the runtime as incompatible and keeps the durable-thread
  contract disabled

Unsupported in v1:

- Treating ambient `~/.zeroclaw` state as the managed CAR workspace root
- Claiming first-class support for volatile wrapper-only launches outside the
  managed `agent_workspace` path
- Claiming that `ZEROCLAW_WORKSPACE` alone proves CAR-grade resume semantics

## CLI Provisioning Flow

The first-class CLI path is:

```bash
car hub agent-workspace create zc-main --runtime zeroclaw --disabled --path <hub_root>
car hub agent-workspace show zc-main --path <hub_root>
car doctor --hub --path <hub_root>
```

`create --disabled` lets you preprovision the workspace manifest entry even when
the runtime is not ready yet. Enabling the workspace runs the same preflight
used by doctor and launch-time checks.

To remove the manifest entry while keeping files:

```bash
car hub agent-workspace remove zc-main --path <hub_root>
```

To also delete the managed workspace files:

```bash
car hub agent-workspace remove zc-main --delete-files --path <hub_root>
```

## Install

On macOS hosts with Homebrew:

```bash
brew install zeroclaw
```

CAR's default config already resolves `agents.zeroclaw.binary: zeroclaw`, so no
repo override is required when the Homebrew binary is on `PATH`.

## Onboard

Run ZeroClaw's own one-time provider setup on the host if it is not already
initialized. This setup is host-local and intentionally outside CAR's default
CI path.

Before running live checks, confirm `zeroclaw config show` reports the
provider/model you expect.

## Host Validation

Raw CLI smoke check:

```bash
zeroclaw agent -m "Reply with the exact text ZC-OK and nothing else." \
  --provider zai \
  --model glm-5
```

Compatibility check:

```bash
zeroclaw --version
zeroclaw agent --help
```

CAR harness integration check:

```bash
ZEROCLAW_DOGFOOD=1 \
ZEROCLAW_TEST_MODEL=zai/glm-5 \
PYTHONPATH=src \
.venv/bin/pytest -q tests/agents/zeroclaw/test_zeroclaw_host_integration.py -m integration
```

If the installed ZeroClaw build does not advertise `--session-state-file`, this
test now skips with the same compatibility message CAR surfaces in doctor and
agent-workspace readiness.

Focused local proof of the CAR-managed contract:

```bash
PYTHONPATH=src \
.venv/bin/pytest -q \
  tests/agents/zeroclaw/test_zeroclaw_supervisor.py \
  tests/test_pma_managed_threads_messages.py
```

## Docker-backed Agent Workspaces

For CAR-managed `agent_workspaces[]` with `destination.kind: docker`, CAR keeps
the workspace root as the durable identity, but runtime compatibility is treated
as deferred until launch unless CAR can probe the containerized binary through
the configured destination.
