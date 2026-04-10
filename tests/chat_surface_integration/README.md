# Chat Surface Integration Harness

This package holds the highest-signal chat-surface fixtures for regressions that
need to exercise real surface ingress plus a subprocess-backed runtime.

## What it covers

- Discord PMA message ingress through `DiscordBotService`
- Telegram PMA message ingress through `TelegramBotService`
- Hermes runtime behavior through the ACP subprocess fixture in
  `tests/fixtures/fake_acp_server.py`

## Fastest way to use it

Run the Hermes PMA surface parity tests:

```bash
.venv/bin/pytest tests/chat_surface_integration/test_hermes_pma_surfaces.py -m integration
```

Run the supervisor-only characterization for Hermes ACP edge cases:

```bash
.venv/bin/pytest tests/agents/hermes/test_hermes_supervisor.py -k missing_turn_id
```

## Current regression fixture

`terminal_missing_turn_id` simulates a Hermes ACP server that completes a turn
without including `turnId` in the terminal notification. Discord PMA previously
stayed on the live `working` placeholder indefinitely in this condition because
the active turn never resolved.

## Adding a new scenario

1. Add or extend a subprocess scenario in `tests/fixtures/fake_acp_server.py`.
1. Reuse `HermesFixtureRuntime` from `harness.py`.
1. Drive the surface under test with `DiscordSurfaceHarness` or
   `TelegramSurfaceHarness`.
1. Assert both progress visibility and final delivery so the test catches
   "spinner forever" regressions instead of only backend failures.

