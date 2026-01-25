# RFC: Move Telegram doctor checks out of core/engine (issue #409)

## Summary
`core/engine.py` currently performs Telegram-specific doctor checks (config flags and optional deps), which conflicts with the Architecture Map’s “no transport/protocol coupling” rule for the Engine. This RFC proposes relocating those checks to the Telegram adapter and composing results at the CLI/server layer.

## Problem statement
- Architecture Map: Engine must be protocol-agnostic (docs/car_constitution/20_ARCHITECTURE_MAP.md).
- Engine doctor inspects Telegram settings and optional dependencies in `core/engine.py`.

## Goals
- Keep engine doctor focused on repo/hub structure, locks/state, required binaries, artifact roots.
- House Telegram-specific checks inside `integrations/telegram/doctor.py` (or similar).
- Present the same doctor output to users by composing subsystem reports.

## Proposed steps
1) Introduce `integrations/telegram/doctor.py` with checks for config enablement, required tokens, optional deps.
2) Expose a doctor registry/composer at the CLI/server layer to run per-subsystem doctors (engine, app-server, telegram, etc.).
3) Remove Telegram-specific logic from `core/engine.py`; keep only generic checks.
4) Add a small test to ensure Telegram doctor runs when telegram is enabled and engine doctor remains transport-agnostic.

## Acceptance criteria
- No Telegram-specific logic in `core/engine.py`.
- Doctor output remains functionally identical for users.
- Dependency boundaries align with the Architecture Map.

## Tracking
Fixes #409.
