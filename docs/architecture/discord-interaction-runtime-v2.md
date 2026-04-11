# Discord Interaction Runtime v2 Contract

This document freezes the target runtime contract for Discord interactions in
CAR before the refactor lands. The goal is to collapse the current mixed
interaction lifecycle into one authority for acknowledgement, execution,
delivery, and recovery.

This is an adapter-layer contract. It must remain consistent with
`docs/ARCHITECTURE_BOUNDARIES.md`: Discord stays in
`src/codex_autorunner/integrations/discord/`, does not become a source of
business truth, and continues to translate Discord events into CAR-owned
runtime actions.

## Scope

Runtime v2 covers:

- `INTERACTION_CREATE` ingress from the Discord gateway
- acknowledgement and defer decisions
- post-ack scheduling and execution
- followup delivery and component-message updates
- idempotency and restart recovery
- temporary coexistence with the current runtime behind a feature flag

Runtime v2 does not change:

- Discord message-turn handling for ordinary `MESSAGE_CREATE`
- CAR business behavior for `/car`, `/pma`, flow actions, or ticket actions
- the `DiscordRestClient` transport implementation in `rest.py`

## Current Interaction Runtime

The Discord gateway cutover now routes all admitted interaction kinds through a
single runtime-owned admission and execution path.

### Current interaction entry points

| Area | Current seam | Current role | Why it must change |
| --- | --- | --- | --- |
| Gateway ingress | `src/codex_autorunner/integrations/discord/service.py` via `_on_dispatch()` | Handles `INTERACTION_CREATE`, calls `InteractionIngress.process_raw_payload()`, builds one runtime admission envelope, applies any dispatch-time ack, then submits to `CommandRunner` | This is now the single gateway admission path. |
| Raw normalization + authz | `src/codex_autorunner/integrations/discord/ingress.py` via `InteractionIngress.process_raw_payload()` | Normalizes payloads, resolves command contract metadata, performs authz, and records timing inputs | Ingress no longer mutates Discord ack state. |
| Background execution | `src/codex_autorunner/integrations/discord/command_runner.py` | Runs admitted interactions off the hot path, preserves conversation order, and applies queue-wait ack policy from the runtime admission envelope when needed | Scheduling is now driven by the admitted envelope instead of service-specific fast-ack callbacks. |
| Post-admission execution path | `src/codex_autorunner/integrations/discord/interaction_dispatch.py:execute_ingressed_interaction()` | Executes already-admitted interactions from `CommandRunner` | This is now the only interaction execution path. |
| Command-family dispatch | `src/codex_autorunner/integrations/discord/car_command_dispatch.py` | Routes `/car ...` subcommands to service handlers | This can remain, but it must become a pure business dispatcher that does not influence ack or response state. |

### Current mutable response helpers

| Area | Current seam | Raw Discord primitive touched today | Why it must change |
| --- | --- | --- | --- |
| Token policy cache + primary/followup response | `src/codex_autorunner/integrations/discord/response_helpers.py:DiscordResponder` | `create_interaction_response()`, `create_followup_message()`, `edit_original_interaction_response()` | Response state lives in an in-memory token cache with no durable recovery record. |
| Component defer/update | `src/codex_autorunner/integrations/discord/service.py` via `_defer_component_update()` and `_update_component_message()` | `create_interaction_response()` type `6` and `7` | Component response mutation is split between service and `response_helpers.py`. |
| Autocomplete | `src/codex_autorunner/integrations/discord/service.py:_respond_autocomplete()` | `create_interaction_response()` type `8` | Raw primitive is still directly exposed in service. |
| Modal launch | `src/codex_autorunner/integrations/discord/service.py:_respond_modal()` | `create_interaction_response()` type `9` | Raw primitive is still directly exposed in service. |

## Remaining Gaps

The admission-path cutover is now in place, but the broader v2 contract is not
fully complete yet:

- `command_runner.py` still owns only in-memory scheduling and ordering.
- `response_helpers.py` still tracks prepared-response policy by interaction
  token, so restart durability is incomplete.
- `service.py` still exposes raw response mutations for component update,
  autocomplete, and modal responses.

The result is that the repo still has multiple effective lifecycle authorities:

- more than one module can touch raw interaction callback primitives
- ordering is conversation-aware only for part of the runtime
- restart recovery is best-effort rather than modeled

## Target Invariants

Runtime v2 must satisfy all of the following:

