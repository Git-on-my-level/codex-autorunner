# Hermes ACP v1 Contract for CAR

This document freezes the CAR-facing Hermes integration contract before
implementation. Downstream Hermes tickets must implement within this envelope
instead of reopening resource-model or capability decisions.

## Scope

Hermes v1 is a runtime-backed agent integrated through ACP. It is not a CAR
native target, and it is not an `agent_workspace` runtime in v1.

## Resource Model

- Hermes is a repo/worktree-backed ACP agent.
- The durable CAR resource remains the CAR managed thread target bound to a repo
  or worktree.
- The durable Hermes resource is Hermes' native session/thread identifier.
- CAR stores the binding from `thread_target_id` to Hermes `session_id` in the
  existing runtime-binding path (`backend_thread_id` plus optional
  `backend_runtime_instance_id` metadata).
- Hermes owns native session durability and resume semantics.
- CAR must not treat Hermes as a first-class CAR-managed `agent_workspace`.
- CAR must not infer extra isolation guarantees from session selection alone.

## State And Isolation Model

- Hermes v1 runs against a shared `HERMES_HOME`.
- CAR does not create per-thread or per-worktree Hermes-home sandboxes in v1.
- CAR does not read Hermes internals, lock files, or Hermes databases directly.
- All Hermes state observation must come through ACP responses, ACP
  notifications, or CAR's own stored bindings and transcript mirrors.
- If the Hermes/ACP layer can report a runtime instance identifier, CAR may
  store it only to detect stale bindings after resets. It is not an isolation
  boundary.
- When a stored Hermes session binding is stale or missing, CAR clears the
  binding and starts a fresh Hermes session through the normal orchestration
  recovery path.

## Session And Turn Lifecycle

- `new_conversation()` creates a Hermes session and CAR persists the returned
  session ID as `backend_thread_id`.
- `resume_conversation()` resumes that Hermes session ID; if Hermes reports the
  session as missing, CAR clears the binding and starts fresh.
- `list_conversations()` is backed by Hermes ACP `session/list`, so CAR may
  advertise active-thread discovery for Hermes and preserve session titles when
  Hermes returns them.
- `start_turn()` sends the user prompt plus any CAR-injected context to the
  bound Hermes session.
- `interrupt()` targets the active Hermes turn for the bound session.
- CAR mirrors assistant output, raw events, and terminal outcomes into its own
  stores, but Hermes remains the source of truth for native session history.
- CAR does not promise transcript reconstruction beyond what it directly
  observed during CAR-managed turns.

## Capability Contract

### Supported v1 target capabilities

| Capability | v1 status | Contract |
| --- | --- | --- |
| `durable_threads` | Supported | Hermes sessions are durable and can be created, resumed, and bound to CAR thread targets. |
| `message_turns` | Supported | Hermes can execute normal message turns against a durable session. |
| `interrupt` | Supported | Hermes can cancel an in-flight turn for a bound session. |
| `active_thread_discovery` | Supported | Hermes exposes session listing through ACP `session/list`, so CAR can list and resume existing Hermes sessions. |
| `event_streaming` | Supported | Hermes can stream progress/events for active turns. |
| `approvals` | Supported target for v1 | Hermes permission requests are part of the v1 contract, but CAR must not advertise this capability until TICKET-160 delivers the full approval bridge end to end. |

### Unsupported v1 capabilities

| Capability | v1 status | Required behavior |
| --- | --- | --- |
| `review` | Unsupported | Fail with a capability-driven error. Do not route review requests into normal turns. |
| `model_listing` | Unsupported | Hermes may accept a free-form model override, but CAR must not promise a discoverable model catalog. |
| `transcript_history` | Unsupported | CAR may show its own mirrored outputs, but Hermes transcript history is not a supported API contract. |

### Model override rule

- A free-form model override string may be passed to Hermes only if the ACP
  layer or a documented Hermes extension accepts it safely.
- CAR must not invent provider/model parsing rules for Hermes based on OpenCode.
- Absence of `model_listing` does not block Hermes support.

## Event Normalization Expectations

