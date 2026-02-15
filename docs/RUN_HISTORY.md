# Run History Contract

This document defines the canonical run-history model in Codex Autorunner.

## Source Of Truth

Use `FlowStore` at `.codex-autorunner/flows.db` as the single source of truth for run history.

- Runs: `flow_runs`
- Timeline/events: `flow_events`
- Artifacts: `flow_artifacts`

Legacy `run_index` / numeric run directories are compatibility-only and must not be used for new run history features.

## What Runs Exist

Use FlowStore run records:

1. Open FlowStore for the repo.
2. Query `list_flow_runs(...)` (optionally by `flow_type` or status).
3. Treat each `FlowRunRecord.id` (string) as the canonical run id.

Primary fields:

- `id`
- `flow_type`
- `status`
- `created_at`, `started_at`, `finished_at`
- `current_step`
- `metadata`

## What Happened In A Run

Use FlowStore events:

1. Query `get_events(run_id, ...)`.
2. Sort/order by `seq` (monotonic per DB).
3. Render timeline using `timestamp`, `event_type`, `data`, and optional `step_id`.

For streaming/polling use cases, use `after_seq` and event-type filters (`get_events_by_type(s)`).

## Where Artifacts Live

Use FlowStore artifacts:

1. Query `get_artifacts(run_id)`.
2. Resolve each artifact by `kind`, `path`, `created_at`, and `metadata`.
3. `path` may be absolute or repo-relative; resolve against repo root before reading.

Artifact discovery must come from `flow_artifacts` first. Filesystem scanning is fallback-only for legacy compatibility paths.

## Compatibility And Deprecation

- `core/run_index.py` is deprecated (legacy migration/support only).
- `RuntimeContext.reconcile_run_index()` is deprecated and now a compatibility no-op.
- Legacy numeric run logs (`.codex-autorunner/runs/<int>/run.log`) are deprecated and not canonical for new runs.

Removal plan:

1. Keep read-only compatibility where old surfaces still reference legacy run ids/logs.
2. Migrate remaining surfaces to FlowStore ids/events/artifacts.
3. Remove legacy run-index APIs and numeric-run log plumbing in a dedicated cleanup ticket.
