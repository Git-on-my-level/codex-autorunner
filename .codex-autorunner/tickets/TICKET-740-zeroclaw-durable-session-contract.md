---
title: "Make the ZeroClaw adapter truthful against the durable-session contract"
agent: "codex"
done: true
goal: "Resolve the current mismatch between the documented durable-session contract and the ZeroClaw integration by either wiring the adapter to real durable ZeroClaw sessions or explicitly downgrading its contract, capabilities, and registration so CAR is not claiming stronger semantics than it actually provides."
---

## Why
The docs say CAR v1 orchestration requires durable thread/session support across restarts and call the ZeroClaw adapter the reference wrapper pattern. The current `ZeroClawSupervisor` fabricates session ids and stores session handles only in memory, so those ids do not survive a CAR restart and cannot be reattached from durable state. That contradiction needs to be removed before the orchestration layer can honestly present ZeroClaw as a generic agent target.

## Tasks
- Inspect the ZeroClaw public API / documented runtime surface and determine whether it exposes a real durable session/thread primitive that CAR can adopt.
- If ZeroClaw has a real durable session primitive:
  - integrate against it directly
  - persist the necessary durable handle/reference in orchestration state
  - make `resume_conversation()` and related operations work across CAR restarts
- If ZeroClaw does **not** have a real durable session primitive:
  - stop advertising it as satisfying the CAR v1 durable-thread contract
  - remove or downgrade the corresponding capabilities
  - move any wrapper-managed volatile session behavior behind explicit experimental wording
  - update docs and registration so the contract is truthful everywhere
- Ensure capability discovery / CLI filtering reflects the final truth automatically.
- Update the docs that currently present ZeroClaw as the reference example so they match the final implementation.

## Acceptance criteria
- There is one coherent story across code, capabilities, registration, and docs for what ZeroClaw supports.
- If ZeroClaw is kept as a CAR v1 orchestration target, its session/conversation handles are durable and resumable under the published contract.
- If ZeroClaw cannot meet that contract, CAR no longer claims that it does.
- Capability queries and CLI output reflect the final support level without hand-wavy exceptions.

## Tests
- Add restart-resilience tests if ZeroClaw is upgraded to real durable sessions.
- Add capability/registration tests proving ZeroClaw is surfaced correctly under the final contract.
- Add doc-oriented verification or snapshot tests if helpful to prevent future drift between implementation and documentation.

## Progress
- Inspected the current ZeroClaw integration and local runtime surface:
  - `ZeroClawSupervisor` fabricates `zeroclaw-session-<uuid>` ids and keeps all session handles in the in-memory `_handles` map.
  - `ZeroClawClient` wraps one live interactive `zeroclaw agent` subprocess per wrapper session and exposes no CAR-visible durable handle beyond that volatile wrapper id.
  - The `zeroclaw` binary is not installed in this workspace, so there was no documented CLI session API to adopt beyond the local wrapper/client code already in-tree.
- Resolved the contract mismatch by downgrading ZeroClaw instead of continuing to advertise it as a CAR v1 durable-thread target:
  - `ZeroClawHarness` and the built-in `zeroclaw` registry descriptor now advertise no orchestration capabilities.
  - `ZeroClawSupervisor` documentation now describes the wrapper as a volatile session owner rather than a durable-session owner.
  - Orchestration thread-target creation now rejects agents that do not advertise `durable_threads`, so managed-thread creation no longer accepts ZeroClaw.
- Updated docs to match the new truthful story:
  - `docs/plugin-api.md` and `docs/adding-an-agent.md` now describe the downgrade path for volatile wrappers and explicitly position ZeroClaw as an experimental non-durable wrapper, not the durable-thread reference example.
- Updated coverage to lock the new contract in place:
  - ZeroClaw harness/registry tests now assert that it no longer advertises `durable_threads`, `message_turns`, `active_thread_discovery`, or `event_streaming`.
  - PMA managed-thread route coverage now asserts that ZeroClaw thread creation is rejected because it lacks `durable_threads`.
  - Orchestration service coverage now asserts that non-durable agents cannot create thread targets.

## Validation
- Focused contract-truth validation:
  - `.venv/bin/ruff check src/codex_autorunner/agents/zeroclaw/supervisor.py src/codex_autorunner/agents/zeroclaw/harness.py src/codex_autorunner/agents/registry.py src/codex_autorunner/core/orchestration/service.py src/codex_autorunner/surfaces/web/routes/pma_routes/managed_threads.py tests/agents/zeroclaw/test_zeroclaw_harness.py tests/agents/test_plugin_agent_conformance.py tests/agents/test_registry_capabilities.py tests/test_agents_registry.py tests/routes/test_agents_routes.py tests/test_pma_managed_threads_routes.py tests/core/orchestration/test_service.py`
  - `.venv/bin/mypy src/codex_autorunner/agents/zeroclaw/harness.py src/codex_autorunner/agents/registry.py src/codex_autorunner/core/orchestration/service.py src/codex_autorunner/surfaces/web/routes/pma_routes/managed_threads.py`
  - `.venv/bin/pytest tests/agents/zeroclaw/test_zeroclaw_harness.py tests/agents/test_plugin_agent_conformance.py tests/agents/test_registry_capabilities.py tests/test_agents_registry.py tests/routes/test_agents_routes.py tests/test_pma_managed_threads_routes.py tests/core/orchestration/test_service.py -q`
  - Result: `All checks passed!`; `Success: no issues found in 4 source files`; `94 passed`