- One ack owner: exactly one runtime component decides whether an interaction is
  answered immediately, deferred ephemeral, deferred public, deferred component
  update, autocomplete-only, or modal-open.
- One execution path: after ingress succeeds, every interaction is routed
  through the same post-ack executor interface, regardless of interaction kind.
- One response state machine: response mutation state is represented as a
  durable runtime record, not an implicit token cache plus service wrappers.
- One registry: slash commands, components, modals, and autocomplete are all
  described by one registry contract with route keys, ack policy, and resource
  locking metadata.
- One scheduler authority: queue choice, ordering, dedupe, and restart resume
  all belong to one runtime scheduler instead of being split between
  `service.py` and `command_runner.py`.

## Target Ownership Boundaries

Runtime v2 keeps Discord in the adapter layer and narrows module ownership.

### Allowed raw Discord response primitive owners after the refactor

After v2, only these modules may touch raw Discord interaction response
primitives:

- `src/codex_autorunner/integrations/discord/rest.py`
- a dedicated runtime responder module that replaces the mutable parts of
  `response_helpers.py` and owns all calls to:
  - `create_interaction_response()`
  - `create_followup_message()`
  - `edit_original_interaction_response()`

The following modules must not call those raw primitives after cutover:

- `src/codex_autorunner/integrations/discord/service.py`
- `src/codex_autorunner/integrations/discord/interaction_dispatch.py`
- `src/codex_autorunner/integrations/discord/car_command_dispatch.py`
- any command handler module under
  `src/codex_autorunner/integrations/discord/car_handlers/`

Those modules may request response operations only through the runtime
responder/state-machine interface.

Handler-facing rule after cutover:

- business handlers and command modules must go through
  `src/codex_autorunner/integrations/discord/interaction_runtime.py` for
  defer-state checks and runtime defer/followup transitions
- low-level service helpers such as `_defer_*`,
  `_send_followup_*`, and `_interaction_has_initial_response()` stay owned by
  the runtime boundary (`service.py`, `effects.py`, and
  `interaction_runtime.py`) and are not a handler API

### Target module boundaries

| Ownership area | Target boundary |
| --- | --- |
| Gateway surface | `service.py` remains the composition root and gateway callback owner. It may construct the runtime and pass payloads in, but it must not decide ack policy or mutate Discord interaction callbacks directly. |
| Ingress normalization | `ingress.py` becomes payload parsing, authz evaluation, and route-key extraction only. It returns a normalized envelope and never calls Discord. |
| Registry | A single runtime registry maps route keys to handler metadata: handler id, ack policy, ack timing, resource keys, and delivery mode. Command-contract lookup for slash commands moves behind this registry so components/modals/autocomplete use the same model. |
| Ack + response state | A dedicated responder/state-machine module replaces token-cache behavior in `response_helpers.py` and absorbs `service.py` modal/autocomplete/component-update raw response code. |
| Scheduler | `command_runner.py` evolves into the only runtime scheduler authority. It chooses inline-vs-queued execution and owns per-resource serialization, dedupe, leases, and restart resume. |
| Execution | `interaction_dispatch.py` is split so the surviving path is "execute an already-admitted runtime envelope." The legacy normalized-dispatch entry point is removed after cutover. |
| Business routing | `car_command_dispatch.py` remains a pure handler router. It receives an already-acked execution context and returns delivery intents or business results. |

## Routing Model

Runtime v2 uses one routing model for all Discord interaction kinds.

### Route keys

The registry route key is a stable, normalized string:

- Slash command: `slash:car/status`, `slash:car/session/reset`
- Component: `component:tickets_filter_select`, `component:flow_action_select`
- Modal: `modal:tickets_modal`
- Autocomplete: `autocomplete:car/review:commit`

Rules:

- Slash command keys use normalized command path segments joined by `/`.
- Component and modal keys are derived from stable custom-id families, not
  opaque per-message tokens.
- Autocomplete keys include the command path plus the focused field name.
- Route-key extraction happens before ack so unknown routes fail deterministically.

### Registry record

Each registry entry must define:

- `route_key`
- `handler_id`
- `interaction_kind`
- `ack_policy`
- `ack_timing`
- `delivery_mode`
- `resource_keys`
- `idempotency_scope`

The registry is the only source of truth for acknowledgement behavior.

## Scheduler And Resource Keys

Runtime v2 keeps scheduling in the adapter layer, but makes ordering explicit.

### Required resource keys

At minimum, every routed interaction must declare these keys when applicable:

