# Web UI Read Model Contracts

This document defines the v1 backend/frontend contracts and ownership rules for
the responsive web UI projection architecture in
`.codex-autorunner/contextspace/spec.md`. The steady-state path is:

`filesystem + sqlite + runtime events -> projection services -> screen read models -> snapshot APIs + cursor streams -> normalized frontend store -> virtualized UI`

Legacy broad-fetch endpoints may remain for diagnostics and tests, but screen
routes must not use them as primary data sources.

## Migration Consumer Audit

Before the read-model migration, the Svelte UI built screen state from broad,
page-local fetches:

- `/chats/[[chatId]]` reads `/hub/pma/threads`, selected thread detail,
  `/timeline`, `/tail`, `/status`, `/queue`, `/hub/pma/files`, `/hub/pma/docs`,
  agent catalogs, and chat SSE streams.
- `/repos`, `/worktrees`, and their detail pages read `/hub/repos` twice through
  `listRepos()` and `listWorktrees()`, then combine that with all runs, all
  chats, all tickets, and sometimes contextspace.
- ticket detail pages read scoped or legacy ticket lists, broad worktree data,
  run lists, dispatch history, linked chat timeline, tail/status, and run SSE.
- fallback ticket loading can enumerate every registered repo/worktree and then
  fetch each mounted legacy ticket queue.

The main FastAPI sources for those legacy payloads were:

- `routes/pma_routes/managed_threads.py` for PMA threads, queue, and timeline;
- `routes/pma_routes/chat_events.py` and `routes/chat_events.py` for current SSE;
- `routes/hub_repo_routes/repo_listing.py` and related hub repo routes;
- `routes/flows.py` and `routes/flow_routes/status_history_routes.py` for runs,
  tickets, events, and dispatch history;
- `routes/hub_repo_routes/tickets.py` for hub-scoped ticket summaries.

## Contract Modules

Backend models live in:

`src/codex_autorunner/surfaces/web/read_model_contracts.py`

Frontend types live in:

`src/codex_autorunner/web_frontend/src/lib/api/readModelContracts.ts`

All payloads use `contractVersion: "web-read-models.v1"` and camelCase JSON
field names. Backend route handlers should serialize Pydantic models with
`dump_read_model_contract(...)`.

## Ownership Map

Core projection ownership:

- Durable hub event/projection storage lives in
  `src/codex_autorunner/core/hub_projection_store.py`.
- Projection service boundaries live in
  `src/codex_autorunner/core/hub_projection_service.py` and family-specific
  services such as `core/orchestration/chat_surface_read_model.py` and
  `core/hub_repo_projection.py`.
- Canonical state still comes from CAR files, orchestration SQLite tables,
  runtime journals, and durable event journals. Projection tables are
  rebuildable read models, not new sources of truth.

Web surface ownership:

- HTTP/SSE route assembly lives under
  `src/codex_autorunner/surfaces/web/routes/`.
- Chat index/detail routes live in `routes/chat_events.py` and PMA route
  helpers until they converge on the generic read-model route namespace.
- Repo/worktree/ticket read-model routes live in
  `routes/hub_repo_routes/read_models.py`.
- Route handlers own screen-shaped payload composition and repair responses;
  they must not push graph reconstruction into Svelte page components.

Frontend ownership:

- Typed contract mappers live in
  `src/codex_autorunner/web_frontend/src/lib/api/readModelContracts.ts`.
- Snapshot clients live in
  `src/codex_autorunner/web_frontend/src/lib/data/readModelClients.ts`.
- Cursor stream/reconnect handling lives in
  `src/codex_autorunner/web_frontend/src/lib/data/readModelStream.ts`.
- Normalized entities, idempotent patch application, optimistic mutations, and
  selectors live in
  `src/codex_autorunner/web_frontend/src/lib/data/readModelStore.ts` and
  `readModelViewModels.ts`.
- Page routes own interaction state, selected ids, viewport state, and command
  wiring only.

## Adding A Projection Or Event

1. Add or extend the backend contract in
   `surfaces/web/read_model_contracts.py` and the matching frontend mapper in
   `web_frontend/src/lib/api/readModelContracts.ts`.