Hermes ACP events must normalize into CAR-owned runtime events without exposing
Hermes internals as a CAR contract.

- Preserve raw ACP payloads for debugging, tailing, and transcript/timeline
  reconstruction.
- Normalize assistant text deltas into CAR assistant-stream deltas.
- Normalize final assistant messages into CAR assistant-message output when a
  distinct final payload exists.
- Normalize tool start/end or equivalent action events into `ToolCall` and
  `ToolResult` when Hermes emits enough data.
- Normalize token/usage events when Hermes provides them.
- Normalize reasoning/progress updates into CAR notices or progress events
  rather than dropping them.
- Normalize permission requests into CAR approval-request events with the raw
  Hermes/ACP context attached.
- Normalize turn completion or idle notifications into CAR completion markers.
- If Hermes emits less metadata than Codex or OpenCode, the stream must degrade
  to generic progress events instead of failing the turn or tail stream.
- CAR must be able to reconstruct the best-effort assistant result from either
  streamed deltas or the final message payload.

## Approval Behavior Expectations

- Hermes permission requests are a first-class part of the v1 contract.
- The generic ACP layer may surface permission events before CAR UX bridging is
  complete, but Hermes must not publicly advertise `approvals` until CAR can
  allow, deny, cancel, and time out those requests end to end.
- Approval requests must pause the active turn deterministically.
- Approval decisions must flow through CAR's existing per-thread approval mode
  and sandbox-policy rules rather than bypassing them.
- Deny, cancel, timeout, and lost-client cases must resolve to deterministic
  terminal behavior that is visible in logs and runtime events.

## UX And Error Rules

- Unsupported Hermes capabilities must fail with explicit capability-driven
  errors, not `Unknown agent` and not silent omission.
- Review requests for Hermes must fail as unsupported, not be forwarded as a
  plain text `/review` prompt.
- Model-list routes for Hermes must fail as unsupported capability, not as
  missing agent.
- Transcript-history requests for Hermes must fail as unsupported capability.
- Selector surfaces may show Hermes even when some actions are unsupported, but
  those actions must explain the missing capability clearly.

## Downstream Touchpoints And Ownership

The table below inventories the current agent-specific seams that Hermes needs
changed or explicitly gated. Owners refer to downstream Hermes tickets.