- Conversation ordering key:
  `conversation:discord:<channel_id>:<guild_id|->`
- Workspace mutation lock:
  `workspace:<canonical_workspace_path>`

Additional optional keys may be added by the registry when needed:

- `binding:discord:<channel_id>`
- `run:<run_id>`
- `thread-target:<thread_target_id>`

### Scheduler rules

- The scheduler is the only authority that decides whether work runs inline or
  queued after ack.
- All ingress-accepted interactions submit through the same scheduler API with
  explicit resource keys. Interactions that do not need serialization, such as
  autocomplete, pass no resource keys and run immediately through that same API.
- Slash commands that mutate repo or workspace state must serialize on both the
  conversation key and workspace key.
- Components and modal submissions may run inline only if their registry entry
  explicitly says they are non-blocking and do not need cross-event ordering.
- Autocomplete always bypasses durable queueing, but it still goes through the
  same registry and ack owner.
- Scheduler admission must be idempotent on interaction id.

## Response State Machine

Runtime v2 must replace implicit prepared-token behavior with a durable
interaction state machine keyed by `interaction_id`.

### States

```text
RECEIVED
  -> REJECTED
  -> ROUTED
  -> ACKING
  -> ACKED_IMMEDIATE
  -> ACKED_DEFERRED_EPHEMERAL
  -> ACKED_DEFERRED_PUBLIC
  -> ACKED_COMPONENT_UPDATE
  -> ACKED_AUTOCOMPLETE
  -> ACKED_MODAL
  -> SCHEDULED
  -> EXECUTING
  -> DELIVERING
  -> COMPLETED
  -> FAILED
  -> EXPIRED
  -> ABANDONED
```

### Transition rules

- `RECEIVED -> REJECTED` is allowed for normalization or authz failure.
- `ROUTED -> ACKING` happens exactly once.
- Any `ACKED_*` state is terminal for the initial callback and becomes the basis
  for later delivery behavior.
- `ACKED_IMMEDIATE`, `ACKED_AUTOCOMPLETE`, and `ACKED_MODAL` may go directly to
  `COMPLETED` if no post-ack work exists.
- Deferred states must pass through `SCHEDULED` and `EXECUTING` before final
  delivery.
- `DELIVERING` may retry followups or original-message edits without re-running
  business logic.
- `FAILED` means handler logic failed after ack.
- `EXPIRED` means Discord no longer accepts the followup/edit path.
- `ABANDONED` is a controlled operator-visible terminal state used during
  cutover or recovery when the runtime cannot safely continue.

## Idempotency Contract

Runtime v2 idempotency is keyed on `interaction_id`.

Rules:

- The runtime must write an interaction lease record before ack side effects.
- Duplicate `INTERACTION_CREATE` deliveries for the same `interaction_id` must
  not produce a second ack or a second handler execution.
- Handler re-entry after restart is allowed only when the prior attempt never
  reached `EXECUTING` completion, and the runtime can prove the state is still
  resumable.
- Delivery retries after execution completion must reuse stored response state
  and must not re-run business logic.

Durable state belongs in Discord transport state, not in service-local memory.
The natural home is `src/codex_autorunner/integrations/discord/state.py` backed
by `.codex-autorunner/discord_state.sqlite3`, which already owns Discord
delivery state.

## Restart Recovery

Restart recovery is explicitly post-ack aware.

### Durable runtime record

Each interaction lease record must persist at least:

- `interaction_id`
- `interaction_token`
- `route_key`
- `interaction_kind`
- `ack_state`
- `scheduler_state`
- `resource_keys`
- `payload_json`
- `handler_id`
- `delivery_cursor` or equivalent followup/edit status
- `attempt_count`
- `updated_at`

### Recovery rules

- On startup, the runtime scans for interaction rows in `ACKED_*`,
  `SCHEDULED`, `EXECUTING`, or `DELIVERING`.
- If the interaction never reached `EXECUTING`, it may be rescheduled without a
  second ack.
- If execution finished but delivery did not, recovery resumes delivery only.
- If Discord rejects the stored token or edit target during recovery, the row
  transitions to `EXPIRED` and the runtime logs an operator-visible event.
- Recovery must never rerun a completed business mutation unless the handler is
  explicitly declared replay-safe.

## Before/After Module Map

### Before

