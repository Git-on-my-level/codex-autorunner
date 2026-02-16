# Architecture Refactor Handoff (Tickets 200-270)

This document is the canonical handoff artifact for the architecture-boundary refactor stream (tickets `TICKET-200` through `TICKET-270`).

## Scope and queue review

- Reviewed ticket order and completion status in `.codex-autorunner/tickets/`:
  - `TICKET-200`, `TICKET-210`, `TICKET-220`, `TICKET-230`, `TICKET-240`, `TICKET-250`, `TICKET-260`: `done: true`
  - `TICKET-270`: final review/PR prep (this handoff)
- Queue order is sequential and dependency-safe for this stream.

## Acceptance/test evidence

- Full suite:
  - `make test` -> `1272 passed, 3 skipped, 76 deselected` (2026-02-15)
- Blocking architecture/run-history/state-root checks:
  - `.venv/bin/python -m pytest tests/test_architecture_boundaries.py -q` -> `3 passed`
  - `.venv/bin/python -m pytest tests/test_review_context.py -q` -> `2 passed`
  - `.venv/bin/python -m pytest tests/core/test_state_roots.py -q` -> `25 passed`

## Ticket-to-change map (files changed per ticket commit)

### TICKET-200 (`9ab9d1da`)
- `docs/ARCHITECTURE_BOUNDARIES.md`
- `tests/test_architecture_boundaries.py`
- `tests/test_core_web_boundary.py`

### TICKET-210 (`08d753cc`)
- `src/codex_autorunner/integrations/agents/__init__.py`
- `src/codex_autorunner/integrations/agents/agent_pool_impl.py`
- `src/codex_autorunner/integrations/agents/build_agent_pool.py`
- `src/codex_autorunner/integrations/telegram/handlers/commands/flows.py`
- `src/codex_autorunner/integrations/telegram/ticket_flow_bridge.py`
- `src/codex_autorunner/surfaces/cli/cli.py`
- `src/codex_autorunner/surfaces/web/routes/flows.py`
- `src/codex_autorunner/tickets/agent_pool.py`
- `tests/test_opencode_agent_pool.py`
- `tests/test_ticket_flow_approval_config.py`

### TICKET-220 (`d7d4b51c`)
- `src/codex_autorunner/core/ports/backend_orchestrator.py`
- `src/codex_autorunner/integrations/agents/agent_pool_impl.py`
- `src/codex_autorunner/integrations/agents/backend_orchestrator.py`
- `src/codex_autorunner/integrations/agents/codex_backend.py`
- `src/codex_autorunner/integrations/agents/wiring.py`
- `tests/test_opencode_agent_pool.py`
- `tests/test_ticket_flow_approval_config.py`

### TICKET-230 (`f3b48f54`)
- `src/codex_autorunner/core/app_server_utils.py`
- `src/codex_autorunner/integrations/agents/agent_pool_impl.py`
- `src/codex_autorunner/integrations/agents/codex_backend.py`
- `src/codex_autorunner/integrations/agents/wiring.py`
- `src/codex_autorunner/integrations/app_server/client.py`
- `src/codex_autorunner/integrations/app_server/env.py`
- `src/codex_autorunner/integrations/app_server/event_buffer.py`
- `src/codex_autorunner/integrations/app_server/ids.py`
- `tests/test_app_server_supervisor.py`
- `tests/test_codex_backend_security.py`

### TICKET-240 (`78e970c5`)
- `docs/RUN_HISTORY.md`
- `src/codex_autorunner/core/review_context.py`
- `src/codex_autorunner/core/runner_controller.py`
- `src/codex_autorunner/core/runtime.py`
- `src/codex_autorunner/surfaces/web/routes/repos.py`
- `tests/test_review_context.py`

### TICKET-250 (`c5b2001b`, `d6f7cea9`)
- `pyproject.toml`
- `src/codex_autorunner/surfaces/cli/cli.py`
- `src/codex_autorunner/surfaces/cli/commands/__init__.py`
- `src/codex_autorunner/surfaces/cli/commands/dispatch.py`
- `src/codex_autorunner/surfaces/cli/commands/doctor.py`
- `src/codex_autorunner/surfaces/cli/commands/flow.py`
- `src/codex_autorunner/surfaces/cli/commands/hub.py`
- `src/codex_autorunner/surfaces/cli/commands/hub_runs.py`
- `src/codex_autorunner/surfaces/cli/commands/hub_tickets.py`
- `src/codex_autorunner/surfaces/cli/commands/inbox.py`
- `src/codex_autorunner/surfaces/cli/commands/repos.py`
- `src/codex_autorunner/surfaces/cli/commands/root.py`
- `src/codex_autorunner/surfaces/cli/commands/templates.py`
- `src/codex_autorunner/surfaces/cli/commands/telegram.py`
- `src/codex_autorunner/surfaces/cli/commands/utils.py`
- `src/codex_autorunner/surfaces/cli/commands/worktree.py`
- `src/codex_autorunner/surfaces/web/app.py`
- `src/codex_autorunner/surfaces/web/app_builders.py`
- `src/codex_autorunner/surfaces/web/routes/hub_messages.py`
- `src/codex_autorunner/surfaces/web/routes/hub_repos.py`

### TICKET-260 (`34528a89`)
- `.deadcode-baseline.json`
- `docs/STATE_ROOTS.md`
- `src/codex_autorunner/contextspace/paths.py`
- `src/codex_autorunner/core/runtime.py`
- `src/codex_autorunner/core/state_roots.py`
- `tests/core/test_state_roots.py`

## Architectural decisions and rationale

- Enforce one-way layer boundaries in CI to prevent incremental architecture drift.
- Treat `AgentPool` as a ticket-flow port and move concrete agent execution to integrations adapters.
- Route all non-interactive turn execution through one orchestrated backend path for consistency and reduced duplicate logic.
- Centralize Codex app-server lifecycle ownership in the supervisor to avoid split ownership/race risks.
- Converge run-history reads on `FlowStore` and deprecate legacy `run_index` paths.
- Split monolithic web/CLI surface modules into composition roots plus focused command/route modules.
- Make state-root contracts explicit and testable; define cache locations as non-canonical.

## Remaining follow-up/debt

- `tests/test_core_web_boundary.py` remains in tree although architecture boundary enforcement is now centralized; consider decommissioning once no legacy value remains.
- Continue opportunistic shrinkage of large extracted modules (for example `surfaces/web/routes/hub_repos.py`) as separate, low-risk refactors.
