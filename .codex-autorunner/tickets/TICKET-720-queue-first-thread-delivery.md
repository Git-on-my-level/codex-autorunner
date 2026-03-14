---
title: "Make thread-target delivery queue-first by default and preserve first-class interrupt"
agent: "codex"
done: true
goal: "Bring runtime-backed thread orchestration in line with the agreed contract: sending a message to a busy thread should enqueue work by default, while explicit interrupt remains available and observable across CLI, web, Discord, Telegram, PMA, and agent-to-agent calls."
---

## Why
The new orchestration layer is largely in place, but runtime-backed thread delivery still rejects on busy threads instead of queueing. `ThreadOrchestrationService.send_message()` immediately creates an execution, and the PMA runtime path still returns `409 already_in_flight` when a running turn exists. That breaks the agreed default busy-thread policy and makes agent-to-agent delegation brittle.

## Tasks
- Audit all thread-target submission entry points and route them through one queue-aware orchestration path:
  - `core/orchestration/service.py`
  - `surfaces/web/routes/pma_routes/managed_thread_runtime.py`
  - `surfaces/web/routes/pma_routes/managed_threads.py`
  - `surfaces/cli/pma_cli.py`
  - Discord / Telegram message ingress
  - PMA message submission
- Define and implement the canonical busy-thread policy for thread targets:
  - default = `queue`
  - explicit alternate action = `interrupt`
  - rejection only when explicitly requested or policy disallows enqueue
- Reuse the new orchestration SQLite state rather than reintroducing sidecar queue semantics.
- Ensure queued work is visible through orchestration queries so humans and agents can inspect:
  - active thread
  - running execution
  - pending queued messages/actions per thread
- Preserve first-class interrupt as an explicit operation that can preempt a running turn when the adapter supports it.
- Normalize route / CLI responses so a second message to a busy thread is reported as queued rather than rejected.
- Update or add docs for the default delivery policy and the difference between queue vs interrupt.

## Acceptance criteria
- Sending a second message to a thread with a running turn does **not** fail by default; it returns a queued/accepted state with a durable queue record.
- Explicit interrupt remains supported and observable from the orchestration layer.
- Discord, Telegram, PMA, CLI, and agent-to-agent message submission all follow the same queue-first behavior for thread targets.
- The orchestrator can answer whether an agent/thread has active work and whether additional work is queued behind it.
- No surface keeps a private bypass that still rejects on busy-thread by default.

## Tests
- Add service-level tests covering:
  - busy thread + default send => queued
  - busy thread + interrupt => running turn interrupted and new work started/queued per policy
  - visibility queries for running + queued work
- Add route/CLI tests proving the old `already_in_flight` rejection is replaced by queue-first behavior.
- Add at least one cross-surface test (Discord or Telegram) showing repeated messages to the same bound agent thread queue correctly.

## Progress
- Implemented queue-first busy-thread delivery in the shared orchestration thread service and PMA store:
  - `MessageRequest` now carries a first-class `busy_policy` (`queue`, `interrupt`, `reject`).
  - Busy thread sends now create queued executions plus durable orchestration queue records in SQLite.
  - Explicit interrupt remains available and starts the new turn immediately when requested.
- PMA managed-thread runtime and CLI now expose queue-aware behavior:
  - default second send returns `send_state=queued` instead of `already_in_flight`
  - explicit reject remains available via `busy_policy=reject` / `car pma thread send --if-busy reject`
  - status output now includes queue depth and queued turns
- Discord and Telegram bot services now expose harness-friendly app-server runtime context:
  - both services publish an `app_server_events` buffer suitable for managed-thread turn observation
  - both services expose app-server / OpenCode supervisor handles on service state so orchestration-backed chat ingress can reuse the existing harness path instead of rebuilding supervisor ownership from scratch
- Discord PMA plain-text message ingress now reuses the shared managed-thread orchestration path:
  - one orchestration-managed thread target is bound per PMA-enabled Discord channel via hub SQLite bindings
  - a second Discord PMA text message to the same busy channel now queues onto that managed thread instead of racing a parallel runtime turn
  - a Discord background queue worker drains queued managed-thread executions and sends their terminal replies back to the channel
- Telegram PMA plain-text message ingress now reuses the shared managed-thread orchestration path:
  - one orchestration-managed thread target is bound per PMA-enabled Telegram topic via hub SQLite bindings
  - a second Telegram PMA text message to the same busy topic now queues onto that managed thread instead of racing a parallel runtime turn
  - a Telegram background queue worker drains queued managed-thread executions and sends their terminal replies back to the topic
  - explicit Telegram PMA interrupt for managed-thread plain-text turns now interrupts the running orchestration execution and cancels queued managed-thread executions for that topic
