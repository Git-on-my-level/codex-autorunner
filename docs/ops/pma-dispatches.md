# PMA Dispatches

PMA dispatches are durable, user-facing notices produced by the Project Management Agent.
They are filesystem-backed and surfaced in the web UI and (when active) Telegram.

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
- If PMA is actively using Telegram, new dispatches created during that PMA turn
  are sent to the Telegram outbox.

## Resolving

- Use the web UI to resolve (preferred), or
- set `resolved_at` to an ISO 8601 timestamp.

## Web API Delivery Semantics

For `POST /hub/pma/chat`, the top-level response `status` reflects turn execution
(`ok|error|interrupted`). Delivery fanout health is reported separately in
`delivery_status` so clients do not need to parse nested outcome objects.

`delivery_status` values:

- `success`: all attempted deliveries succeeded.
- `partial_success`: some deliveries succeeded and at least one failed.
- `failed`: all attempted deliveries failed.
- `duplicate_only`: no deliveries were sent because all targets were deduped for the turn.
- `skipped`: delivery was not attempted for a non-error reason (for example no targets/content).

Backward compatibility:

- `delivery_outcome` and `dispatch_delivery_outcome` remain unchanged and still carry
  detailed target-level fields (`errors`, counts, target keys).
