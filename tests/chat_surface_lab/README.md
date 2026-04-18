# Chat Surface Lab

Shared contracts for the chat-surface lab initiative.

This package is the discoverable home for the test-only models that later
tickets will use to build a deterministic Telegram and Discord surface lab. The
goal is to keep future work anchored to one obvious package instead of
recreating scenario, transcript, and artifact contracts inside individual test
files.

## Current scope

TICKET-100 adds the package skeleton only:

- `scenario_models.py` defines declarative scenario metadata
- `transcript_models.py` defines normalized transcript and timeline records
- `artifact_manifests.py` defines artifact bundle metadata
- `backend_runtime.py` defines the backend-neutral fixture runtime contract used
  by lab and surface tests to drive Codex app-server, Hermes/ACP, and OpenCode
  fixtures through one normalized control seam

These modules are intentionally lightweight. They provide importable contracts
for later tickets without changing existing integration behavior.

TICKET-130 adds semantic surface simulators:

- `telegram_simulator.py` for deterministic Telegram behavior + transcript
  normalization
- `discord_simulator.py` for deterministic Discord interaction/message behavior
  + transcript normalization

## Relationship to nearby packages

- `tests.chat_surface_lab`
  Owns shared lab contracts and package-level documentation.
- `tests.chat_surface_harness`
  Owns reusable Telegram, Discord, and Hermes test harness helpers.
- `tests.chat_surface_integration`
  Owns today’s high-signal end-to-end chat regression suites.

The intended layering is:

1. `chat_surface_lab` declares shared contracts.
2. `chat_surface_harness` provides service-facing fixture helpers.
3. `chat_surface_integration` and future lab runners consume both.

## Design constraints

- Use real surface entrypoints rather than a parallel fake business stack.
- Keep backend runtime modeling transport-neutral so Hermes, ACP, app-server,
  and later OpenCode fixtures can share the same scenario contract.
- Prefer deterministic, diffable artifact shapes over ad hoc assertions.
