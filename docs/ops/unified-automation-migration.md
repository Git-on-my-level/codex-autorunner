# Unified Automation Migration Note

Existing PMA timer/subscription APIs and GitHub reaction config remain usable,
but they are explicit adapters over the unified automation plane. Normal runtime
does not backfill legacy PMA rows into first-class automation rules.

## PMA Timers And Subscriptions

- `POST /hub/pma/subscriptions` creates a system-owned automation rule through
  the canonical automation store.
- `POST /hub/pma/timers` creates a canonical automation schedule.
- Lifecycle transitions are recorded as normalized automation events. Matching
  rules enqueue PMA jobs in `orch_automation_jobs`.
- Existing legacy subscription, timer, and wakeup rows are retained only for
  diagnostics and explicit operator migration. Unsupported old shapes fail with
  stable `PMA_LEGACY_AUTOMATION_*` diagnostics instead of silently materializing
  runtime rules.

Operators should inspect canonical state in hub `orchestration.sqlite3`
automation tables. `.codex-autorunner/pma/automation_store.json` is a
compatibility mirror for old PMA adapter state, not a supported runtime source.

## Diagnostics And Release Gate

Automation migration blockers are exposed through stable JSON diagnostics:

```bash
car doctor --json
car hub orchestration status --json
car pma automation migration-status --json
```

The machine-readable payload reports pending orchestration schema versions,
legacy PMA automation residue, malformed compatibility rows, mirror health, and
operator next steps. Blockers use stable codes such as
`AUTOMATION_MIGRATION_SCHEMA_PENDING`,
`AUTOMATION_MIGRATION_LEGACY_BACKFILL_PENDING`,
`AUTOMATION_MIGRATION_MIRROR_INCOMPLETE`, and the row-level
`PMA_LEGACY_AUTOMATION_*` diagnostics raised by the explicit PMA migration.

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
