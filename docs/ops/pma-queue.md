# PMA Queue Persistence

The PMA queue writes lane state to JSONL files under `.codex-autorunner/pma/queue/`.
Pending items are treated as durable: when a lane worker starts, it replays
pending items from the JSONL file into the in-memory queue and processes them.

Cancelled/completed/failed items remain in the JSONL file as an audit trail.
