---
title: "Scope orchestration ingress, bindings, and PMA generalization across CAR surfaces"
agent: "codex"
done: true
goal: "Define the downstream ticket set that routes all CAR interaction through orchestration, generalizes PMA into an orchestrator client, and preserves Discord/Telegram/web/CLI usability."
---

## Progress

- [x] Read the universal-ingress, binding, PMA, and active-thread-query sections of `.codex-autorunner/contextspace/spec.md`.
- [x] Reviewed `src/codex_autorunner/core/chat_bindings.py`, `src/codex_autorunner/surfaces/cli/pma_cli.py`, `src/codex_autorunner/surfaces/web/routes/pma_routes/*`, and the current Discord/Telegram service and state seams that still route or persist thread bindings directly.
- [x] Confirmed that `src/codex_autorunner/core/chat_bindings.py` is currently a derived read layer over PMA/Discord/Telegram state, not an authoritative binding service.
- [x] Confirmed that PMA web routes still branch directly to Codex/OpenCode runtime logic, Discord keeps its own surface-local conversation orchestrator, and Telegram still stores topic-bound `active_thread_id` state in transport-owned records.
- [x] Wrote `.codex-autorunner/contextspace/surface-routing-bindings-and-pma-generalization.md` to lock the universal ingress path, authoritative binding model, PMA generalization boundary, and active-thread query expectations.
- [x] Reserved the `520-600` downstream range for ingress, binding, PMA-generalization, and status/query surface work from this scope ticket.
- [x] Created downstream tickets:
  - `TICKET-520-add-orchestration-backed-ingress-for-web-discord-telegram-and-agents.md`
  - `TICKET-540-add-authoritative-binding-storage-and-cross-surface-query-support.md`
  - `TICKET-560-generalize-pma-web-and-cli-onto-orchestration-services.md`
  - `TICKET-580-add-active-thread-and-binding-query-surfaces.md`
  - `TICKET-600-update-transcript-preview-tail-and-status-ux-for-orchestration.md`
- [x] Kept PMA UX as a compatibility surface while removing PMA-only routing as the intended long-term architecture.

## Tasks
- Read `spec.md` sections on universal ingress, bindings, PMA, and active-thread queries.
- Review the current surface and binding code:
  - `src/codex_autorunner/core/chat_bindings.py`
  - `src/codex_autorunner/surfaces/cli/pma_cli.py`
  - `src/codex_autorunner/surfaces/web/routes/pma_routes/*`
  - Discord / Telegram CLI and service entrypoints
  - any PMA context docs that currently direct callers to managed-thread special cases
- Produce a routing memo that states:
  - how inbound surface events become orchestration actions
  - how bindings are represented and stored
  - how PMA special commands remain available without PMA conversation bypasses
  - how agents/humans query active threads for an agent or binding
- Write downstream implementation tickets for:
  - orchestration-backed surface ingress
  - binding storage/query support
  - PMA route/CLI shims onto orchestration services
  - thread lookup / active-work query commands
  - transcript preview / tail / status UX updates
- Assign complex routing/generalization work to `codex` and straightforward CLI/web updates and verification to `opencode` where appropriate.

## Acceptance criteria
- A routing memo exists that covers CLI, web UI/API, Discord, Telegram, PMA, and agent-to-agent invocation.
- Downstream tickets exist for each surface path that currently bypasses a generic orchestration service.
- Generated tickets preserve PMA UX while removing PMA-only thread logic as the long-term architectural center.
- Generated tickets include active-thread and binding query support so agents can coordinate with other agents.

## Tests
- Verify the generated tickets touch all major ingress paths.
- Verify the generated tickets maintain one-thread-per-binding semantics for chat bindings.
- Verify no generated ticket leaves direct thread execution calls in surfaces where orchestration should now own routing.

## Outcome

The routing boundary locked by this ticket is:
- every work-causing surface event becomes an orchestration action first
- bindings are authoritative orchestration records, not transport-owned thread authorities
- PMA remains a client and addressable agent, not a routing bypass
- active-thread and binding queries come from orchestration-visible state

The major current extraction targets identified from code are:
- `src/codex_autorunner/core/chat_bindings.py`
- `src/codex_autorunner/surfaces/web/routes/pma_routes/managed_thread_runtime.py`
- `src/codex_autorunner/surfaces/web/routes/pma_routes/chat_runtime.py`
- `src/codex_autorunner/surfaces/cli/pma_cli.py`
- `src/codex_autorunner/integrations/discord/service.py`
- `src/codex_autorunner/integrations/telegram/service.py`
- `src/codex_autorunner/integrations/telegram/handlers/commands/execution.py`
