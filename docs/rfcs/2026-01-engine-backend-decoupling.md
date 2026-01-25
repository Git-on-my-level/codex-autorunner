# RFC: Decouple Engine from backend adapters (issue #406)

## Summary
Engine currently imports Codex/OpenCode adapter implementations directly, violating the Constitution’s requirement that the Engine remain protocol-agnostic. This RFC captures the boundary and proposed refactor path to restore one-way dependencies.

## Problem statement
- Constitution: Engine is protocol-agnostic; adapters translate external protocols (docs/car_constitution/10_CODEBASE_CONSTITUTION.md).
- Architecture Map: Engine has no transport/vendor coupling (docs/car_constitution/20_ARCHITECTURE_MAP.md).
- `core/engine.py` imports `codex_autorunner.agents.*` and `codex_autorunner.integrations.app_server.*`, making backend choice part of the Engine.

## Goals
- Engine depends only on a narrow adapter interface (e.g., `AgentBackend` contract) and receives normalized run events/artifacts.
- Backend-specific orchestration lives in adapter packages.
- Enforce import direction via lightweight guard (import-lint/CI).

## Proposed steps
1) Define/confirm a minimal backend interface (`AgentBackend` + `RunEvent` stream) in `integrations/agents/`.
2) Move backend orchestration (OpenCode supervisor, Codex app-server client/supervisor, capability checks) out of `core/engine.py` into adapter modules.
3) Update Engine to depend on the interface only; remove direct adapter imports.
4) Add an import guard preventing `core/*` from importing `integrations/*` adapter implementations.

## Acceptance criteria
- `core/engine.py` contains no imports from `codex_autorunner.agents.*` or `codex_autorunner.integrations.app_server.*`.
- Engine compiles against the adapter interface only.
- CI check enforces dependency direction.

## Tracking
Fixes #406.
