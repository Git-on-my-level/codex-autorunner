---
title: "Generalize PMA web and CLI surfaces onto orchestration services"
agent: "codex"
done: true
goal: "Keep PMA user-facing workflows intact while removing PMA-only managed-thread routing as the long-term execution center."
---

## Why this ticket exists

`TICKET-160` concluded that PMA should remain available as both a client and an addressable agent, but its routes and CLI commands must stop owning the generic runtime-thread path. This ticket converts PMA web and CLI surfaces into orchestration-backed shims.

Run order note:
- Run this ticket after `TICKET-520` and `TICKET-540`.
- Land this PMA generalization before the later query and status/tail UX follow-through.

## Expected files/modules to touch

- `src/codex_autorunner/surfaces/web/routes/pma_routes/managed_thread_runtime.py`
- `src/codex_autorunner/surfaces/web/routes/pma_routes/managed_threads.py`
- `src/codex_autorunner/surfaces/web/routes/pma_routes/tail_stream.py`
- `src/codex_autorunner/surfaces/cli/pma_cli.py`
- `src/codex_autorunner/core/orchestration/service.py`
- `src/codex_autorunner/core/orchestration/interfaces.py`
- `docs/ops/pma-managed-thread-status.md`
- `tests/test_pma_cli.py`
- `tests/test_pma_routes.py`
- `tests/test_pma_managed_threads_routes.py`
- `tests/test_pma_managed_threads_messages.py`
- `tests/core/orchestration/test_interfaces.py`

## Tasks

- Replace direct backend branching in PMA web runtime routes with orchestration service calls for send, interrupt, resume, and status/tail lookup.
- Convert `car pma thread ...` commands into orchestration-backed shims while preserving familiar verbs and output shape where practical.
- Keep PMA-specific slash-command or operator UX affordances, but make them consumers of orchestration-owned thread state instead of the owners of thread execution semantics.
- Ensure PMA remains both:
  - an orchestration client for its own chat and control surfaces
  - an orchestration-addressable agent definition for delegation or cross-agent coordination
- Preserve queue/default-admission behavior and automation wakeup semantics through orchestration instead of PMA-only stores.

## Acceptance criteria

- PMA web and CLI surfaces call orchestration services for thread execution and control.
- PMA UX remains intact enough for existing operators, but PMA no longer serves as a bypass around generic orchestration logic.
- PMA thread status and automation behavior are expressed through orchestration-owned services and state.
- No direct surface-owned Codex/OpenCode branch remains the primary runtime path for PMA thread actions.

## Tests

- Extend `tests/test_pma_cli.py` to show PMA thread commands hit orchestration-backed endpoints or services rather than PMA-only runtime branches.
- Extend `tests/test_pma_routes.py`, `tests/test_pma_managed_threads_routes.py`, and `tests/test_pma_managed_threads_messages.py` for orchestration-backed send, interrupt, and status behavior.
- Run `.venv/bin/python -m pytest tests/test_pma_cli.py tests/test_pma_routes.py tests/test_pma_managed_threads_routes.py tests/test_pma_managed_threads_messages.py`.

## Completed

- Added the missing orchestration query/control seam for PMA-owned thread reads and controls:
  - `archive_thread_target`
  - `get_latest_execution`
- Moved PMA managed-thread create/list/get/resume/archive routes onto the orchestration service seam, leaving PMA-only store usage only for narrow compatibility/audit shims and compaction/turn history paths.
- Moved PMA managed-thread status/tail reads onto orchestration-backed thread/execution queries, while preserving the existing operator-facing payload shape and event-stream behavior.
- Kept PMA thread send/interrupt on the orchestration-backed runtime execution path and reused the same managed-thread orchestration service builder across PMA runtime routes.
- Added CLI coverage proving the PMA thread verbs keep hitting the orchestration-backed PMA thread route contract for:
  - `create`
  - `list`
  - `info`
  - `status`
  - `tail`
  - `resume`
  - `archive`
- Added route coverage proving the PMA managed-thread CRUD and status/tail endpoints resolve through the orchestration service seam rather than direct PMA store reads.
- Updated `docs/ops/pma-managed-thread-status.md` to note that PMA thread query/control/status surfaces now consume orchestration-backed state.

## Verification

- Passed `.venv/bin/python -m pytest tests/test_pma_cli.py tests/test_pma_routes.py tests/test_pma_managed_threads_routes.py tests/test_pma_managed_threads_messages.py`
- Passed `.venv/bin/python -m pytest tests/core/orchestration/test_interfaces.py`
