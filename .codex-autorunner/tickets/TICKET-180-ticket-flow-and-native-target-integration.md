---
title: "Scope ticket_flow integration and explicit CAR-native targets under orchestration"
agent: "codex"
done: true
goal: "Write the downstream implementation tickets that integrate ticket_flow as a first-class flow target, unify dispatch/event visibility, and clarify CAR-native targets such as PMA and other built-in agent-like behaviors."
---

## Progress

- [x] Read the flow-target, `ticket_flow`, event-normalization, and native-target sections of `.codex-autorunner/contextspace/spec.md`.
- [x] Reviewed the current flow and dispatch seams in `src/codex_autorunner/core/flows/*`, `src/codex_autorunner/flows/ticket_flow/definition.py`, `src/codex_autorunner/surfaces/cli/commands/flow.py`, `src/codex_autorunner/core/pma_dispatches.py`, `src/codex_autorunner/core/flows/pause_dispatch.py`, and `src/codex_autorunner/core/pma_dispatch_interceptor.py`.
- [x] Confirmed that `FlowDefinition` / `FlowController` / `FlowRuntime` / `FlowStore` already own real flow semantics and should remain authoritative for flow-engine internals.
- [x] Confirmed that flow status, event, and dispatch visibility is currently split across flow-store events, flow route history/status handlers, Telegram `ticket_flow` pause bridging, and PMA dispatch helpers.
- [x] Wrote `.codex-autorunner/contextspace/ticket-flow-and-native-target-integration.md` to lock the flow-target versus thread-target boundary, `ticket_flow` orchestration mapping, dispatch/event projection model, and the list of CAR-native targets that should become registry-visible.
- [x] Reserved the `620-680` downstream range for flow-target wrapping, event/dispatch projection, orchestration flow controls, and native-target registry/docs work from this scope ticket.
- [x] Created downstream tickets:
  - `TICKET-620-wrap-ticket-flow-as-an-orchestration-flow-target.md`
  - `TICKET-640-project-flow-lifecycle-and-dispatch-events-into-orchestration.md`
  - `TICKET-660-route-flow-cli-and-api-controls-through-orchestration.md`
  - `TICKET-680-make-car-native-targets-explicit-in-registry-and-docs.md`
- [x] Kept `ticket_flow` as a native flow target and did not scope any rewrite that would turn it into a standard thread adapter.

## Tasks
- Read `spec.md` sections on flow targets, ticket flow, event normalization, and CAR-native targets.
- Review the current flow and dispatch code:
  - `src/codex_autorunner/core/flows/*`
  - `src/codex_autorunner/flows/ticket_flow/definition.py`
  - `src/codex_autorunner/surfaces/cli/commands/flow.py`
  - `src/codex_autorunner/core/pma_dispatches.py`
  - `src/codex_autorunner/core/flows/pause_dispatch.py`
  - `src/codex_autorunner/core/pma_dispatch_interceptor.py`
- Produce a memo that states:
  - how flow targets differ from thread targets in orchestration
  - how `ticket_flow` lifecycle commands map into orchestration
  - how flow dispatches/events project into the unified event surface
  - which built-in CAR-native targets should become explicitly registry-visible
- Write downstream implementation tickets for:
  - the flow-target orchestration wrapper
  - event/dispatch normalization and projection
  - CLI/API control of flow targets through orchestration
  - any explicit registry or docs work needed for CAR-native targets
- Assign complex flow/orchestration glue work to `codex` and straightforward projection/tests/docs work to `opencode`.

## Acceptance criteria
- A memo exists that clearly distinguishes flow targets from thread targets.
- Downstream tickets exist for wrapping `ticket_flow` instead of rewriting it.
- Generated tickets keep current ticket-flow lifecycle semantics intact while making them discoverable and invokable through orchestration.
- Generated tickets include dispatch/event projection work so flows and threads can share a common observability surface.

## Tests
- Verify the generated tickets do not flatten `ticket_flow` into a standard thread adapter.
- Verify the generated tickets cover start/resume/stop/status and dispatch visibility.
- Verify generated ticket dependencies stay one-directional and ascending.

## Outcome

The flow-target boundary locked by this ticket is:
- `ticket_flow` is a `FlowTarget`, not a conversational thread target
- `FlowController` / `FlowRuntime` / `FlowStore` remain authoritative for flow-engine internals
- orchestration wraps and projects native flow behavior instead of replacing it
- flow lifecycle and dispatches join the shared observability surface through thin projections

The explicit CAR-native targets identified here are:
- `ticket_flow` as a native flow target
- future CAR-native flows registered through the same flow-target path
- PMA as a CAR-native orchestration-addressable agent/thread target once generalized

The native behaviors explicitly left out of the target registry are:
- `PmaDispatchInterceptor`
- reactive/debounce helpers
- Codex/OpenCode CAR-context helpers that are still normal runtime-backed agent definitions