- Managed-thread runtime execution now preserves native input items instead of dropping PMA attachment/media turns back to the legacy path:
  - `MessageRequest` plus the runtime-thread harness contract now carry optional `input_items`, and queued execution payloads round-trip them through orchestration SQLite
  - the Codex harness forwards those `input_items` to the app-server `turn/start` call, while non-app-server harnesses continue to ignore them without breaking the shared contract
  - Discord and Telegram PMA managed-thread turns now rebuild the first text input item with the PMA runtime prompt/discoverability wrapper and keep attachment-derived `localImage` items attached to the same queued execution
- Ticket-flow / agent-pool delegation now creates and reuses orchestration thread targets instead of treating backend session ids as the only durable handle:
  - `AgentTurnResult.conversation_id` now returns the orchestration thread target id, which the ticket runner reuses on subsequent delegated turns
  - repeated delegated sends to the same busy durable-thread agent now create queued orchestration execution records and drain in order on that thread target
  - queued delegated work is visible through the same `orch_thread_targets` / `orch_thread_executions` state used by PMA, CLI, web, Discord, and Telegram
- Added tests for:
  - store-level queued turn creation and promotion
  - service-level queue-first and interrupt behavior
  - PMA route queue visibility and background drain
  - CLI queued-send output
- Updated `docs/ops/pma-managed-thread-status.md` with queue-first vs interrupt behavior.

## Validation
- Re-ran the focused queue-first orchestration/PMA test slice after resolving the `black` formatting failure that blocked checkpointing:
  - `.venv/bin/pytest tests/core/orchestration/test_interfaces.py tests/test_pma_thread_store.py tests/core/orchestration/test_service.py tests/core/orchestration/test_bindings.py tests/test_pma_managed_threads_messages.py tests/test_pma_routes.py tests/test_pma_cli.py tests/surfaces/web/routes/pma_routes/test_managed_thread_runtime.py -q`
  - Result: `163 passed`
- Fixed the follow-up mypy checkpoint failure in `core/orchestration/service.py` by guarding `thread.workspace_root` before building `Path(...)`.
- Re-ran targeted validation for the typing fix:
  - `.venv/bin/mypy src/codex_autorunner/core/orchestration/service.py`
  - `.venv/bin/pytest tests/core/orchestration/test_service.py tests/surfaces/web/routes/pma_routes/test_managed_thread_runtime.py -q`
  - Result: `Success: no issues found in 1 source file`; `19 passed`
- Validated the new Discord/Telegram service context hooks:
  - `.venv/bin/pytest tests/integrations/discord/test_service_startup.py tests/test_telegram_service_context.py -q`
  - Result: `3 passed`
- Resolved the follow-up `ruff` checkpoint failure caused by import ordering in `integrations/telegram/service.py`.
- Re-ran narrow validation after the import-order fix:
  - `.venv/bin/ruff check src/codex_autorunner/integrations/telegram/service.py`
  - `.venv/bin/pytest tests/integrations/discord/test_service_startup.py tests/test_telegram_service_context.py -q`
  - Result: `All checks passed!`; `3 passed`
- Validated the Discord PMA plain-text managed-thread queue path without regressing the existing attachment-focused coverage:
  - `.venv/bin/ruff check --fix src/codex_autorunner/integrations/discord/service.py src/codex_autorunner/integrations/discord/message_turns.py tests/integrations/discord/test_message_turns.py`
  - `.venv/bin/black src/codex_autorunner/integrations/discord/service.py src/codex_autorunner/integrations/discord/message_turns.py tests/integrations/discord/test_message_turns.py`
  - `.venv/bin/pytest tests/integrations/discord/test_message_turns.py -k "managed_thread_queue or attachment_only_in_pma_mode_uses_hub_inbox_snapshot or image_attachment_in_pma_mode_adds_native_local_image_item or sends_queued_notice_when_dispatch_queue_is_busy" -q`
  - Result: `4 passed`
- Validated the Telegram PMA plain-text managed-thread queue path while keeping the existing PMA routing/recovery coverage green:
  - `.venv/bin/ruff check --fix src/codex_autorunner/integrations/telegram/handlers/commands/execution.py tests/test_telegram_pma_routing.py`
  - `.venv/bin/black src/codex_autorunner/integrations/telegram/handlers/commands/execution.py tests/test_telegram_pma_routing.py`
  - `.venv/bin/pytest tests/test_telegram_pma_routing.py -q`
  - `.venv/bin/ruff check src/codex_autorunner/integrations/telegram/handlers/commands/execution.py tests/test_telegram_pma_routing.py`
  - `.venv/bin/mypy src/codex_autorunner/integrations/telegram/handlers/commands/execution.py`
  - Result: `24 passed`; `All checks passed!`; `Success: no issues found in 1 source file`
- Re-ran the focused cross-surface queue slice after the Telegram migration to confirm Discord and Telegram still agree on queue-first behavior:
  - `.venv/bin/pytest tests/integrations/discord/test_message_turns.py -k "managed_thread_queue or attachment_only_in_pma_mode_uses_hub_inbox_snapshot or image_attachment_in_pma_mode_adds_native_local_image_item or sends_queued_notice_when_dispatch_queue_is_busy" -q`
  - `.venv/bin/pytest tests/test_telegram_pma_routing.py -k "managed_thread_queue or pma_missing_thread_resets_registry_and_recovers" -q`
  - Result: `4 passed`; `2 passed`
