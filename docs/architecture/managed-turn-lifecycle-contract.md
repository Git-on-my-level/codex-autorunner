# Managed Turn Lifecycle Contract

Managed-thread runtime completion must be separated from optional finalization
work. A turn reaches terminal orchestration state only when CAR durably records
one terminal outcome for that turn. Queue advancement depends on that durable
record, not on delivery, transcript, timeline, trace, PR, activity, or cleanup
work.

## Phases

Code and logs should use these phase names where practical:

1. `accepted`
2. `queued`
3. `runtime_starting`
4. `runtime_running`
5. `runtime_terminal_observed`
6. `terminal_recording`
7. `terminal_recorded`
8. `delivery_enqueued`
9. `side_effects_pending`
10. `side_effects_complete`

Legal forward transitions are encoded in
`src/codex_autorunner/core/orchestration/managed_turn_lifecycle_contract.py`.
The important boundary is `terminal_recorded`: it is the first durable terminal
orchestration state and the only phase that unblocks the next queued turn on the
same managed thread.

## Terminal Recording

Terminal recording is the first mandatory action after runtime completion,
timeout, interrupt, or lost-backend recovery observes a terminal outcome
candidate. The durable terminal outcome is exactly one of:

- `ok`
- `error`
- `interrupted`

Recording must be idempotent. Replaying the same terminal outcome is a
duplicate and should be treated as already complete. Replaying a different
terminal outcome is a conflict: keep the first durable outcome, do not rewrite
the turn, and emit observable diagnostics so recovery can be audited.

The current store uses `orch_thread_executions.status` as the durable terminal
status (`ok`, `error`/`failed`, or `interrupted`) and `finished_at` as the
recording marker. Later implementation tickets should add explicit lifecycle
phase evidence without changing the core rule: the queue may advance only after
the terminal outcome has been durably recorded.

## Side Effects

The following work is optional post-terminal side effect work. It may retry,
fail, or be recovered independently, but it must not keep an execution in
`running` after terminal recording succeeds:

- live timeline persistence
- final timeline persistence
- transcript writes
- cold trace persistence
- Discord, Telegram, or web-visible delivery
- PR binding
- activity updates
- archive cleanup

Side-effect workers may read the durable terminal outcome and enqueue their own
work records after `terminal_recorded`. They must not be required for
`terminal_recorded`, and they must not decide whether the next managed-thread
turn can start.

## Minimal Later Model Changes

Later implementation tickets should add only the durable fields needed to make
the contract observable and recoverable:

- A lifecycle phase or equivalent terminal-record marker on
  `orch_thread_executions`.
- An idempotency key for the terminal outcome write, keyed by managed turn.
- Structured conflict evidence for duplicate terminal writes that disagree.
- Durable side-effect intent rows for delivery, timeline/cold trace,
  transcript, PR binding, activity updates, and cleanup.
- Recovery timestamps or attempt counters so stale `runtime_running`,
  `runtime_terminal_observed`, and side-effect phases can be classified from
  SQLite state alone.

Until those fields exist, code that records outcomes should treat
`orch_thread_executions.status != 'running'` plus `finished_at` as the existing
terminal-recorded compatibility projection.
