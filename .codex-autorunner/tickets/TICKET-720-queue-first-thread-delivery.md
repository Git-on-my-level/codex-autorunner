---
title: "Make thread-target delivery queue-first by default and preserve first-class interrupt"
agent: "codex"
done: false
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
