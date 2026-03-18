# PMA Dispatches

PMA dispatches are durable, user-facing notices produced by the Project Management Agent.
They are filesystem-backed and surfaced in the web UI.

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
- When PMA auto-resolves a paused ticket-flow dispatch, CAR mirrors that PMA reply to the repo's preferred bound non-PMA chat surface so the bound user sees that no response is needed.
- When PMA escalates a paused ticket-flow dispatch to the user, CAR sends the escalation notice to one PMA-bound chat for that repo:
  - prefer the freshest matching PMA Discord binding
  - fall back to a matching PMA Telegram binding if no Discord PMA binding matches
- Ticket-flow completion still requires a clean commit after each finished ticket. If a ticket is marked done but the repo remains dirty or ownership is ambiguous, PMA should escalate instead of guessing a reply.

## Resolving

- Use the web UI to resolve (preferred), or
- set `resolved_at` to an ISO 8601 timestamp.
