# ZeroClaw Host Dogfood

This is the opt-in local-host path for validating the CAR-managed ZeroClaw
workspace contract.

## Contract

For CAR-managed `agent_workspace` resources, ZeroClaw currently proves this
contract:

- CAR resolves the configured `zeroclaw` binary and launches it under
  `ZEROCLAW_WORKSPACE=<workspace_root>/workspace`.
- One agent workspace can back multiple durable CAR threads.
- Workspace memory is shared at the workspace root.
- Session state stays isolated per thread under
  `<workspace_root>/threads/<session_id>/session-state.json`.
- Queueing remains per managed thread. CAR does not serialize unrelated threads
  just because they share one ZeroClaw workspace.

Current caveats:

- One ZeroClaw session still allows only one active turn at a time.
- `interrupt`, `review`, and model-catalog features are not advertised.
- The prove-out assumes CAR-managed workspace roots, not ambient
  `~/.zeroclaw` state.

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

Before running the live checks, confirm `zeroclaw config show` reports the
provider/model you expect. A known-good example is:

- `default_provider: zai`
- `default_model: glm-5`

## Host Validation

Raw CLI smoke check:

```bash
zeroclaw agent -m "Reply with the exact text ZC-OK and nothing else." \
  --provider zai \
  --model glm-5
```

CAR harness integration check:

```bash
ZEROCLAW_DOGFOOD=1 \
ZEROCLAW_TEST_MODEL=zai/glm-5 \
PYTHONPATH=src \
.venv/bin/pytest -q tests/agents/zeroclaw/test_zeroclaw_host_integration.py -m integration
```

Optional overrides:

- `ZEROCLAW_TEST_MODEL`
- `ZEROCLAW_TEST_PROMPT`
- `ZEROCLAW_EXPECTED_SUBSTRING`

Focused local proof of the CAR-managed contract:

```bash
PYTHONPATH=src \
.venv/bin/pytest -q \
  tests/agents/zeroclaw/test_zeroclaw_supervisor.py \
  tests/test_pma_managed_threads_messages.py
```

## Manual Shared-Memory Checklist

Use this when you want host evidence beyond the automated test suite.

1. Create one CAR-managed ZeroClaw workspace and two CAR threads under it.
2. In thread A, ask ZeroClaw to create `shared-memory.txt` inside the managed
   workspace with a unique token.
3. In thread B, ask ZeroClaw to read that same file and echo the token.
4. Confirm both threads have distinct
   `threads/<session_id>/session-state.json` files and that each file only
   contains its own conversation state.
5. Send work to both threads while one turn is still running. Confirm the
   second thread starts immediately instead of being queued behind the first.

## Manual Multi-Workspace Checklist

1. Create two CAR-managed ZeroClaw workspaces on the same host.
2. In workspace A, write a unique token into `shared-memory.txt`.
3. In workspace B, ask ZeroClaw to read `shared-memory.txt` and confirm it does
   not see workspace A's token.
4. Confirm both workspaces are served by the same configured `zeroclaw` binary.

## Docker-backed Agent Workspaces

For CAR-managed `agent_workspaces[]` with `destination.kind: docker`:

- CAR still treats the managed workspace root as the durable identity.
- CAR bind-mounts that workspace root into the container.
- ZeroClaw runs with `ZEROCLAW_WORKSPACE=<workspace_root>/workspace`.
- Durable session state remains under `<workspace_root>/threads/<session_id>/session-state.json`.

This keeps the Docker path aligned with the local managed-workspace contract instead
of introducing a separate container-only layout.
