# PMA Queue Persistence

## Canonical store

The PMA queue is backed by the ``orch_queue_items`` table in the orchestration
SQLite database (``.codex-autorunner/state.sqlite3``).  SQLite is the single
source of truth for all lane state: enqueues, status transitions (pending →
running → completed/failed/cancelled/deduped), idempotency checks, and
compaction all read from and write to ``orch_queue_items``.

When a lane worker starts, it replays pending items from SQLite into the
in-memory ``asyncio.Queue`` and processes them.  Cross-process enqueue
(``enqueue_sync``) notifies the running worker via the event loop, and the
worker refreshes pending rows from SQLite on the next poll cycle.

## Compatibility mirrors

After every canonical mutation, ``PmaQueue`` rewrites a JSONL lane file under
``.codex-autorunner/pma/queue/`` as a **compatibility and audit artifact**.
These JSONL files are *not* the source of truth.  Deleting them does not affect
queue behaviour — ``replay_pending`` and ``_refresh_lane_from_disk`` both read
from SQLite, and the mirror is regenerated on the next write.

Mirror files exist so that:

- ad-hoc tooling can inspect lane state without opening SQLite, and
- audit history remains visible in the filesystem.

## Compaction

Compaction deletes old terminal rows from ``orch_queue_items`` and then
regenerates the JSONL mirror.  Non-terminal items (pending, running) are never
removed.  The compaction threshold is based on the JSONL mirror file size as a
heuristic.
