# CAR Codebase Constitution

This document defines the identity and long-lived invariants of the CAR codebase. It is intentionally aspirational and time-decay resistant.

## Identity
CAR is a local-first, filesystem-backed agent orchestration system with multiple user surfaces (e.g., Telegram, Web) and multiple execution backends (e.g., Codex, OpenCode). It optimizes for leverage, speed, and evolvability.

## Non-negotiable invariants

### 1) Filesystem is the source of truth
- Durable artifacts > chat transcripts > model memory.
- If something matters (state, decisions, outputs), it must be representable on disk.

### 2) Canonical runtime state lives under a single root
- `.codex-autorunner/` under the repo root is the canonical location for per-repo runtime + agent state.
- A separate global root (default `~/.codex-autorunner/`) is allowed only for cross-repo caches/locks
  (e.g., update cache, app-server workspace pool) and must be explicitly configured.
- Avoid “shadow state” in env-only values, tmp dirs, implicit globals, or UI-only state.

### 3) Layering and replaceability
- **Engine**: protocol-agnostic semantics (runs, scheduling, state transitions).
- **Control plane**: filesystem-backed intent + artifacts.
- **Adapters**: translate external protocols into engine commands.
- **Surfaces**: present state and accept inputs.
- Adapters and surfaces are replaceable; engine + control plane survive refactors.

### 4) YOLO by default; safety is an opt-in posture
- Default execution posture is permissive (full permissions) under an assumed isolated workspace model.
- Safety knobs exist as explicit modes (e.g., review/safe) for higher-stakes contexts.

### 5) Determinism over cleverness
- Prefer explicit configs and stable state machines.
- Avoid implicit behavior that cannot be reconstructed from artifacts.

### 6) Small, reviewable diffs
- One primary intent per change.
- Avoid drive-by refactors; isolate mechanical refactors from behavior changes.

### 7) Observability is a contract
- Every run must leave enough signal to answer: what happened, why, where it failed.
- A run that cannot be explained from artifacts is considered a failed run.
- Observability must be bounded and cheap by default: prefer compact manifests and pointers over copied blobs.
- Verbose artifacts are opt-in or retention-bounded, never an unconditional per-invocation default.

### 8) CAR is self-describing
- Agents must be able to discover CAR basics from the local repo/worktree without relying on prior chat history.
- CAR must provide a stable agent-facing introspection primitive (for example, `car describe --json`) as a first-class surface.
- Self-description is local-first, version-coupled, and low-entropy: small canonical docs plus stable machine-readable CLI output.

### 9) Ticket templates are first-class control plane
- Templates are the canonical reusable behavior guidance system.
- Templates must be discoverable and applicable from primary CAR surfaces.
- Template application must record provenance (template reference/version) in durable artifacts.

### 10) Agents are executors, not authorities
- Agents propose and execute; files decide.
- No hidden coupling to chat history; re-load truth from disk each run.

## Decision hierarchy
When documents conflict:
1. Constitution (this doc)
2. Architecture Map
3. Engineering Standards
4. Observability & Operations
5. Agent docs (onboarding/cheatsheet/workflows)
6. Glossary

## Evolution rules
- Prefer adding new primitives over overloading existing ones.
- Prefer improving ticket templates and introspection primitives over adding new instruction layers.
- Preserve backward compatibility at the adapter/surface boundary when feasible, not in engine semantics.
- If an invariant must change, record the rationale in durable docs and update this constitution.
