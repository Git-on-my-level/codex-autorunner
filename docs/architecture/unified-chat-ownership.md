# Unified Chat Ownership

CAR has one managed-thread chat model for PMA web, Discord, and Telegram. The
backend owns canonical ordering, turn lifecycle, progress projection, surface
bindings, and final-delivery state. Surface code may render or cache projections,
but it must reconcile by backend IDs and ordering keys.

## Canonical Timeline

`src/codex_autorunner/core/orchestration/managed_thread_timeline.py` builds the
canonical timeline contract. Timeline items have stable `item_id` and
`order_key` fields and are typed as user messages, assistant messages,
intermediate output, tool groups, statuses, approvals, artifacts, and delivery
state.

The timeline is assembled from durable managed-thread turns, persisted execution
events, turn metadata, artifacts, and the delivery ledger. Queued user messages
appear once before execution starts, keep their stable turn item IDs while
waiting, and remain in order when promoted into active work. Intermediate output
stays separate from final assistant output.

PMA exposes this contract through
`/hub/pma/threads/{managed_thread_id}/timeline`. Browser code should map these
items into UI cards and may add temporary optimistic user items after a send, but
must replace them by stable backend IDs when the timeline refreshes.

## Surface Adapters

`src/codex_autorunner/adapters/chat/` owns shared surface contracts,
managed-thread coordinator setup, progress helpers, shared command semantics,
and adapter-neutral delivery hooks.

Discord and Telegram adapters own transport details only: platform ingress,
platform IDs, message formatting, edits, deletes, callbacks, attachment
transfer, rate limits, and API errors. They can render progress and final
results, but they must use shared coordinator and delivery hooks for lifecycle
and terminal-delivery policy.

PMA web routes under `src/codex_autorunner/surfaces/web/routes/pma_routes/`
expose orchestration state to the browser. They should not derive a second
transcript order from `/turns`, `/tail`, local storage, or streamed events.
Streaming and polling are invalidation paths; the timeline remains the rendered
source of truth.

## Bindings

Managed-thread targets live in the orchestration store. Surface bindings resolve
a PMA chat, Discord channel/thread, or Telegram chat/topic to the durable
managed-thread target. Runtime backend session bindings are separate from surface
bindings and are used only to resume or reset the underlying agent session.

Binding ownership:

- `core/orchestration/` owns durable thread targets, executions, queue state,
  runtime bindings, and surface binding records.
- `adapters/chat/` owns shared resolution and coordinator-facing metadata.
- Surface adapters own platform identifiers and API calls after the shared
  target has been resolved.

## Current Inventory

This is the contract inventory for the unified chat surface migration. It names
current state owners before implementation work starts so later tickets can move
read paths without guessing.

| Area | Current state owners | Target role |
| --- | --- | --- |
| Orchestration | `orch_thread_targets`, `orch_thread_executions`, `orch_queue_items`, `orch_bindings`, `orch_chat_operations`, `orch_managed_thread_deliveries`, `orch_notification_conversations` | Shared authority for normalized identity, binding, turn lifecycle, final delivery, and notification reply continuations. |
| PMA managed threads | `ManagedThreadStore`, managed-thread compatibility DB path, `/hub/pma/*` routes | Compatibility projection over the shared model. PMA route shapes stay stable while their data source converges. |
| Discord adapter | `discord_state.sqlite3` tables `channel_bindings`, `outbox`, `turn_progress_leases`, `interaction_ledger` | Transport-local preferences, API retry state, progress message leases, and interaction ack/replay cursors. Not lifecycle authority. |
| Telegram adapter | `telegram_state.sqlite3` tables `telegram_topics`, `telegram_topic_scopes`, `telegram_outbox`, `telegram_pending_approvals`, `telegram_pending_voice` | Transport-local topic metadata, current topic scope, API retry state, and pending UI state. Not lifecycle authority. |
| Web Hub | PMA chat view-model polling, file-chat draft state, PMA SSE compatibility stream | Projection consumer. The Hub should render normalized snapshots and subscribe to generic chat events. |
| Notification replies | `orch_notification_conversations`, `PmaNotificationStore` | Replyable chat surface identity keyed by notification id until a continuation thread target is bound. |
| Channel directory | `.codex-autorunner/chat/channel_directory.json`, `ChannelDirectoryStore` | Discovery projection with display and `seen_at` metadata only. |

## Normalized Lifecycle

The smallest shared lifecycle vocabulary for chat-like surfaces is:

- `discovered`: a conversation is known from channel discovery or notification
  delivery, but is not yet bound to a durable thread target.
- `bound`: the surface identity resolves to a durable thread target or
  continuation target.
- `queued`: a user turn is accepted and waiting for managed-thread execution.
- `running`: a managed turn is executing and may emit progress.
- `idle`: the latest managed turn completed successfully and the surface can
  accept another turn.
- `failed`: the latest managed turn or surface operation failed and needs user
  or recovery attention.
- `archived`: the surface remains inspectable but does not accept new turns
  without explicit unarchive or rebinding.

Contract fixtures live in
`tests/fixtures/chat_surface/lifecycle_contract.json` and are validated by
`tests/contracts/chat_surface/test_lifecycle_fixture_contract.py`. They cover
`/new`, bind, rebind, archive, queued, running, done, failed, delivery retry,
channel discovery, Discord, Telegram, Web-originated, PMA managed-thread, and
notification continuation scenarios.

Managed-thread turn finalization has a stricter orchestration contract in
`docs/architecture/managed-turn-lifecycle-contract.md`: only durable
`terminal_recorded` state unblocks the next queued turn, while delivery,
timeline, transcript, trace, PR binding, activity, and cleanup work are
post-terminal side effects.

## Delivery Ledger

Final delivery is durable orchestration state, not adapter-local state.
`src/codex_autorunner/core/orchestration/managed_thread_delivery_ledger.py`
records delivery intents, claims, attempts, retries, duplicate suppression, and
terminal delivery outcomes.

Adapters may execute delivery records for their platform and report outcomes
back through the shared delivery engine. They must not create a hidden retry
ledger or decide terminal delivery lifecycle independently. The canonical
timeline projects delivery ledger records as `delivery_state` items so PMA,
Discord, and Telegram can be inspected against the same backend truth.

## Change Checklist

- Add shared lifecycle, ordering, progress, or delivery behavior under
  `adapters/chat/` or `core/orchestration/`.
- Keep PMA, Discord, and Telegram adapters limited to projection, rendering,
  transport, and platform constraints.
- Preserve stable timeline item IDs and ordering keys when changing payloads.
- Cover queued sends, running progress, final assistant output, delivery state,
  and cross-surface projection in backend/frontend tests.
