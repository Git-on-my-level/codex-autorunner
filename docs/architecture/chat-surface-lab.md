# Chat Surface Lab Architecture

This document records the repo-local contract for the chat-surface lab
initiative described in `.codex-autorunner/contextspace/spec.md`.

The lab is a test-only architecture. It exists to give CAR one deterministic,
headless, artifact-backed way to exercise Telegram, Discord, and future chat
surfaces without relying on live platform accounts or screenshot-only manual
verification.

This document is intentionally aligned with the surrounding repo architecture:

- real chat runtime and UX behavior remain owned by production modules under
  `src/codex_autorunner/integrations/`
- lab-only contracts, scenario models, and artifact shapes live under
  `tests/chat_surface_lab/`
- reusable surface fixtures stay in `tests/chat_surface_harness/`
- current high-signal end-to-end regressions continue to live in
  `tests/chat_surface_integration/` until later tickets migrate or wrap them

TICKET-100 establishes only the foundation contract and package skeleton. It
does not change Telegram, Discord, Hermes, or other runtime behavior.

## Scope

The chat-surface lab covers test infrastructure for:

- driving the real Telegram and Discord service entrypoints
- expressing backend-neutral scenario inputs
- capturing normalized transcript and timeline outputs
- declaring artifact bundles that later tickets will write
- giving future tickets a stable, discoverable package home

This ticket does not include:

- migrating existing integration tests into the new package
- introducing platform simulators or backend adapters yet
- adding browser-render or screenshot generation code yet
- changing existing Discord, Telegram, Hermes, or PMA behavior

## Problem Statement

The repo already has useful chat-surface testing pieces, but they are split
across reusable harness helpers and focused integration suites:

- `tests/chat_surface_harness/` provides reusable service fixtures
- `tests/chat_surface_integration/` exercises production-facing regressions
- backend fixtures under `tests/fixtures/` simulate Hermes, ACP, and app-server
  flows

Those pieces are valuable, but they do not yet provide one obvious location for
shared scenario contracts, transcript models, and evidence artifact metadata.
Without that shared home, future tickets would have to infer file layout and
data contracts from scattered tests.

The lab foundation closes that gap by introducing a package boundary first,
before any migration or simulator work starts.

## Requirements

The repo-local lab contract inherits the contextspace requirements and applies
them to the test package layout.

### Preserve real surface entrypoints

The lab must continue to drive the existing `TelegramBotService` and
`DiscordBotService` seams rather than introducing a parallel fake business
stack.

### Keep backend fixtures transport-neutral

The lab package must be able to describe scenarios that later map to Hermes,
ACP, app-server, or OpenCode fixtures without baking those runtime details into
surface-specific test code.

### Capture user-visible surface behavior

The lab must eventually describe the semantics that matter for production UX:

- visible feedback timing
- placeholder lifecycle
- edits, sends, deletes, and retries
- duplicate deliveries and restart windows
- interrupt and approval flows

### Preserve artifact-backed evidence

The lab must treat transcript, timeline, HTML render, screenshot, accessibility
snapshot, timing report, and structured logs as first-class artifacts, even if
this ticket only introduces the manifest models.

### Stay CI- and agent-friendly

The package layout must be obvious enough that follow-on tickets can add one
entrypoint command and CI wiring without rearranging the foundation.

## Ownership Boundaries

### `tests/chat_surface_lab/`

This package owns lab-only contracts and shared test metadata:

- scenario models and scenario-level expectations
- normalized transcript and timeline models
- artifact manifest models for run evidence
- package-level documentation that explains the lab layering

This package does not own live fixtures or migration of existing suites in this
ticket.

### `tests/chat_surface_harness/`

This package continues to own reusable service-facing harness helpers and
runtime fixture wiring. It remains the best place for helpers that construct
Telegram, Discord, and Hermes test services directly.

### `tests/chat_surface_integration/`

This package continues to own today’s highest-signal integration regressions.
It remains the place for tests that verify current PMA behavior through the
real surface ingress path while the lab is still being built out.

### Production packages

Production ownership does not move:

- `src/codex_autorunner/integrations/chat/` continues to own shared runtime and
  UX contracts
- `src/codex_autorunner/integrations/telegram/` and
  `src/codex_autorunner/integrations/discord/` continue to own transport
  adapters and platform behavior
- `.codex-autorunner/contextspace/spec.md` remains the initiative-level source
  of truth; this document is the repo-local implementation contract for tests

## Package Plan

TICKET-100 establishes the following initial layout:

```text
tests/chat_surface_lab/
  README.md
  __init__.py
  scenario_models.py
  transcript_models.py
  artifact_manifests.py
```

Phase intent for those modules:

- `scenario_models.py`
  Defines the declarative scenario shape that later parsers, corpus files, and
  runners will use.
- `transcript_models.py`
  Defines the normalized transcript and timeline records used to compare
  Telegram and Discord behavior.
- `artifact_manifests.py`
  Defines the stable artifact inventory that later runners and renderers will
  write to disk.

The package should stay importable at every phase, even while modules are still
contract-first placeholders.

## Artifact Contract

Later tickets must be able to produce a stable artifact bundle with at least
these logical outputs:

- normalized transcript JSON
- surface timeline JSON
- rendered HTML transcript
- screenshot PNG
- accessibility snapshot JSON
- timing or budget report JSON
- structured log extract

TICKET-100 only introduces the manifest types that name these artifacts. It
does not implement writers yet.

## Migration Guidance

Later tickets should adopt the package in this order:

1. Introduce backend runtime contracts behind `tests/chat_surface_lab/`.
2. Add Telegram and Discord simulator layers that still call into the real
   service entrypoints.
3. Teach current integration suites to import shared lab models where useful.
4. Move or wrap duplicated scenario declarations only when the shared contract
   is concrete enough to reduce guessing.

The migration rule is intentionally conservative: foundation first, behavior
unchanged until the lab contracts are ready to absorb real callers.

## Boundary Check

This ticket must preserve three invariants:

- existing Telegram, Discord, and Hermes integration behavior stays unchanged
- the new package contains only test-side contracts and documentation
- later tickets can find one explicit repo-local contract instead of inferring
  package structure from scattered tests
