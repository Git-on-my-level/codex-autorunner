# ZeroClaw Host Dogfood

This is the opt-in local-host path for validating ZeroClaw after `TICKET-740`.

## Contract

ZeroClaw is **not** a CAR v1 durable-thread orchestration target. The current
host validation path is limited to the experimental wrapper-managed flow:

- CAR resolves the configured `zeroclaw` binary
- CAR instantiates `ZeroClawSupervisor` and `ZeroClawHarness`
- a local wrapper session is created
- one prompt is sent and its terminal output is observed

Do not treat this as restart-resumable or orchestration-managed durable-thread
proof.

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

On this host, the validated runtime is already initialized and
`zeroclaw config show` reports:

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

## Current Host Status

On this host, `brew install zeroclaw` succeeds, the Homebrew service is running,
and `zeroclaw config show` reports a working `zai/glm-5` local configuration.
CAR can instantiate the real ZeroClaw supervisor/harness through its normal
binary resolution path.

The validated host-local smoke checks now pass:

- raw CLI: returns `ZC-OK`
- CAR harness integration: `1 passed`

This proves the supported post-`TICKET-740` limited wrapper path on this host.
It does not change ZeroClaw's non-durable contract in CAR core.