2. Add the family-specific projection query/rebuild logic in `core/` near the
   canonical data it reads. Use `HubProjectionService` only as the shared
   durable event/projection boundary.
3. Add a screen-shaped snapshot route and, when the screen is live-updating, a
   cursor patch stream. Every snapshot returns a `cursor` and `repair` policy.
4. Add normalized store state, patch application, selectors, and optimistic
   reconciliation in `web_frontend/src/lib/data/`.
5. Add tests at the contract, route, store, and scale layers. For scale budgets,
   extend `tests/chat_surface_lab/web_responsiveness_budgets.py`.

## Shared Types

`ProjectionCursor` is the durable stream position. `sequence` is monotonic
within `source`; `value` is the opaque cursor clients persist and send back.

`ReadModelEventEnvelope` wraps every patch event with:

- `eventType`, for example `chat.index.patch` or `worktree.runtime.patch`;
- `cursor`, the event cursor after applying the patch;
- `entityKind` and `entityId`;
- `operation`: `upsert`, `patch`, `delete`, `reorder`, `invalidate`, or `reset`;
- optional `sourceRevision` for filesystem/sqlite/runtime source state.

`RepairPolicy` tells the client which snapshot route to request after reconnect,
cursor gaps, invalidation, or reset operations.

Cursor guarantees:

- Cursors are ordered within a `source`; clients must not compare sequence
  values across unrelated sources.
- Cursor values are opaque strings for clients. The numeric sequence is for
  ordering and diagnostics.
- A route that cannot prove replay continuity must emit
  `projection.cursor_gap` or return a repair response instead of silently
  skipping events.
- Event application is idempotent. Replaying the same event after reconnect
  must converge on the same normalized store state.

## Target Routes

Initial route shapes should be:

- `GET /hub/read-models/chats`
  - Query: `filter`, `q`, `limit`, `after`, `group`, `includeArchived`.
  - Response: `ChatIndexSnapshot`.
- `GET /hub/read-models/chats/{chatId}`
  - Query: `timelineLimit`, `before`, `after`.
  - Response: `ChatDetailSnapshot`.
- `GET /hub/read-models/repo-worktree/topology`
  - Query: `limit`, `after`, `includeArchived`.
  - Response: `RepoWorktreeTopologySnapshot`.
- `GET /hub/read-models/repo-worktree/runtime`
  - Query: `limit`, `after`, `entityKind`, `entityId`.
  - Response: `RepoWorktreeRuntimeSnapshot`.
- `GET /hub/read-models/tickets/{ticketId}`
  - Query: `dispatchLimit`, `dispatchAfter`.
  - Response: `TicketDetailSnapshot`.
- `GET /hub/read-models/events`
  - Query: `after`, `families`, `entityKind`, `entityId`.
  - SSE events: `ReadModelPatchEvent` payloads.

Snapshot routes may add screen-specific query parameters, but they must keep one
primary snapshot per screen and must return a repair cursor.

Current compatibility route names may differ while the surface converges on the
generic `/hub/read-models/*` namespace. For example, chat index/detail currently
also expose `/hub/chat/index`, `/hub/chat/threads/{threadId}/detail`, and
`/hub/chat/patches`. Treat the contract shape and cursor semantics as the stable
surface; route aliases are migration details.

## Pagination Semantics

Every unbounded list is represented by `PageWindow`:

- `limit` is the applied server-side item limit.
- `nextCursor` and `previousCursor` are opaque page cursors.
- `totalEstimate` is optional and may be approximate.
- `totalIsExact` tells the UI whether counts are definitive.

Clients must not infer durable ordering from timestamps. Ordering comes from the
snapshot window and subsequent `reorder` or `reset` events.

## Replay And Idempotency

Patch events are safe to replay:

- Apply only if the event cursor is newer than the last applied cursor for that
  stream source.
- `upsert` replaces or creates the entity by id.
- `patch` updates listed fields only.
- `delete` removes listed ids.
- `reorder` replaces the visible window order using the supplied `order`.
- `invalidate` marks the entity stale and schedules a repair snapshot.
- `reset` discards the affected screen window and requires repair.

