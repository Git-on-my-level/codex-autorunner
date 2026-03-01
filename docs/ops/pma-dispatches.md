# PMA Dispatches

PMA dispatches are durable, user-facing notices produced by the Project Management Agent.
They are filesystem-backed and surfaced in the web UI, with optional chat delivery when PMA has an active channel context.

## Storage

```
<hub_root>/.codex-autorunner/pma/dispatches/<timestamp>_<id>.md
```

## File format

Dispatch files are Markdown with YAML frontmatter.

```yaml
---
title: "Review docs updates"
priority: action
created_at: "2026-02-05T22:17:00Z"
source_turn_id: "pma-<thread>-<turn>"
links:
  - label: "Decisions"
    href: "/repos/<repo_id>/?tab=workspace&doc=decisions"
resolved_at: null
---

Please confirm the decisions update before continuing.
```

## Delivery rules

- On creation, dispatches are listed in the web PMA Dispatches panel.
- If PMA is actively using a chat channel, new dispatches created during that PMA turn
  are queued to that channel outbox.

## Resolving

- Use the web UI to resolve (preferred), or
- set `resolved_at` to an ISO 8601 timestamp.

## Web API Delivery Semantics

For `POST /hub/pma/chat`, the top-level response `status` reflects turn execution
(`ok|error|interrupted`). Delivery health is reported separately in
`delivery_status` so clients do not need to parse nested outcome objects.

`delivery_status` values:

- `success`: all attempted deliveries succeeded.
- `partial_success`: at least one delivery succeeded and at least one failed.
- `failed`: all attempted deliveries failed.
- `duplicate_only`: no deliveries were sent because the same dispatch/output was already delivered for this turn.
- `skipped`: delivery was not attempted for a non-error reason (for example no channel context/content).

Backward compatibility:

- `delivery_outcome` and `dispatch_delivery_outcome` remain unchanged and still carry
  detailed fields (`errors`, counts, metadata).
