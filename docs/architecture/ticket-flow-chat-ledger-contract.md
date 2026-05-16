# Ticket-Flow Chat Ledger Contract

Contract version: `ticket_flow_chat_ledger.v1`

Ticket-flow sequencing may continue to use repo-local `flows.db`, but chat and
flow visibility are orchestration concerns. Every executed ticket-flow agent
turn must create or reuse a managed thread and persist a repairable
`flow_run_id + ticket_id -> managed_thread_id` link in orchestration state before
the turn is allowed to complete.

## Canonical Facts

Required lifecycle facts:

- `flow_run.started`
- `flow_run.completed`
- `flow_run.failed`
- `ticket.selected`
- `managed_thread.created`
- `managed_thread.reused`
- `ticket_thread.linked`
- `ticket_turn.started`
- `ticket_turn.completed`
- `ticket_turn.failed`

Repo-local `flows.db` remains responsible for native flow-engine execution
state: run status, current step, pause/resume state, terminal state, and
`state.ticket_engine`. This proves what the sequencing engine did, but it is not
the source of truth for Web Hub chat visibility.

Hub orchestration state must contain:

- Flow lifecycle events for run start, completion, and failure with `run_id`,
  `flow_type=ticket_flow`, `workspace_root`, and timestamps.
- Ticket selection events with `flow_run_id`, `ticket_id`, `ticket_path`,
  `workspace_root`, and timestamps.
- A managed thread target in `orch_thread_targets` for each executed ticket turn.
  Its metadata must include `flow_type=ticket_flow`, `thread_kind=ticket_flow`,
  `run_id`, `flow_run_id`, `ticket_id`, `workspace_root`, and
  `ticket_flow_link_key`.
- A durable thread-link fact with `flow_run_id`, `ticket_id`,
  `managed_thread_id`, and `workspace_root`.
- Managed turn execution state in `orch_thread_executions`, including
  `managed_thread_id`, turn status, start time, and terminal time.
- Chat-surface events and bindings derived from the managed thread, not from
  Discord or Telegram status notices.

## Rebuild Rules

The Web Hub chat index rebuilds from orchestration-owned managed thread targets,
thread executions, bindings, delivery records, channel-directory entries, and
`orch_chat_surface_events`. It must not read repo-local `flows.db` or
`.codex-autorunner/flows/<run_id>/chat/*.jsonl`.

A future flow index rebuilds from orchestration flow lifecycle, ticket
selection, thread-link, and turn lifecycle facts. `flows.db` can be used as
engine state and as repair/backfill input for older runs, but projected Hub
visibility must be recoverable from orchestration after repair.

Discord and Telegram flow status messages are delivery artifacts. During the
transition they may keep compatibility shims, but their content should be
rendered from the same canonical projections used by Web Hub.

## Code Anchor

The testable source for this contract is
`codex_autorunner.core.orchestration.ticket_flow_chat_ledger_contract`.
