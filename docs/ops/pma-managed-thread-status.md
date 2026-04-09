# PMA Managed-Thread Status Model

CAR persists one normalized runtime status and now exposes a separate
operator-facing headline for PMA managed threads.

## Status Enum

| Status | Meaning | Terminal |
| --- | --- | --- |
| `idle` | Healthy and waiting for work | no |
| `running` | A managed turn is currently executing | no |
| `paused` | Execution was intentionally compacted/paused | no |
| `completed` | The latest managed turn finished successfully | yes |
| `interrupted` | The latest managed turn was intentionally interrupted | yes |
| `failed` | The latest managed turn finished unsuccessfully | yes |
| `archived` | The thread is read-only and archived | yes |

## Transition Table

| Current | Signal | Next |
| --- | --- | --- |
| any | `thread_created` | `idle` |
| `paused`, `archived` | `thread_resumed` | `idle` |
| `idle`, `completed`, `failed`, `paused` | `turn_started` | `running` |
| `idle`, `completed`, `failed`, `paused` | `thread_compacted` | `paused` |
| `running` | `managed_turn_completed` | `completed` |
| `running` | `managed_turn_failed` | `failed` |
| `running` | `managed_turn_interrupted` | `interrupted` |
| any | `thread_archived` | `archived` |

## Persisted Context

Each managed thread persists:

- `normalized_status`
- `status_reason_code`
- `status_updated_at`
- `status_terminal`
- `status_turn_id`

API, CLI, and hub UI surfaces read these persisted fields instead of re-inferring
status from mixed lifecycle signals.

## Operator-Facing API Contract

Managed-thread API responses now expose:

- `operator_status`: recommended headline state for operator surfaces
- `is_reusable`: convenience boolean for "can this thread take more work now?"
- `active_turn_diagnostics`: compact in-flight provenance for the active/latest
  managed turn, including request metadata, backend ids, last observed event,
  and whether the turn currently appears stalled

The operator headline intentionally differs from the persisted runtime outcome:

| `normalized_status` | `lifecycle_status` | `operator_status` | `is_reusable` |
| --- | --- | --- | --- |
| `idle` | `active` | `idle` | `true` |
| `completed` | `active` | `reusable` | `true` |
| `interrupted` | `active` | `reusable` | `true` |
| `failed` | `active` | `attention_required` | `false` |
| `running` | `active` | `running` | `false` |
| `paused` | `active` | `paused` | `false` |
| `archived` | `archived` | `archived` | `false` |

Compatibility expectations:

- `normalized_status` remains the raw runtime state and should be used for audit,
  debugging, and low-level automation.
- `lifecycle_status` remains the write-admission state (`active` vs `archived`).
- `status` remains a compatibility alias for `normalized_status` on PMA thread API
  payloads while downstream surfaces migrate.
- `status_reason`, `status_changed_at`, `status_terminal`, and `status_turn_id`
  continue to expose the last raw transition details without translation.

PMA thread create/list/get/resume/archive and status/tail reads go through the
shared orchestration service seam. PMA keeps its operator-facing response
shape, but the web and CLI surfaces no longer treat direct PMA store access as
the primary runtime-thread query/control path.

## Resource Ownership And Bindings

Every PMA managed thread is a durable CAR thread target owned by a typed
resource:

- `resource_kind: repo` for repo-backed threads
- `resource_kind: agent_workspace` for first-class runtime workspaces

Chat-surface bindings resolve to that durable thread target. For agent
workspaces, this distinction matters: CAR binds Discord/Telegram/web chat to a
consistent durable CAR thread under the workspace, while the workspace root
continues to provide shared memory across multiple threads.

## Busy-Thread Delivery

Managed-thread delivery is queue-first by default.

- Sending a message to a busy thread creates a queued turn plus a durable
  orchestration queue record.
- Prefer `car pma thread send --message-file <path>` or `--message-stdin` for
  multiline or shell-sensitive prompts; the CLI now echoes the exact delivered
  message after acceptance.
- `busy_policy=interrupt` (or `car pma thread send --if-busy interrupt`) keeps
  interrupt as a first-class operation when the runtime supports it.
- `busy_policy=reject` preserves the old fail-fast behavior for callers that
  explicitly want it.
- `/hub/pma/threads/{id}/status` and `car pma thread status` expose both the
  active turn and any queued turns behind it.

## PMA Prompt Context And Deltas

PMA prompt assembly uses a delta-oriented context model to reduce token overhead
on repeated turns within the same managed thread.

### When PMA Sends Full Context

Full context (AGENTS.md, active_context.md, context_log.md tail, and hub snapshot)
is injected when:

- **First turn**: No prior prompt state exists for the session key.
- **Digest mismatch**: The persisted bundle digest does not match the current
  section digests (indicates corruption or external modification).
- **Explicit refresh**: `force_full_context=True` is passed to the prompt builder
  (e.g., operator requests a refresh or recovery action).

In these cases, the prompt includes `<pma_workspace_docs>` and `<hub_snapshot>`
blocks with full content.

### When PMA Sends Deltas

On subsequent turns, when:

1. A `prompt_state_key` is provided (typically the managed_thread_id or session ID)
2. A valid prior state exists in `.codex-autorunner/pma/prompt_state.json`
3. The bundle digest validates correctly

The prompt builder switches to delta mode:

- Stable prompt sections (`prompt.md`, discoverability pointers,
  `<pma_fastpath>`, durable docs, and unchanged hub snapshots) are omitted from
  the repeated turn payload and summarized compactly in
  `<what_changed_since_last_turn>`.
- Changed sections (e.g., `active_context.md`) are re-injected inline.
- The full hub snapshot is replaced with `<hub_snapshot_ref>` referencing the
  cached digest.
- The `<current_actionable_state>` block always includes the current action queue
  and any pending operator items.

### Active Context Compaction

`active_context.md` is expected to remain actionable and concise. When it exceeds
the configured line budget (`active_context_max_lines`, default 200), it is
automatically pruned:

- The full content is appended to `context_log.md` under a timestamped snapshot header.
- `active_context.md` is reset to the template content with an auto-prune marker.

Older reconciled notes, completed work, and historical context should migrate to
`context_log.md` rather than accumulating in `active_context.md`.

### Context Log

`context_log.md` is an append-only history of auto-pruned snapshots and manual
archival. PMA prompts include only the tail (default last 120 lines) to keep
token usage bounded while preserving recent context continuity.
