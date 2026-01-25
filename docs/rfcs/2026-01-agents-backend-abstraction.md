# RFC: Decide fate of `integrations/agents` backend abstraction (issue #412)

## Summary
`integrations/agents/*` defines `AgentBackend`, `OpenCodeBackend`, `CodexAppServerBackend`, and `RunEvent` types but is not used by the main engine execution path. This RFC records options to either adopt the abstraction or remove it to avoid architectural drift.

## Options
**A) Adopt the abstraction (preferred if keeping protocol-agnostic engine)**
- Make Engine consume only `AgentBackend` + streamed `RunEvent`.
- Move Codex/OpenCode orchestration from `core/engine.py` into backend implementations.
- Ensure telemetry/logging flows through the interface.

**B) Remove the abstraction**
- If a different seam is chosen, delete `integrations/agents/*` and document the canonical boundary.

## Proposed steps (for option A)
1) Finalize `AgentBackend` contract (inputs/outputs, event schema, capability flags).
2) Wire Engine to depend on the interface; eliminate direct imports of backend implementations.
3) Update tests to exercise Engine using the interface.
4) Remove duplicate backend execution paths once the interface is primary.

## Acceptance criteria
- Either Engine runs through `AgentBackend` end-to-end OR `integrations/agents/*` is removed and documented.
- Docs describe the canonical backend boundary to avoid future duplication.

## Tracking
Fixes #412.
