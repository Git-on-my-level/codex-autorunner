# Active Context

## Current effort

Set up the CAR orchestration-layer scope package as the active planning context for this worktree.

The canonical specification now lives in `.codex-autorunner/contextspace/spec.md`, and the scope tickets now live in `.codex-autorunner/tickets/`.

## Why now

CAR already has the seams needed to become the orchestration layer for runtime-backed agents and CAR-native flows, but those seams are currently split across PMA-specific paths, transport-specific bindings, and mixed JSON/SQLite state.

This scope package is meant to turn that architectural direction into a dependency-safe downstream build plan.

## Target outcome

- CAR owns one thin orchestration ingress for agent threads and flow targets.
- Downstream runtimes remain canonical owners of conversation state.
- Hub-level SQLite becomes the canonical store for orchestration metadata, bindings, executions, mirrored plain-text transcripts, and projections.
- `ticket_flow` remains a flow target rather than being flattened into a normal chat thread.
- The final downstream implementation ticket set can run in one clean shot without reverse dependencies.

## Execution shape

- Use the imported spec to lock object names, service boundaries, state ownership, routing expectations, and adapter capabilities.
- Execute the six scope tickets in order so they produce the real downstream implementation tickets.
- Keep PMA as an orchestration client rather than the special architectural center.
- Preserve CAR-native flow semantics while exposing flows and threads through one observability surface.

## Imported artifacts

- Source package: `.codex-autorunner/filebox/inbox/car_orchestration_scope-75e08ae3.zip`
- Review follow-up package: `.codex-autorunner/filebox/inbox/car_orchestration_review_tickets-4ea7501c.zip`
- Canonical spec: `.codex-autorunner/contextspace/spec.md`
- Scope tickets:
  - `TICKET-100-orchestration-contract-and-target-model.md`
  - `TICKET-120-hub-sqlite-schema-and-state-migration.md`
  - `TICKET-140-runtime-adapter-capability-and-zeroclaw-scope.md`
  - `TICKET-160-surface-routing-bindings-and-pma-generalization.md`
  - `TICKET-180-ticket-flow-and-native-target-integration.md`
  - `TICKET-200-downstream-ticket-synthesis-and-linearization.md`
- Review follow-up tickets:
  - `TICKET-720-queue-first-thread-delivery.md`
  - `TICKET-740-zeroclaw-durable-session-contract.md`

## Current status

The contextspace and ticket queue are aligned around orchestration scoping plus the first review-driven follow-up work. The next execution step is to run the scope tickets and then pick up the imported queue-first delivery and ZeroClaw contract tickets as downstream implementation follow-ons.
