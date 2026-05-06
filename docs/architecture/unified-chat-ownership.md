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

`src/codex_autorunner/integrations/chat/` owns shared surface contracts,
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
- `integrations/chat/` owns shared resolution and coordinator-facing metadata.
- Surface adapters own platform identifiers and API calls after the shared
  target has been resolved.

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
  `integrations/chat/` or `core/orchestration/`.
- Keep PMA, Discord, and Telegram adapters limited to projection, rendering,
  transport, and platform constraints.
- Preserve stable timeline item IDs and ordering keys when changing payloads.
- Cover queued sends, running progress, final assistant output, delivery state,
  and cross-surface projection in backend/frontend tests.
