# Chat Surface Integration Harness

This package holds the highest-signal chat-surface fixtures for regressions that
need to exercise real surface ingress plus a subprocess-backed runtime.

## What it covers

- Discord PMA message ingress through `DiscordBotService`
- Telegram PMA message ingress through `TelegramBotService`
- Hermes official ACP runtime behavior through the subprocess fixture in
  `tests/fixtures/fake_acp_server.py`

## Fastest way to use it

Run the single lab entrypoint before merging chat UX/runtime changes:

```bash
make test-chat-surface-lab
```

For direct integration-focused pytest runs, use:

```bash
.venv/bin/pytest tests/chat_surface_integration/test_hermes_pma_ux_regressions.py -m integration
.venv/bin/pytest tests/chat_surface_integration/test_hermes_pma_surface_parity.py -m integration
.venv/bin/python -m pytest tests/test_cross_surface_parity.py -q
.venv/bin/python -m pytest tests/test_doctor_checks.py -q -k chat_doctor_checks
```

Run the Hermes PMA surface parity tests only:

```bash
.venv/bin/pytest tests/chat_surface_integration/test_hermes_pma_surfaces.py -m integration
```

Run the official-path timeout characterization:

```bash
.venv/bin/pytest tests/chat_surface_integration/test_hermes_pma_official_timeout.py -q
```

## Current regression coverage

The Hermes surface tests now cover the UX-contract scenarios that previously
regressed in production:

- Discord sends an initial preparation message.
- Progress updates edit that same placeholder into `working`.
- Completion edits the placeholder to `done`, deletes it, then sends the final reply.
- Telegram sends a lightweight `Working...` placeholder and then a separate final reply.
- Both surfaces emit first-visible and first-progress timing logs that are
  checked against non-flaky latency budgets.
- Busy-thread queueing becomes visibly queued before recovery resumes the
  waiting turn.
- Interrupt controls must acknowledge quickly and still reconcile to a final
  interrupted state.
- Related parity/doctor checks keep the shared UX regression matrix explicit so
  required scenarios cannot quietly disappear from coverage.

Managed-thread restart recovery is currently covered separately at the
orchestration/runtime layer rather than by a Telegram/Discord surface harness.

Legacy ACP prompt-notification fixtures still exist in `fake_acp_server.py` for
generic ACP compatibility work, but they are not the current Hermes contract.

## Relationship to `tests/chat_surface_lab/`

`tests/chat_surface_lab/` now provides the shared package boundary for
scenario, transcript, and artifact contracts. This integration package remains
the home for current high-signal regressions that exercise the real surface
entrypoints. Later tickets may migrate common declarations into the lab
package, but this ticket does not change the existing integration behavior.

Operator and agent workflow details live in
`docs/ops/chat-surface-lab.md`.

## Adding a new scenario

1. Add or extend a subprocess scenario in `tests/fixtures/fake_acp_server.py`.
1. Reuse `HermesFixtureRuntime` from `harness.py`.
1. Drive the surface under test with `DiscordSurfaceHarness` or
   `TelegramSurfaceHarness`.
1. Assert the actual surface lifecycle, not just backend completion. For
   Discord this means preparation send, progress edits, placeholder cleanup,
   and final reply delivery.