| Current module | Current effective ownership |
| --- | --- |
| `ingress.py` | normalization, authz, command-contract lookup, ack/defer, telemetry |
| `command_runner.py` | in-memory FIFO plus conversation queueing |
| `interaction_dispatch.py` | legacy normalized dispatcher path plus post-ack execution path |
| `car_command_dispatch.py` | business routing for `/car` commands |
| `response_helpers.py` | token-scoped response policy cache plus response/followup/edit primitives |
| `service.py` | composition root plus extra raw response helpers for modal, autocomplete, and component update |

### After

| Current module | Target v2 ownership boundary |
| --- | --- |
| `ingress.py` | normalize payload, authz, derive route key and normalized envelope; no raw Discord response calls |
| `command_runner.py` | single scheduler authority with durable interaction leases, resource-key locking, and restart resume |
| `interaction_dispatch.py` | post-ack runtime executor only; legacy normalized-dispatch path removed after cutover |
| `car_command_dispatch.py` | pure business router for `/car` commands; no ack or response-state decisions |
| `response_helpers.py` | replaced or narrowed into runtime responder/state machine owner |
| `service.py` | composition root only; wires gateway, runtime, outbox, and store, but does not call raw interaction callback primitives |

## Migration Map From Current Modules

This is the required migration map for the named seams in the ticket.

| Current module | Current seam | v2 destination |
| --- | --- | --- |
| `ingress.py` | `InteractionIngress.process_raw_payload()` does normalization, authz, and ack | Split into envelope creation plus registry lookup. Ack side effects move to the runtime responder. |
| `command_runner.py` | `submit()`, `submit_ingressed()`, per-conversation queues | Collapse into one scheduler API that accepts runtime envelopes, applies resource keys, persists leases, and resumes on restart. |
| `interaction_dispatch.py` | `handle_normalized_interaction()` and `execute_ingressed_interaction()` coexist | Keep only the post-ack executor shape. Route admission, authz, and ack are removed from this module. |
| `car_command_dispatch.py` | Service-driven subcommand fanout | Keep as business dispatch only. It may depend on normalized runtime context, never on raw Discord callback state. |
| `response_helpers.py` | Prepared token cache and response helpers | Replace with a durable responder/state machine owner. No response policy inferred from transient in-memory cache. |
| `service.py` | `_on_dispatch()` chooses queues; `_defer_component_update()`, `_respond_autocomplete()`, `_respond_modal()`, `_update_component_message()` touch raw primitives | `service.py` becomes a wiring layer. Those raw response methods move behind the runtime responder. Queue policy moves to the scheduler. |

## Feature Flag And Cutover Plan

Old and new paths must coexist temporarily.

### Flag shape

Introduce a runtime flag under Discord config:

- `discord_bot.interaction_runtime_v2.enabled`

Recommended temporary supporting flags:

- `discord_bot.interaction_runtime_v2.shadow_compare`
- `discord_bot.interaction_runtime_v2.route_allowlist`

### Cutover stages

1. Implement the runtime registry, responder, and durable lease record behind
   the disabled feature flag.
2. Run the v2 ingress path in shadow mode from `service.py:_on_dispatch()`:
   build the same normalized envelope, resolve route keys, and log differences
   against the current runtime without changing behavior.
3. Cut slash-command traffic to v2 first, because it already uses ingress plus
   `CommandRunner.submit_ingressed()`.
4. Cut component, modal, and autocomplete traffic to v2 after the runtime
   responder absorbs `_defer_component_update()`,
   `_respond_autocomplete()`, `_respond_modal()`, and
   `_update_component_message()`.
5. Delete the legacy dispatcher entry point
   `interaction_dispatch.handle_normalized_interaction()` after all Discord
   interaction kinds run through the v2 executor.
6. Remove shadow flags and obsolete service wrappers only after restart-recovery
   characterization passes.

## Logging And Observability Expectations

The existing operational guidance in `docs/AGENT_SETUP_DISCORD_GUIDE.md`
already points operators to:

- `discord.ingress.completed`
- `discord.runner.stalled`
- `discord.runner.timeout`
- `discord.runner.execute.done`

Runtime v2 must preserve those signals or replace them with strictly better
names that still expose:

- ingress latency
- ack latency and ack policy
- scheduler queue wait
- execution duration
- delivery retries
- recovery resume decisions
- duplicate-interaction drops

## Non-Goals

- No new business logic inside Discord runtime modules.
- No new source of truth outside `.codex-autorunner/` state roots.
- No adapter-layer ownership of durable workspace/run truth that belongs in CAR
  orchestration state.
- No direct command handlers calling raw Discord callback primitives.
