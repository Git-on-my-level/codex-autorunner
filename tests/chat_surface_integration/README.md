# Chat Surface Integration Harness

This package holds the highest-signal chat-surface fixtures for regressions that
need to exercise real surface ingress plus a subprocess-backed runtime.

## What it covers

- Discord PMA message ingress through `DiscordBotService`
- Telegram PMA message ingress through `TelegramBotService`
- Hermes official ACP runtime behavior through the subprocess fixture in
  `tests/fixtures/fake_acp_server.py`

## Fastest way to use it

Run the Hermes PMA surface parity tests:

```bash
.venv/bin/pytest tests/chat_surface_integration/test_hermes_pma_surfaces.py -m integration
```

Run the official-path timeout characterization:

```bash
.venv/bin/pytest tests/chat_surface_integration/test_hermes_pma_official_timeout.py -q
```

## Current regression coverage

The Hermes surface tests cover the current official session lifecycle only:

- Discord sends an initial preparation message.
- Progress updates edit that same placeholder into `working`.
- Completion edits the placeholder to `done`, deletes it, then sends the final reply.
- Telegram sends a lightweight `Working...` placeholder and then a separate final reply.

Legacy ACP prompt-notification fixtures still exist in `fake_acp_server.py` for
generic ACP compatibility work, but they are not the current Hermes contract.

## Adding a new scenario

1. Add or extend a subprocess scenario in `tests/fixtures/fake_acp_server.py`.
1. Reuse `HermesFixtureRuntime` from `harness.py`.
1. Drive the surface under test with `DiscordSurfaceHarness` or
   `TelegramSurfaceHarness`.
1. Assert the actual surface lifecycle, not just backend completion. For
   Discord this means preparation send, progress edits, placeholder cleanup,
   and final reply delivery.
