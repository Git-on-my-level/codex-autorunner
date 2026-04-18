# PMA Queue Persistence

## Canonical store

The PMA queue is backed by the ``orch_queue_items`` table in the orchestration
SQLite database.  SQLite is the single source of truth for all lane state:
enqueues, status transitions (pending -> running -> completed/failed/cancelled/
deduped), idempotency checks, and compaction all read from and write to
``orch_queue_items``.

## source_kind separation

``orch_queue_items`` is shared between two owners distinguished by
``source_kind``:

- ``source_kind='pma_lane'`` — generic PMA lane items managed by
  ``PmaQueue``.  All reads, mutations, compaction, and JSONL mirror writes
  from ``PmaQueue`` filter to this ``source_kind`` exclusively.
- ``source_kind='thread_execution'`` — queued managed-thread turn items
  managed by ``PmaThreadStoreLifecycle`` via helpers in
  ``pma_thread_store_rows.py``.  Claim, cancel, promote, and complete
  operations on these rows are owned by the thread-store lifecycle layer.

``PmaQueue`` must not read, modify, or compact ``thread_execution`` rows.
``PmaThreadStoreLifecycle`` must not modify ``pma_lane`` rows.  Lane-id
conventions (``pma:*`` vs ``thread:*``) prevent overlap in practice, but the
``source_kind`` column is the authoritative filter.

## Cross-process and in-process coordination

When a lane worker starts, it replays pending ``pma_lane`` items from SQLite
into the in-memory ``asyncio.Queue`` and processes them.  Cross-process enqueue
(``enqueue_sync``) notifies the running worker via the event loop, and the
worker refreshes pending rows from SQLite on the next poll cycle.

Dequeue transitions items from ``pending`` to ``running`` in SQLite and then
regenerates the JSONL mirror.  The lane worker's executor callback is
responsible for item-level processing; the queue only manages state
transitions.

## Compatibility mirrors

After every canonical mutation, ``PmaQueue`` rewrites a JSONL lane file under
``.codex-autorunner/pma/queue/`` as a **compatibility and audit artifact**.
These JSONL files are *not* the source of truth.  Deleting them does not affect
queue behaviour — ``replay_pending`` and ``_refresh_lane_from_disk`` both read
from SQLite, and the mirror is regenerated on the next write.

Mirror files exist so that:

- ad-hoc tooling can inspect lane state without opening SQLite, and
- audit history remains visible in the filesystem.

Mirror content is scoped to ``source_kind='pma_lane'`` rows only.
``thread_execution`` queue rows are not included in JSONL mirrors.

## Compaction

Compaction deletes old terminal rows from ``orch_queue_items`` and then
regenerates the JSONL mirror.  Non-terminal items (pending, running) are never
removed.

The compaction trigger counts terminal ``pma_lane`` rows in SQLite.  When the
count exceeds ``COMPACTION_MIN_TERMINAL_ROWS``, compaction keeps only the last
``DEFAULT_COMPACTION_KEEP_LAST`` terminal items and deletes the rest.

Compaction does not depend on the JSONL mirror file size or any other mirror
artifact.
