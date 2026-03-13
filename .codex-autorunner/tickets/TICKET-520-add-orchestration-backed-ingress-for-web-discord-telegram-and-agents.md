---
title: "Add orchestration-backed ingress for web, Discord, Telegram, and agent delegation"
agent: "codex"
done: true
goal: "Make every surface that starts or continues thread/flow work submit through one orchestration ingress instead of surface-local execution paths."
---

## Why this ticket exists

`TICKET-160` locked the ingress model in `.codex-autorunner/contextspace/surface-routing-bindings-and-pma-generalization.md`. CAR currently accepts work through PMA web routes, Discord service logic, Telegram handlers, and agent-side helpers that each perform their own routing. This ticket creates the shared ingress path those surfaces must call.

Run order note:
- Run this ticket after `TICKET-220`, `TICKET-240`, `TICKET-320`, `TICKET-420`, and `TICKET-440`.
- Treat this as the foundation for later binding, PMA-shim, and UX query work.

## Expected files/modules to touch

- `src/codex_autorunner/core/orchestration/service.py`
- `src/codex_autorunner/core/orchestration/threads.py`
- `src/codex_autorunner/core/orchestration/flows.py`
- `src/codex_autorunner/core/orchestration/events.py`
- `src/codex_autorunner/surfaces/web/routes/pma_routes/chat_runtime.py`
- `src/codex_autorunner/integrations/discord/service.py`
- `src/codex_autorunner/integrations/telegram/service.py`
- `src/codex_autorunner/integrations/telegram/handlers/messages.py`
- `tests/integrations/discord/test_service_routing.py`
- `tests/test_telegram_pma_routing.py`
- `tests/browser/test_orchestration.py`

## Tasks

- Add the orchestration ingress methods that surface code can use for:
  - send/continue thread messages
  - interrupt control actions
  - target resolution between runtime-backed threads and flow targets
  - normalized execution/event emission
- Refactor web PMA chat runtime, Discord service routing, Telegram message routing, and agent-delegation entry points to submit through the shared orchestration path.
- Preserve flow-target distinctness so no ingress path rewrites `ticket_flow` into a conversational thread target.
- Keep downstream runtimes authoritative for conversation state and flow engine state. This ingress layer should own routing and scheduling, not absorb runtime/flow internals.
- Preserve specialized surface UX while removing direct surface-to-harness execution as the long-term architecture.

## Acceptance criteria

- Web, Discord, Telegram, and agent-driven thread/flow work all submit through one orchestration ingress path.
- Surface code no longer owns the primary decision about whether a request targets a runtime thread or a flow.
- The ingress layer emits normalized orchestration-visible events for later status/tail surfaces.
- No path in this ticket makes `ticket_flow` behave like a normal chat thread.

## Tests

- Extend `tests/integrations/discord/test_service_routing.py` to show Discord-bound work resolves through orchestration instead of direct surface-local execution.
- Extend `tests/test_telegram_pma_routing.py` to show Telegram messages and PMA-mode traffic route through orchestration.
- Add or extend `tests/browser/test_orchestration.py` to cover orchestration-backed ingress from web routes.
- Run `.venv/bin/python -m pytest tests/integrations/discord/test_service_routing.py tests/test_telegram_pma_routing.py tests/browser/test_orchestration.py`.

## Progress

- Added a shared surface-ingress layer under `src/codex_autorunner/core/orchestration/` with `SurfaceOrchestrationIngress`, `SurfaceIngressResult`, normalized `OrchestrationEvent` records, and explicit flow/thread request types.
- Routed Discord conversational ingress through the shared orchestration seam in `integrations/discord/message_turns.py`, and moved Discord thread control actions (`resume`, `reset`, `interrupt`) onto the same ingress wrapper in `integrations/discord/service.py`.
- Routed Telegram text and media message ingress through the shared orchestration seam in `integrations/telegram/handlers/messages.py` so paused `ticket_flow` replies and runtime-thread turns are chosen in orchestration instead of surface-local branches.
- Routed PMA web queue execution through the shared orchestration ingress in `src/codex_autorunner/surfaces/web/routes/pma_routes/chat_runtime.py` while keeping PMA as a runtime-thread path and preserving downstream runtime ownership of conversation state.
- Added ingress-focused coverage in `tests/core/orchestration/test_service.py`, `tests/integrations/discord/test_message_turns.py`, `tests/integrations/discord/test_service_routing.py`, `tests/test_pma_routes.py`, `tests/test_telegram_pma_routing.py`, and `tests/browser/test_orchestration.py`.

## Outcome

- Web, Discord, and Telegram now submit conversational work through one orchestration-backed ingress abstraction rather than each surface owning the primary flow-vs-thread routing decision.
- `ticket_flow` remains a distinct flow target: paused flow replies are surfaced via `PausedFlowTarget` and `FlowTarget`, while runtime-backed work continues through thread submissions.
- The ingress emits normalized orchestration events (`ingress.received`, `ingress.target_resolved`, `ingress.thread_submitted`, `ingress.flow_resumed`, and thread control events) that later status/tail work can build on.
- Surface-specific UX stays intact: Discord keeps progress and attachment handling, Telegram keeps topic queueing and media handling, and PMA web keeps queue-backed execution, but those surfaces now call a shared routing seam.

## Validation

- `.venv/bin/python -m pytest tests/integrations/discord/test_service_routing.py tests/test_telegram_pma_routing.py tests/browser/test_orchestration.py`
- `.venv/bin/python -m pytest tests/core/orchestration/test_service.py tests/integrations/discord/test_message_turns.py tests/test_pma_routes.py tests/integrations/discord/test_service_routing.py tests/test_telegram_pma_routing.py tests/browser/test_orchestration.py`
- `.venv/bin/python -m ruff check src/codex_autorunner/core/orchestration/events.py src/codex_autorunner/core/orchestration/flows.py src/codex_autorunner/core/orchestration/threads.py src/codex_autorunner/core/orchestration/service.py src/codex_autorunner/core/orchestration/__init__.py src/codex_autorunner/integrations/discord/message_turns.py src/codex_autorunner/integrations/discord/service.py src/codex_autorunner/integrations/telegram/handlers/messages.py src/codex_autorunner/surfaces/web/routes/pma_routes/chat_runtime.py tests/core/orchestration/test_service.py tests/integrations/discord/test_message_turns.py tests/integrations/discord/test_service_routing.py tests/test_pma_routes.py tests/test_telegram_pma_routing.py tests/browser/test_orchestration.py`
