# Web UI Read Model Contracts

This document defines the target v1 backend/frontend contracts for the
responsive web UI projection architecture in `.codex-autorunner/contextspace/spec.md`.
It is a contract document only; legacy route migration is intentionally out of
scope for `TICKET-001`.

## Current Consumer Audit

The Svelte UI currently builds screen state from broad, page-local fetches:

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

The main FastAPI sources for these payloads are:

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
