# Hermes Distillation Spec

This document is the consolidated spec for CAR's Hermes-inspired runtime model across
chat delivery and execution destinations. It is the durable, implementation-facing
reference for:

- PMA delivery targets (intent + delivery bookkeeping)
- Channel directory (derived routing cache)
- Run chat mirroring artifacts (observability substrate)
- Repo/worktree execution destination behavior (local vs docker)

## Problem Statement

CAR supports multiple surfaces (web, Telegram, Discord, CLI). Without a single
control-plane contract, surfaces drift and delivery semantics become inconsistent.

This spec exists to keep parity and replayability cheap:

- Filesystem artifacts are canonical truth.
- Adapters/surfaces may vary, but they must project to the same on-disk contracts.
- Deterministic ids and durable mirrors make retries and debugging predictable.

## Canonical State Locations

All locations below are relative to a hub root or repo root and must remain under
`.codex-autorunner/` (see [STATE_ROOTS](../STATE_ROOTS.md)).

### 1) PMA Delivery Targets (canonical intent)

Path:

- `<hub_root>/.codex-autorunner/pma/delivery_targets.json`

Shape (v1, abbreviated):

```json
{
  "version": 1,
  "updated_at": "2026-02-26T10:00:00Z",
  "targets": [
    { "kind": "web" },
    { "kind": "local", "path": ".codex-autorunner/pma/deliveries.jsonl" },
    { "kind": "chat", "platform": "telegram", "chat_id": "123", "thread_id": "456" },
    { "kind": "chat", "platform": "discord", "chat_id": "987654321" }
  ],
  "last_delivery_by_target": {
    "chat:telegram:123:456": "turn_abc",
    "chat:discord:987654321": "turn_abc",
    "local:.codex-autorunner/pma/deliveries.jsonl": "turn_abc"
  }
}
```

Notes:

- `targets` is source-of-truth intent.
- `last_delivery_by_target` is durable per-target dedupe bookkeeping for PMA output
  delivery turns.
- Target identity uses normalized target keys:
  - `web`
  - `local:<path>`
  - `chat:telegram:<chat_id>[:<thread_id>]`
  - `chat:discord:<channel_id>`

### 2) Channel Directory (derived cache)

Path:

- `<hub_root>/.codex-autorunner/chat/channel_directory.json`

Shape (v1, abbreviated):

```json
{
  "version": 1,
  "updated_at": "2026-02-26T10:00:00Z",
  "entries": [
    {
      "platform": "telegram",
      "chat_id": "123",
      "thread_id": "456",
      "display": "My Group / Topic",
      "seen_at": "2026-02-26T09:55:00Z",
      "meta": { "chat_type": "supergroup" }
    },
    {
      "platform": "discord",
      "chat_id": "987654321",
      "display": "CAR HQ / #general",
      "seen_at": "2026-02-26T09:55:00Z",
      "meta": { "guild_id": "123456" }
    }
  ]
}
```

Notes:

- This file is derived from inbound traffic and may be rebuilt.
- Key shape is `platform:chat_id[:thread_id]` (where `thread_id` means real thread
  id, not platform-scoped metadata like discord guild id).

### 3) Chat Mirroring (run artifacts)

Paths:

- `<repo_root>/.codex-autorunner/flows/<run_id>/chat/inbound.jsonl`
- `<repo_root>/.codex-autorunner/flows/<run_id>/chat/outbound.jsonl`

Records are append-only JSON lines containing at least:

- `ts`, `direction`, `platform`, `chat_id`, `thread_id`, `message_id`
- `actor`, `kind`, `text`, `meta`
- Compatibility fields: `run_id`, `event_type`, `text_preview`, `text_bytes`

### 4) Destination Config and Inheritance

Path:

- `<hub_root>/.codex-autorunner/manifest.yml`

Contract:

- Repo/worktree entries may define `destination`.
- Effective destination resolution:
  1. Worktree destination, if present and valid
  2. Else base repo destination, if present and valid
  3. Else default `{kind: local}`

See [Destinations](../configuration/destinations.md) for CLI and troubleshooting.

## Contracts and Invariants

### A) Per-target idempotency and dedupe

- PMA output dedupe is per target key via `last_delivery_by_target[target_key] == turn_id`.
- Outbox ids are deterministic and target-scoped:
  - PMA output: `pma:{turn_id}:{target_key}:{chunk_index}`
  - PMA dispatch: `pma-dispatch:{dispatch_id}:{target_key}:{chunk_index}`
- Deterministic ids ensure retries/upserts do not collide across targets.

### B) Channel directory is derived, not authoritative intent

- Channel directory is a convenience cache for selection/routing UX.
- Canonical intent is delivery targets config, not channel directory entries.
- Stale/missing directory entries must never invalidate explicit target refs.

### C) Observability is contractual

- PMA delivery mirrors and flow chat mirrors are durable append-only artifacts.
- Run event stream remains canonical for lifecycle replay:
  - See [Run Events](../ops/run-events.md).

### D) Execution posture and state roots

- CAR remains YOLO-by-default for execution posture.
- Default destination is `local`.
- Docker destination is opt-in and first-class.
- Docker destination keeps supervisor workspace state under repo-local:
  - `<repo_root>/.codex-autorunner/app_server_workspaces`
- This remains canonical because it is inside `.codex-autorunner/`.

## Operational Workflows

### Manage PMA delivery targets

```bash
car pma targets list --path <hub_root>
car pma targets add discord:<channel_id> --path <hub_root>
car pma targets add telegram:<chat_id>:<thread_id> --path <hub_root>
car pma targets add local:.codex-autorunner/pma/deliveries.jsonl --path <hub_root>
car pma targets rm discord:<channel_id> --path <hub_root>
car pma targets clear --path <hub_root>
```

### Inspect channel directory

```bash
car chat channels list --path <hub_root>
car chat channels list --query discord --json --path <hub_root>
```

### Inspect mirrors

```bash
tail -n 50 <hub_root>/.codex-autorunner/pma/deliveries.jsonl
tail -n 50 <repo_root>/.codex-autorunner/flows/<run_id>/chat/inbound.jsonl
tail -n 50 <repo_root>/.codex-autorunner/flows/<run_id>/chat/outbound.jsonl
```

### Destination diagnostics

```bash
car hub destination show <repo_id> --path <hub_root>
car doctor --repo <hub_root>
```

## Follow-on Feature Guidance (e.g., remote destination)

Any new destination kind should preserve these properties:

- Effective destination remains explicit and inspectable from hub manifest state.
- Supervisor/workspace state remains in canonical roots only.
- Delivery target semantics remain platform-agnostic and target-keyed.
- Retry semantics use deterministic ids to avoid cross-target collisions.
- Mirrors/events stay durable so behavior is reconstructable from artifacts alone.

## Related Docs

- [Destinations](../configuration/destinations.md)
- [State Roots Contract](../STATE_ROOTS.md)
- [Canonical Run Events](../ops/run-events.md)
