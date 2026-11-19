# CAR Codebase Constitution

This document defines the identity and long-lived invariants of CAR. It is
intentionally aspirational and time-decay resistant.

## Identity

CAR is a local-first, durable, cross-vendor attention router. It accepts lifecycle and
attention events from agents and automation, normalizes them into one incident model,
preserves human handoffs, routes replies and authorized effects, and explains what
happened from durable records.

Autonomous operation, policy judgment, and learning are capability providers. They may
improve how CAR handles attention, but they are not CAR core and cannot become an
alternative authority for routing, grants, effects, delivery, or audit.

CAR v2, the filesystem ticket runner and orchestration hub, is deprecated. Its code and
documentation remain during migration for security fixes, critical correctness fixes,
and migration support. New product semantics follow the v3 constitution.

## Non-negotiable invariants

### 1) Durable state is the source of truth

- If an event, human action, grant, decision, effect, delivery, or failure matters, it
  must be reconstructable from durable state.
- CAR v3 core runtime truth lives in SQLite on disk. In-memory handles may accelerate
  work but must have an explicit crash/degradation path.
- Chat transcripts and provider memory are context, never authority.

### 2) Canonical state has explicit ownership

- CAR v3 global state defaults to `~/.car/` and may be explicitly relocated.
- Core owns its database, configuration, provider registry, provider-invocation
  provenance, and core-managed artifacts beneath that root.
- Provider-specific state lives beneath
  `~/.car/providers/<provider-id>/instances/<instance-id>/<continuity-key>/state/` and
  must not add private authority to the core schema or merge continuity scopes
  implicitly.
- Legacy `.codex-autorunner/` roots remain authoritative only for deprecated v2 state.

### 3) Router, providers, adapters, and surfaces stay replaceable

- **Router core:** event, identity, incident, routing, human handoff, grants, effects,
  safety, outbox, audit, recovery, digest, and watchdog semantics.
- **Capability providers:** operator proposals, policy advice, and memory context or
  learning through separate versioned contracts.
- **Adapters:** translate external event and delivery protocols without owning routing
  policy.
- **Surfaces:** present core state and record human inputs without inventing lifecycle
  state or mutating provider internals.
- Dependencies point toward versioned core contracts. Providers never receive a raw
  executor and surfaces never become a second control plane.

The checked-in compatibility inventory remains explicit during migration: Web and CLI
are local operator surfaces, Telegram is the first v3 chat surface, and Discord remains
a supported v2 adapter until its v3 migration is separately ratified. Existing runtime
integrations include Codex and OpenCode in v2; Hermes is the first non-native v3
capability provider. Listing an integration here records support and migration scope—it
does not grant that integration core authority.

### 4) Autonomy is granted, never inferred

- Providers may learn preferences and propose grants.
- Only an explicit human action creates, broadens, or renews a grant.
- CAR core stores and enforces grants for every autonomous effect class.
- Provider confidence, memory, conversation history, or policy advice cannot become
  authorization.
- Core safety rails may reject an effect even when a grant covers it.

### 5) Providers propose; CAR executes

- Every externally visible action is a typed effect proposed to CAR core.
- Core validates, authorizes, persists, executes, and audits effects through one path.
- Provider absence, incompatibility, timeout, invalid output, or crash degrades to a
  deterministic human escalation, not silent loss or improvised execution.

### 6) Determinism over cleverness

- Urgency, expiry, idempotency, loop prevention, leases, delivery state, grants, safety
  gates, and fallback routing are deterministic core behavior.
- Prefer explicit configuration and stable state machines over hidden inference.
- A decision must name the provider instance, context references, grant, safety
  verdict, effect, and result that shaped it.

### 7) Small, reviewable diffs

- One primary intent per change.
- Avoid drive-by refactors; isolate mechanical changes from behavior changes.
- A rewrite is not permission to weaken evidence, migration, rollback, or review
  boundaries.

### 8) Observability is a contract

- Every incident and effect must answer: what happened, why, which authority allowed
  it, where it was delivered, and where it failed.
- Queued, delivered, uncertain, failed, expired, superseded, abandoned, suppressed,
  and no-target outcomes are distinct durable facts. A degraded fallback is recorded
  separately and cannot masquerade as confirmed delivery.
- Observability must be bounded and cheap by default; keep compact facts and references
  instead of unbounded copied transcripts.
- CAR requires an independently observed liveness signal before claiming daemon silence
  is unambiguous.

### 9) CAR is self-describing

- Agents and operators must discover CAR basics, active provider topology, capabilities,
  safety posture, and health without prior chat history.
- CAR provides stable machine-readable introspection and a compact human/agent brief.
- Unsupported capabilities fail explicitly rather than disappearing from surfaces or
  being forwarded as ordinary text.

### 10) Human facts outrank provider state

- Human instructions, feedback, grants, revocations, and replies are durable core facts.
- Surfaces record those facts generically; configured providers observe them through a
  retryable contract.
- Provider observation failure never rolls back a human action.
- Agents and providers propose and execute within authority; durable core facts decide.

## Decision hierarchy

When documents conflict:

1. Constitution (this document)
2. Accepted v3 architecture decisions under `v3/docs/architecture/`
3. v3 binding design (`v3/DESIGN.md`)
4. Architecture Map
5. Engineering Standards
6. Observability and Operations
7. Agent docs and workflows
8. Glossary

Documents explicitly marked deprecated or legacy describe v2 behavior only and cannot
override v3 decisions.

## Evolution rules

- Keep router-core contracts smaller than provider implementations.
- Add provider capabilities rather than vendor-specific intelligence to core.
- Stabilize provider contracts through the native and Hermes implementations before
  promising a marketplace or compatibility ecosystem.
- Preserve compatibility at event, provider, adapter, surface, and export boundaries
  when feasible; do not preserve obsolete internal abstractions merely for shape.
- If an invariant changes, record the rationale in an accepted decision and update this
  constitution in the same change.