- Added orchestration/service coverage for cancelling queued managed-thread executions during interrupt, and Telegram PMA routing coverage for managed-thread interrupt:
  - `.venv/bin/ruff check src/codex_autorunner/core/orchestration/interfaces.py src/codex_autorunner/core/orchestration/service.py src/codex_autorunner/core/pma_thread_store.py src/codex_autorunner/integrations/telegram/handlers/commands_runtime.py tests/core/orchestration/test_service.py tests/test_telegram_pma_routing.py`
  - `.venv/bin/black src/codex_autorunner/core/orchestration/interfaces.py src/codex_autorunner/core/orchestration/service.py src/codex_autorunner/core/pma_thread_store.py src/codex_autorunner/integrations/telegram/handlers/commands_runtime.py tests/core/orchestration/test_service.py tests/test_telegram_pma_routing.py`
  - `.venv/bin/mypy src/codex_autorunner/core/orchestration/service.py src/codex_autorunner/integrations/telegram/handlers/commands_runtime.py src/codex_autorunner/integrations/telegram/handlers/commands/execution.py`
  - `.venv/bin/pytest tests/core/orchestration/test_service.py tests/test_telegram_pma_routing.py -q`
  - Result: `All checks passed!`; `Success: no issues found in 3 source files`; `42 passed`
- Validated the attachment/native-input-item managed-thread bridge across orchestration plus both chat surfaces:
  - `.venv/bin/black src/codex_autorunner/agents/base.py src/codex_autorunner/agents/codex/harness.py src/codex_autorunner/agents/opencode/harness.py src/codex_autorunner/agents/zeroclaw/harness.py src/codex_autorunner/core/orchestration/interfaces.py src/codex_autorunner/core/orchestration/models.py src/codex_autorunner/core/orchestration/service.py src/codex_autorunner/integrations/discord/message_turns.py src/codex_autorunner/integrations/discord/service.py src/codex_autorunner/integrations/telegram/handlers/commands/execution.py tests/core/orchestration/test_interfaces.py tests/core/orchestration/test_runtime_threads.py tests/core/orchestration/test_service.py tests/integrations/discord/test_message_turns.py tests/test_telegram_pma_routing.py tests/agents/test_harness_contract.py tests/agents/test_plugin_agent_conformance.py`
  - `.venv/bin/ruff check src/codex_autorunner/agents/base.py src/codex_autorunner/agents/codex/harness.py src/codex_autorunner/agents/opencode/harness.py src/codex_autorunner/agents/zeroclaw/harness.py src/codex_autorunner/core/orchestration/interfaces.py src/codex_autorunner/core/orchestration/models.py src/codex_autorunner/core/orchestration/service.py src/codex_autorunner/integrations/discord/message_turns.py src/codex_autorunner/integrations/discord/service.py src/codex_autorunner/integrations/telegram/handlers/commands/execution.py tests/core/orchestration/test_interfaces.py tests/core/orchestration/test_runtime_threads.py tests/core/orchestration/test_service.py tests/integrations/discord/test_message_turns.py tests/test_telegram_pma_routing.py tests/agents/test_harness_contract.py tests/agents/test_plugin_agent_conformance.py`
  - `.venv/bin/mypy src/codex_autorunner/core/orchestration/service.py src/codex_autorunner/integrations/telegram/handlers/commands/execution.py src/codex_autorunner/agents/base.py src/codex_autorunner/agents/codex/harness.py src/codex_autorunner/agents/opencode/harness.py src/codex_autorunner/agents/zeroclaw/harness.py`
  - `.venv/bin/pytest tests/core/orchestration/test_service.py tests/core/orchestration/test_runtime_threads.py tests/core/orchestration/test_interfaces.py tests/integrations/discord/test_message_turns.py tests/test_telegram_pma_routing.py tests/agents/test_harness_contract.py tests/agents/test_plugin_agent_conformance.py -q`
  - Result: `All checks passed!`; `Success: no issues found in 6 source files`; `136 passed`
- Routed ticket-flow / agent-pool delegation onto orchestration-owned thread/execution state:
  - `.venv/bin/ruff check src/codex_autorunner/integrations/agents/agent_pool_impl.py tests/test_opencode_agent_pool.py tests/test_ticket_flow_approval_config.py`
  - `.venv/bin/mypy src/codex_autorunner/integrations/agents/agent_pool_impl.py`
  - `.venv/bin/pytest tests/test_opencode_agent_pool.py tests/test_ticket_flow_approval_config.py -q`
  - Result: `All checks passed!`; `Success: no issues found in 1 source file`; `10 passed`

## Follow-up
- `TICKET-730-route-agent-pool-delegation-through-orchestration-thread-targets.md` remains as the narrower cleanup ticket for removing the remaining direct `BackendOrchestrator` execution dependency inside the delegation worker now that the queue-first orchestration state and thread-target contract are in place.
