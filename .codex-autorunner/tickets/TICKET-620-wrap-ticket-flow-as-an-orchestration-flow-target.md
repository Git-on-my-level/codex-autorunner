---
title: "Wrap ticket_flow as an orchestration flow target"
agent: "codex"
done: true
goal: "Expose ticket_flow through the orchestration service as a first-class flow target while preserving the native flow engine and flows.db as the canonical runtime."
---

## Why this ticket exists

`TICKET-180` locked the flow-target contract in `.codex-autorunner/contextspace/ticket-flow-and-native-target-integration.md`. `ticket_flow` already has the correct native flow semantics in `src/codex_autorunner/core/flows/*`; this ticket adds the orchestration wrapper layer instead of rewriting it as a thread adapter.

Run order note:
- Run this ticket after `TICKET-220`, `TICKET-240`, `TICKET-320`, and `TICKET-520`.
- Treat this as the foundation for later flow-event projection, control routing, and native-target visibility work.

## Expected files/modules to touch

- `src/codex_autorunner/core/orchestration/flows.py`
- `src/codex_autorunner/core/orchestration/service.py`
- `src/codex_autorunner/core/orchestration/models.py`
- `src/codex_autorunner/core/flows/controller.py`
- `src/codex_autorunner/flows/ticket_flow/runtime_helpers.py`
- `tests/core/orchestration/test_flow_targets.py`
- `tests/core/test_flow_controller_e2e.py`

## Tasks

- Add the orchestration-facing `FlowTarget` wrapper for `ticket_flow`.
- Map orchestration operations onto existing flow-controller behavior for:
  - start
  - resume
  - stop
  - status
  - list active runs
- Keep `flows.db` and flow artifacts authoritative for flow-engine internals.
- Ensure the wrapper preserves `ticket_flow` lifecycle semantics, including pause/resume and worker-backed execution.
- Do not introduce any adapter layer that makes `ticket_flow` look like a normal conversational thread target.

## Acceptance criteria

- `ticket_flow` is available as a first-class orchestration flow target.
- Orchestration control paths delegate to the existing flow engine instead of duplicating or replacing it.
- Repo-local `flows.db` remains authoritative for flow run internals.
- No code in this ticket flattens `ticket_flow` into a thread adapter or chat session abstraction.

## Tests

- Add coverage in `tests/core/orchestration/test_flow_targets.py` for start/resume/stop/status mapping through the orchestration flow wrapper.
- Extend `tests/core/test_flow_controller_e2e.py` to show orchestration wrapping preserves existing `ticket_flow` lifecycle behavior.
- Run `.venv/bin/python -m pytest tests/core/orchestration/test_flow_targets.py tests/core/test_flow_controller_e2e.py`.

## Completed

- Added the orchestration-facing `FlowRunTarget` model plus an `OrchestrationFlowService` contract for CAR-native flow targets.
- Wrapped `ticket_flow` behind a dedicated orchestration target wrapper/service instead of flattening it into a thread target:
  - `build_ticket_flow_target`
  - `TicketFlowTargetWrapper`
  - `FlowBackedOrchestrationService`
  - `build_ticket_flow_orchestration_service`
- Mapped orchestration flow operations onto the existing flow engine:
  - `start` and `resume` call the native `FlowController` path and preserve worker startup through `ensure_worker`
  - `stop` delegates to the native controller stop path
  - `status` and `list active runs` read from `flows.db` via `FlowStore`, keeping native flow state authoritative
- Added `FlowController.list_active_runs()` so orchestration-facing flow listings reuse controller semantics instead of duplicating status rules.
- Kept the flow wrapper import path lazy so package-level orchestration exports do not introduce `core` import-order regressions.
- Added focused unit coverage for wrapper/service operation mapping and an end-to-end pause/resume lifecycle test showing orchestration wrapping preserves `ticket_flow` behavior.

## Verification

- Passed `.venv/bin/python -m pytest tests/core/orchestration/test_flow_targets.py tests/core/test_flow_controller_e2e.py`
- Passed `.venv/bin/python -m pytest tests/core/orchestration/test_interfaces.py tests/core/orchestration/test_orchestration_models.py`
