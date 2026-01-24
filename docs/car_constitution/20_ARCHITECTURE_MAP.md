# Architecture Map

Goal: allow a new agent to locate the correct seam for a change without relying on fragile file-level details.

## Mental model
```
[ Engine ]  →  [ Control Plane ]  →  [ Adapters ]  →  [ Surfaces ]
```
Left is most stable; right is most volatile.

## Engine (protocol-agnostic)
Responsibilities:
- run lifecycle + state transitions
- scheduling/locks/queues
- deterministic semantics

Non-responsibilities:
- no UI concepts
- no transport/protocol coupling
- no vendor SDK assumptions

## Control plane (filesystem-backed intent)
Responsibilities:
- canonical state + artifacts under `.codex-autorunner/`
- plans/snapshots/outputs/run metadata
- a durable bridge between humans, agents, and the engine

## Adapters (protocol translation)
Responsibilities:
- translate external events/requests into engine commands
- normalize streaming/logging into canonical run artifacts
- tolerate retries, restarts, partial failures

Non-responsibilities:
- avoid owning business logic; keep logic in engine/control plane

## Surfaces (UX)
Responsibilities:
- render state; collect inputs; support reconnects
- provide ergonomics (logs, terminal, dashboards)

Non-responsibilities:
- do not become state owners; never be the only place truth lives

## Cross-cutting constraints
- **One-way dependencies**: Surfaces → Adapters → Control Plane → Engine (never reverse).
- **Isolation is structural**: containment via workspaces/credentials, not interactive prompts.
- **Replaceability**: any adapter/surface can be rewritten; engine/control plane must remain stable.