| Area | Current seam to touch | Why Hermes needs it | Owner |
| --- | --- | --- | --- |
| ACP core | `src/codex_autorunner/agents/acp/` (new package) | Generic stdio ACP transport, request/response plumbing, notifications, cancel, permission hook, and fake server fixture. | TICKET-120 |
| Config and validation | `src/codex_autorunner/core/config.py`, `src/codex_autorunner/core/config_validation.py` | Add Hermes config defaults and validate `hermes acp` launch config without implying `agent_workspace` semantics. | TICKET-130 |
| Discovery and doctor | `src/codex_autorunner/core/runtime.py`, `src/codex_autorunner/surfaces/cli/commands/doctor.py` | Surface Hermes readiness, missing binary/remediation, and shared-home assumptions through doctor/preflight. | TICKET-130 |
| Registry | `src/codex_autorunner/agents/registry.py` | Register Hermes with the exact v1 capability set and no false claims. | TICKET-140 |
| Hermes harness | `src/codex_autorunner/agents/hermes/` (new package) | Thin Hermes wrapper over ACP core for create/resume/list/turn/interrupt/event-stream behavior. | TICKET-140 |
| Capability/runtime mapping | `src/codex_autorunner/core/orchestration/runtime_thread_events.py` | Map Hermes/ACP event shapes into CAR runtime events while keeping raw payloads. | TICKET-140 |
| PMA turn execution | `src/codex_autorunner/surfaces/web/routes/pma_routes/chat_runtime.py` | Remove Codex/OpenCode-only assumptions in PMA runtime execution and stream handling. | TICKET-150 |
| PMA live tailing | `src/codex_autorunner/surfaces/web/routes/pma_routes/tail_stream.py` | Extend live tail support beyond Codex and ZeroClaw so Hermes turns can be tailed. | TICKET-150 |
| PMA managed-thread runtime | `src/codex_autorunner/surfaces/web/routes/pma_routes/managed_thread_runtime.py`, `src/codex_autorunner/core/orchestration/service.py`, `src/codex_autorunner/core/pma_thread_store.py` | Ensure Hermes session bindings are stored, recovered, cleared, and interrupted through the generic managed-thread path. | TICKET-150 |
| Approval bridge | `src/codex_autorunner/agents/acp/`, `src/codex_autorunner/agents/hermes/`, `src/codex_autorunner/integrations/chat/handlers/approvals.py`, `src/codex_autorunner/integrations/telegram/handlers/approvals.py` | Turn ACP permission requests into CAR approval UX and only advertise `approvals` when the full path works. | TICKET-160 |
| Web agent metadata | `src/codex_autorunner/surfaces/web/routes/agents.py`, `src/codex_autorunner/surfaces/web/routes/pma_routes/meta.py` | Replace hard-coded Codex/OpenCode model/event assumptions with registry-driven capability behavior. | TICKET-170 |
| CLI PMA metadata and unsupported actions | `src/codex_autorunner/surfaces/cli/pma_cli.py` | Remove static allowlists and make unsupported `model_listing`, `review`, and `transcript_history` actions fail clearly for Hermes. | TICKET-170 |
| Ticket-flow runtime allowlist | `src/codex_autorunner/integrations/agents/agent_pool_impl.py`, `src/codex_autorunner/tickets/agent_pool.py` | Ticket flow currently rejects non-Codex/OpenCode agents and still documents only those IDs. | TICKET-180 |
| Legacy backend/thread-key seams | `src/codex_autorunner/integrations/agents/backend_orchestrator.py`, `src/codex_autorunner/integrations/app_server/threads.py` | Hermes ticket flow should use the runtime-thread seam rather than extending old Codex/OpenCode thread-key state. | TICKET-180 |
| Shared chat and file chat catalogs | `src/codex_autorunner/integrations/chat/agents.py`, `src/codex_autorunner/integrations/chat/model_selection.py`, `src/codex_autorunner/surfaces/web/routes/file_chat.py`, `src/codex_autorunner/surfaces/web/routes/file_chat_routes/execution.py`, `src/codex_autorunner/integrations/app_server/threads.py` | Remove static Codex/OpenCode assumptions and gate Hermes cleanly where a surface still lacks support. | TICKET-190 |
| Telegram agent/model/review UX | `src/codex_autorunner/integrations/telegram/handlers/commands_runtime.py`, `src/codex_autorunner/integrations/telegram/handlers/selections.py`, `src/codex_autorunner/integrations/telegram/handlers/commands_spec.py` | Telegram currently assumes Codex/OpenCode rules for model UX and review flows; Hermes must either work or fail with capability-driven errors. | TICKET-200 |
| Discord selectors and unsupported actions | `src/codex_autorunner/integrations/discord/service.py` | Discord selection and command UX still assume current agents and need explicit Hermes gating for unsupported actions. | TICKET-210 |
| Operator docs and characterization | `docs/adding-an-agent.md`, `docs/ops/hermes-acp.md` (new), Hermes-focused tests | Document installation, shared `HERMES_HOME`, capability gaps, and characterize the high-risk end-to-end paths. | TICKET-220 |

## Explicit Non-Goals For v1

- No CAR-managed Hermes-home isolation.
- No direct reads from Hermes databases or internal state files.
- No Hermes review mode.
- No Hermes model catalog/listing contract.
- No Hermes transcript-history contract.
- No promise that Hermes matches Codex/OpenCode metadata richness in live tails.

## Scope Corrections Applied After Freezing This Contract

- TICKET-170 must explicitly own capability-driven failures for unsupported CLI
  metadata/actions, including `review` and `transcript_history`, not just model
  listing.
- TICKET-200 must explicitly own Telegram-side capability-driven rejection for
  Hermes review/model UX paths that remain unsupported.
- TICKET-210 must explicitly own Discord-side capability-driven rejection for
  unsupported Hermes actions so Hermes is not merely selectable but misleading.
