# Unified Automation Migration Note

Generalized automation is the canonical runtime path. PMA timer/subscription
route names are aliases that write directly to the unified automation plane.
Normal runtime does not backfill legacy PMA rows into first-class automation
rules.

## Managed-Thread Timers And Subscriptions

- `car automation ...` is the promoted CLI for scheduled product automations.
- `car pma automation ...` remains an alias for the same generalized commands.
- `car pma thread spawn/send --notify-on terminal` creates managed-thread
  terminal follow-up through the canonical automation store.
- `car pma thread subscribe --id <managed_thread_id>` creates a thread-scoped
  lifecycle subscription rule.
- `car hub subscription list/cancel` inspects or disables managed-thread
  lifecycle subscription rules.
- `POST /hub/pma/subscriptions` creates a system-owned automation rule through
  the canonical automation store.
- `POST /hub/pma/timers` creates a canonical automation schedule.
- Lifecycle transitions are recorded as normalized automation events. Matching
  rules enqueue jobs in `orch_automation_jobs`.
- Existing legacy subscription, timer, and wakeup rows are diagnostic residue
  only. They are not imported, mirrored, or validated as runtime input.

Operators should inspect canonical state in hub `orchestration.sqlite3`
automation tables.

## Diagnostics And Release Gate

Automation migration blockers are exposed through stable JSON diagnostics:

```bash
car doctor --json
car hub orchestration status --json
car automation migration-status --json
```

The machine-readable payload reports pending orchestration schema versions,
legacy PMA automation residue, legacy executor shapes still present in unified
automation tables, and operator next steps. Legacy PMA automation rows are now
reported only as `AUTOMATION_MIGRATION_LEGACY_RESIDUE`; operators should delete
those rows or drop the retired tables instead of migrating them forward.

The release gate must run:

```bash
.venv/bin/python scripts/check_migration_observability_docs.py
```

The check keeps these commands documented alongside the code paths that emit the
diagnostics.

## GitHub Reactions

`github.automation.reactions` seeds or updates built-in SCM automation rules.
SCM webhooks and polling still ingest and normalize GitHub events, but reaction
work is represented as automation jobs with publish-operation refs and attempts.

Disable built-in reaction behavior by disabling the corresponding automation
rule or using the existing reaction profile configuration that seeds disabled
rules.
