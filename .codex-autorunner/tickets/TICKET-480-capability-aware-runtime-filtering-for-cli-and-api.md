---
title: "Add capability-aware runtime filtering for CLI and API surfaces"
agent: "opencode"
done: true
goal: "Make CAR surfaces auto-filter runtime operations based on the expanded capability model instead of assuming every adapter supports the same controls."
---

## Why this ticket exists

`TICKET-140` and `TICKET-420` require capability discovery to drive human- and agent-facing feature filtering. Once the runtime capability vocabulary is expanded, CLI and API surfaces need a straightforward pass to present only the features each adapter actually supports.

Run order note:
- Run this ticket after `TICKET-420`, `TICKET-440`, and `TICKET-460`.
- Land this surface filtering before the final adapter conformance pass.

## Expected files/modules to touch

- `src/codex_autorunner/surfaces/cli/pma_cli.py`
- `src/codex_autorunner/surfaces/web/routes/pma_routes/managed_thread_runtime.py`
- `src/codex_autorunner/core/orchestration/catalog.py`
- `tests/surfaces/cli/test_pma_cli_capabilities.py`
- `tests/surfaces/web/routes/pma_routes/test_managed_thread_runtime.py`

## Tasks

- Update CLI capability discovery and filtering to hide or disable actions when the selected runtime lacks the required capability.
- Update the managed-thread web/runtime routes to use the orchestration-visible capability view instead of ad hoc backend assumptions.
- Make interrupt, review, model-listing, transcript-history, and streaming affordances capability-aware.
- Ensure the filtering logic treats ZeroClaw as a normal adapter consumer of the shared capability model, not as a one-off special case.
- Preserve the v1 boundary that only durable thread/session runtimes appear as supported thread targets.

## Acceptance criteria

- CLI and web/runtime surfaces use the expanded capability model to filter operations.
- Unsupported actions fail with clear capability errors instead of falling through to backend-specific surprises.
- ZeroClaw-visible limitations or richer features appear through the shared capability view rather than bespoke branching.
- No UI/API path in this ticket implies support for single-session runtimes.

## Tests

- Add CLI coverage in `tests/surfaces/cli/test_pma_cli_capabilities.py` for capability-aware filtering and error messaging.
- Add web/runtime route coverage in `tests/surfaces/web/routes/pma_routes/test_managed_thread_runtime.py` for capability-gated actions such as interrupt and review.
- Run `.venv/bin/python -m pytest tests/surfaces/cli/test_pma_cli_capabilities.py tests/surfaces/web/routes/pma_routes/test_managed_thread_runtime.py`.
