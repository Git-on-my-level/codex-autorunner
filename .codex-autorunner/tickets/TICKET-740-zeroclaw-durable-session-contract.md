---
title: "Make the ZeroClaw adapter truthful against the durable-session contract"
agent: "codex"
done: false
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