If the stream reports `projection.cursor_gap`, the client must request the
snapshot route listed in `RepairPolicy` with the last applied cursor in the
`after` query parameter. The backend may return a normal snapshot or a 409-style
repair response that instructs the client to drop local window state and reload.

Invalidation rules:

- `patch` is for field-level changes whose prior entity/window is still valid.
- `reorder` is for replacing visible ordering without reloading entity bodies.
- `invalidate` marks an entity or window stale and schedules the listed repair
  snapshot.
- `reset` means local state for that stream/window is no longer trustworthy and
  the client must replace it from a snapshot.
- Projection invalidation must be documented in the route test or architecture
  doc that introduces it. Undocumented invalidation is considered a migration
  regression.

## Read Model Families

Chat index:

- Snapshot: `ChatIndexSnapshot`.
- Patch: `ChatIndexPatchEvent`.
- Carries rows, group headers, counters, active filter/query, and cursor.

Chat detail:

- Snapshot: `ChatDetailSnapshot`.
- Patch: `ChatDetailPatchEvent`.
- Carries thread metadata, visible timeline window, queue summary, artifacts,
  and optimistic reconciliation ids (`clientMessageId`, `backendMessageId`).

Repo/worktree:

- Snapshot: `RepoWorktreeTopologySnapshot` for identity and relationships.
- Snapshot: `RepoWorktreeRuntimeSnapshot` for fast-changing status.
- Patch: `RepoWorktreePatchEvent`.
- Topology changes must not invalidate runtime windows, and runtime changes must
  not force topology reloads.

Ticket detail:

- Snapshot: `TicketDetailSnapshot`.
- Patch: `TicketDetailPatchEvent`.
- Carries selected ticket, sibling queue context, linked run, linked chats,
  artifacts, dispatch window, and cursor.

## Rebuild And Repair Behavior

- Projection stores must validate schema/version on startup and either reuse
  current state or reset/rebuild deterministic projections from canonical CAR
  state.
- `HubProjectionService.rebuild_all_projections()` rebuilds the shared entity
  projection from the durable hub UI event journal. Family-specific rebuilds
  must offer an equivalent deterministic path when they cache derived state.
- Corrupt or stale projection state should fail closed into a rebuild or a
  repair-required response. It must not make screen routes fall back to broad
  page-local collection fetches.
- Repair snapshots are normal screen snapshots with enough cursor/revision data
  to resume streaming. A repair may drop a window and reload it; it must not ask
  the frontend to reconstruct hidden graph state from unrelated endpoints.

## Final Migration Guarantees

Forbidden steady-state patterns:

- Page-local broad fetch choreography, including global chat/repo/worktree/run
  lists as primary screen inputs where a scoped read model exists.
- Normal polling loops for screen updates. Use snapshot once, cursor stream for
  changes, and repair snapshot after gaps or manual refresh.
- Unbounded DOM lists for high-cardinality chats, timelines, repos, worktrees,
  tickets, artifacts, or dispatch output.
- Frontend transcript composition from `/turns`, `/tail`, local storage, or
  streamed fragments. Backend-owned timeline projections are canonical.
- Undocumented projection invalidation or compatibility fallbacks.

Temporary compatibility behavior must be labeled in code comments or durable
docs with its non-UI purpose and removal condition.

## Verification Map

The completed ticket sequence covers the migration contract:

- `TICKET-001`: versioned backend/frontend contracts and cursor semantics.
- `TICKET-002`: durable projection store, event journal, replay, and rebuild
  service boundary.
- `TICKET-003`: chat index/detail snapshots and chat patch streams.
- `TICKET-004`: repo/worktree/ticket screen read models.
- `TICKET-005`: normalized frontend entity store, selectors, and optimistic
  reconciliation.
- `TICKET-006`: virtualization for high-cardinality surfaces.
- `TICKET-007`: cursor streams and repair snapshots replacing normal polling.
- `TICKET-008`: responsiveness budgets, diagnostics, and large-hub smoke tests.
- `TICKET-009`: removal or diagnostics-only labeling of legacy broad fetch
  paths.
