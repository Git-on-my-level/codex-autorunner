# Unified Automation Migration Note

Existing PMA timer/subscription APIs and GitHub reaction config remain usable,
but they are adapters over the unified automation plane.

## PMA Timers And Subscriptions

- `POST /hub/pma/subscriptions` creates a compatibility subscription row and a
  system-owned automation rule.
- `POST /hub/pma/timers` creates a compatibility timer row and an automation
  schedule.
- Lifecycle transitions are recorded as normalized automation events. Matching
  rules enqueue PMA jobs in `orch_automation_jobs`.
- Existing wakeup rows are retained only for compatibility and repair. Before
  execution, they are backfilled into automation events/jobs and then drained
  through the automation worker.

Operators should inspect canonical state in hub `orchestration.sqlite3`
automation tables. `.codex-autorunner/pma/automation_store.json` is a mirror for
legacy tooling and ad-hoc visibility.

## GitHub Reactions

`github.automation.reactions` seeds or updates built-in SCM automation rules.
SCM webhooks and polling still ingest and normalize GitHub events, but reaction
work is represented as automation jobs with publish-operation refs and attempts.

Disable built-in reaction behavior by disabling the corresponding automation
rule or using the existing reaction profile configuration that seeds disabled
rules.
