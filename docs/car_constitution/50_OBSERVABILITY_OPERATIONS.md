# Observability & Operations

Principle: observability replaces safety prompts. YOLO is acceptable if behavior is reconstructible from artifacts.

## Run identity
Every run must have:
- stable run_id
- timestamps (start/end/last progress)
- actor identity (agent/backend/surface)
- execution mode (yolo/safe/review)

## Canonical event + log stream
- Logs are append-only and attributable to a run.
- Prefer structured “run events” for phase transitions and key decisions.
- Normalize streaming differences across backends into a consistent representation.

## State transitions
Record transitions and blocking points:
- phase changes
- waiting on IO/subprocess/agent
- retries and backoffs
- timeout boundaries

## Failure is first-class
- Timeouts are failures with evidence.
- Partial output must be explicitly labeled.
- Swallowed exceptions and silent fallbacks are disallowed.

## Debugging order (operational heuristic)
1) Identify run_id
2) Read run metadata
3) Scan state transitions/events
4) Inspect stdout/stderr/stream artifacts
5) Only then inspect code

## Debugging Ticket-Flow Recovery
For a crashed or restarting ticket flow, keep the reducer/supervisor path as the
mental model:

1) Identify the run with `car ticket-flow status --run-id <run_id>` or from
   `.codex-autorunner/flows.db`.
2) Inspect `.codex-autorunner/flows/<run_id>/worker.exit.json` for process-exit
   evidence. A stale reaper kill is marked with `exit_origin: stale_reaper`,
   `exit_kind: reaped_stale`, and `shutdown_intent: false`.
3) Inspect `.codex-autorunner/flows/<run_id>/crash.json` for the canonical
   crash/reap payload used by surfaces and failure events.
4) Inspect flow events and telemetry in `flows.db` for reconcile transitions,
   `recovery_takeover`, failure projections, restart attempts, and restart
   exhaustion.
5) If the current ticket is `done: true`, check `git status --porcelain`.
   Dirty work means the commit barrier should preserve the current ticket and
   prevent advancement until commit/recovery is resolved.

Do not treat a killed worker as a user shutdown unless the worker exit evidence
was written by the user-stop path. Stale reaper evidence is recovery evidence;
the reconciler/supervisor decides the user-visible state.

## Minimum “explainability” bar
From artifacts alone, a debugger must be able to answer:
- what happened?
- why did it happen?
- where did it fail/stall?
- what is the safest replay/retry path?

## Web read-model diagnostics
- Projection lag, snapshot latency, event journal latency, stream lag, cursor
  gaps, and virtualized DOM row counts are part of the Web Hub responsiveness
  budget.
- Use `docs/ops/web-read-models.md` for the concrete rebuild and diagnostic
  commands before changing projection code.
- Chat index/detail stream cursor repair is surfaced as `projection.cursor_gap`
  with a `repair.snapshotRoute` pointing back to `/hub/read-models/chats*`.
- A stream or snapshot regression must identify the failing family: chat index,
  chat detail, repo/worktree topology, repo/worktree runtime, ticket detail, or
  a newer documented family.

## Ticket-flow visibility diagnostics
- Treat `.codex-autorunner/flows.db` as flow-engine execution state: it proves
  run sequencing, status, and terminal history.
- Treat hub orchestration records as Web Hub chat visibility state:
  `orch_thread_targets`, `orch_thread_executions`, bindings, delivery ledgers,
  and chat-surface events must rebuild the Chats view.
- A completed ticket-flow turn without a repairable
  `flow_run_id + ticket_id -> managed_thread_id` orchestration link is a
  projection gap. Hub Messages surfaces these gaps under
  `ticket_flow_visibility` diagnostics; repair/backfill should use the
  ticket-flow visibility repair path.
- Discord and Telegram terminal status watchers still have a temporary
  `flows.db` fallback while flow notifications finish migrating to canonical
  projections. These reads emit `legacy_flows_db_status_fallback` diagnostic log
  events and must not be expanded into new chat visibility sources.
