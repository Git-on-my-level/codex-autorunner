---
title: "Scope the orchestration contract, target model, and shared vocabulary"
agent: "codex"
done: true
goal: "Lock the orchestration-layer contract in CAR terms and write the downstream implementation tickets for the core service, target model, and capability surface."
---

## Progress

- [x] Read the spec and current seams in `src/codex_autorunner/agents/base.py`, `src/codex_autorunner/agents/registry.py`, `src/codex_autorunner/core/hub.py`, `src/codex_autorunner/core/pma_thread_store.py`, `src/codex_autorunner/surfaces/web/routes/pma_routes/managed_thread_runtime.py`, and `src/codex_autorunner/surfaces/cli/pma_cli.py`.
- [x] Wrote `.codex-autorunner/contextspace/orchestration-contract-target-model.md` to lock the orchestration object model and service boundary in current CAR terms.
- [x] Reserved the `220-280` downstream range for the orchestration-core buildout from this scope ticket.
- [x] Created downstream tickets:
  - `TICKET-220-orchestration-package-and-target-model.md`
  - `TICKET-240-orchestration-service-interfaces.md`
  - `TICKET-260-capability-discovery-and-cli-filtering.md`
  - `TICKET-280-runtime-thread-target-on-agent-harness.md`
- [x] Kept PMA on the client side of the design instead of preserving PMA as a special-case execution bypass.

## Tasks
- Read `spec.md` and the current CAR seams around `AgentHarness`, `src/codex_autorunner/agents/registry.py`, `src/codex_autorunner/core/hub.py`, `src/codex_autorunner/core/pma_thread_store.py`, `src/codex_autorunner/surfaces/web/routes/pma_routes/managed_thread_runtime.py`, and `src/codex_autorunner/surfaces/cli/pma_cli.py`.
- Write a short code-grounded design memo under `.codex-autorunner/contextspace/` that confirms the final object model and names to use in code:
  - agent definition
  - thread/session
  - message
  - execution
  - flow target
  - binding
  - capability
- Decide the exact orchestration service boundary to generalize beneath PMA without inventing unnecessary new abstractions.
- Write the downstream implementation tickets for:
  - the orchestration package/module layout
  - the canonical service interfaces
  - capability discovery / CLI filtering
  - the runtime-backed thread target path built on top of `AgentHarness`
- Assign complex architectural work to `codex` and mechanical/straightforward verifiable work to `opencode`.
- Place the downstream tickets in a dedicated numeric range and leave gaps for later inserts.

## Acceptance criteria
- A design memo exists that names the final orchestration objects and maps them to current CAR files.
- Downstream tickets are created for the core orchestration package and target model.
- Every downstream ticket produced by this scope ticket:
  - references the exact files/modules it expects to touch
  - has explicit acceptance criteria and tests
  - assigns `codex` for complex implementation and `opencode` for straightforward verifiable work
  - uses only lower-numbered dependencies
- The scope write-up does not leave the PMA path as a special-case bypass.

## Tests
- Verify the generated tickets are valid CAR ticket markdown with required frontmatter.
- Verify generated ticket numbers are ascending and dependency-safe.
- Verify no generated ticket describes ticket flow as a normal conversational thread target.

## Outcome

The final orchestration vocabulary from this ticket is:
- `AgentDefinition`
- `ThreadTarget`
- `MessageRequest`
- `ExecutionRecord`
- `FlowTarget`
- `Binding`
- `TargetCapability`

The shared boundary to generalize beneath PMA is one thin `src/codex_autorunner/core/orchestration/` service layer that owns orchestration ids, target resolution, and runtime-thread dispatch while delegating runtime operations to `AgentHarness`.
