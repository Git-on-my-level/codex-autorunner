---
title: "Add migration verification, parity checks, and rollback guardrails"
agent: "opencode"
done: true
goal: "Provide deterministic checks that prove the orchestration-state migration is complete and safe before legacy PMA stores stop being treated as canonical."
---

## Why this ticket exists

State migration is incomplete if it only copies bytes. CAR needs deterministic parity checks, migration summaries, and rollback guardrails before operators can trust the new store.

Run order note:
- Run this ticket after `TICKET-340` and `TICKET-360`.

## Expected files/modules to touch

- `src/codex_autorunner/core/orchestration/__init__.py`
- `src/codex_autorunner/core/orchestration/verification.py`
- `src/codex_autorunner/surfaces/cli/commands/hub.py`
- `tests/core/orchestration/test_migration_verification.py`

## Tasks

- Add migration verification code that compares row counts and representative ids between legacy stores and `orchestration.sqlite3`.
- Add transcript parity checks based on mirrored text length and content hashes or comparable deterministic checks.
- Add queue and automation parity checks for pending/running items, idempotency keys, timers, and wakeups.
- Record migration-run summaries and failure diagnostics so rollback decisions are based on explicit evidence.
- Add rollback guardrails that keep legacy stores available as read-only fallback until verification passes.

## Acceptance criteria

- The migration path has explicit parity and completeness checks for thread, automation, queue, transcript, and event-projection data.
- Operators can see a clear success/failure summary rather than inferring migration health from logs alone.
- Rollback guidance or guardrails exist before legacy stores are fully deprecated.
- This ticket does not reintroduce JSON/JSONL as canonical state; it only uses legacy artifacts for verification and rollback safety.

## Tests

- Add coverage in `tests/core/orchestration/test_migration_verification.py` for parity-check behavior and failure reporting.
- Smoke `car hub orchestration verify --path <hub_root> --json` and `car hub orchestration status --path <hub_root> --json` against a migrated hub root.
- Run `.venv/bin/python -m pytest tests/core/orchestration/test_migration_verification.py`.
