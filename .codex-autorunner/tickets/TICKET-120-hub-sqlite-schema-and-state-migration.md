---
title: "Scope the hub SQLite schema and migrate orchestration state out of JSON/JSONL"
agent: "codex"
done: true
goal: "Define the exact hub SQLite schema, authority boundaries, and migration tickets for orchestration state, including the current PMA automation/thread/transcript state."
---

## Progress

- [x] Read `docs/STATE_ROOTS.md`, `src/codex_autorunner/core/pma_thread_store.py`, `src/codex_autorunner/core/pma_automation_store.py`, `src/codex_autorunner/core/pma_automation_persistence.py`, `src/codex_autorunner/core/pma_transcripts.py`, plus current PMA queue/reactive/audit/lifecycle persistence seams.
- [x] Wrote `.codex-autorunner/contextspace/hub-sqlite-schema-and-state-migration.md` to lock the canonical hub SQLite path, authority boundaries, table split, and transcript mirror rule.
- [x] Reserved the `320-400` downstream range for state migration work from this scope ticket.
- [x] Created downstream tickets:
  - `TICKET-320-orchestration-hub-sqlite-schema.md`
  - `TICKET-340-migrate-pma-thread-automation-and-queue-state.md`
  - `TICKET-360-migrate-transcript-mirrors-and-event-projections.md`
  - `TICKET-380-update-state-roots-and-ops-docs-for-orchestration-sqlite.md`
  - `TICKET-400-add-migration-verification-and-rollback-checks.md`
- [x] Kept repo-local `flows.db` authoritative for flow-engine internals and transport DBs authoritative for transport delivery state.

## Tasks
- Read `spec.md` sections on state ownership and compare them to:
  - `docs/STATE_ROOTS.md`
  - `src/codex_autorunner/core/pma_thread_store.py`
  - `src/codex_autorunner/core/pma_automation_store.py`
  - `src/codex_autorunner/core/pma_automation_persistence.py`
  - `src/codex_autorunner/core/pma_transcripts.py`
  - any current queue/event artifacts that should move into SQLite
- Produce a schema memo that defines:
  - the canonical hub SQLite path
  - authoritative tables
  - any projection tables versus authoritative tables
  - authority boundaries for repo-local `flows.db`, transport state DBs, and hub SQLite
- Decide exactly what transcript text is mirrored and what is explicitly excluded.
- Write downstream implementation tickets for:
  - SQLite schema creation/migrations
  - backfill/migration logic from existing PMA stores
  - transcript mirror migration
  - state-roots documentation updates
  - verification and rollback checks
- Assign complex migration work to `codex` and straightforward verification/documentation work to `opencode`.

## Acceptance criteria
- A schema memo exists and is consistent with `spec.md`.
- Downstream tickets exist for all state migration and schema work needed for the build.
- The downstream tickets clearly state what remains authoritative in repo-local flow state versus hub SQLite projections.
- No generated ticket leaves JSON/JSONL as the canonical source of orchestration state.
- The migration ticket set includes validation/consistency checks rather than only code changes.

## Tests
- Verify the generated tickets include explicit migration verification steps.
- Verify the tickets update `docs/STATE_ROOTS.md` and any related docs/contracts as needed.
- Verify transcript storage tickets preserve the “plain text only, non-authoritative mirror” rule.

## Outcome

The canonical hub orchestration database from this ticket is:
- `<hub_root>/.codex-autorunner/orchestration.sqlite3`

The primary authoritative tables locked by this scope are:
- `orch_thread_targets`
- `orch_thread_executions`
- `orch_thread_actions`
- `orch_automation_subscriptions`
- `orch_automation_timers`
- `orch_automation_wakeups`
- `orch_queue_items`
- `orch_reactive_debounce_state`

The primary mirror/projection tables locked by this scope are:
- `orch_transcript_mirrors`
- `orch_event_projections`
- `orch_flow_run_projections`
- `orch_audit_entries`
