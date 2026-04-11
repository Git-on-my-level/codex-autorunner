# Discord Interaction Runtime v2 Contract

This document records the implemented runtime v2 contract for Discord
interactions in CAR. The goal of the refactor was to collapse the mixed
interaction lifecycle into one authority for acknowledgement, execution,
delivery, and recovery, and the current branch now reflects that replacement.

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

### Current response-state owners

| Area | Current seam | Current ownership |
| --- | --- | --- |
| Ack + response session | `src/codex_autorunner/integrations/discord/interaction_session.py` plus `src/codex_autorunner/integrations/discord/response_helpers.py:DiscordResponder` | The responder/session pair owns initial callbacks, followups, original edits, component updates, autocomplete, and modal opens. |
| Handler-facing runtime boundary | `src/codex_autorunner/integrations/discord/interaction_runtime.py` plus `src/codex_autorunner/integrations/discord/effects.py:DiscordEffectServiceProxy` | Handler-facing modules route through typed runtime helpers and effect buffering instead of touching raw Discord primitives directly. |
| Composition root | `src/codex_autorunner/integrations/discord/service.py` | Service wires ingress, scheduler, ledger, responder, and effect sink together, while raw callback mutations are delegated through the responder/effect path. |

## Follow-up Hardening

The original correctness problem is materially fixed. The remaining work is
cleanup and hardening rather than another runtime replacement:

- Keep this document and the module docstrings aligned with the implemented
  runtime rather than the earlier cutover plan.
- Split `interaction_registry.py` by route family before it becomes the next
  brittle center of control.
- Continue moving handler-facing modules away from private service wrappers and
  onto typed runtime/proxy methods.
- The durable ledger is strong for single-process restart recovery. If CAR
  later supports overlapping Discord workers, add owner/expiry execution leases
  on top of the current recovery model.

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
- `handler_id`
- `conversation_id`
- `ack_mode`
- `scheduler_state`
- `resource_keys`
- `payload_json`
- `envelope_json`
- `delivery_cursor` with the pending/completed Discord callback payload
- `attempt_count`
- `updated_at`

In the current implementation this lease lives in
`interaction_ledger` inside
`src/codex_autorunner/integrations/discord/state.py` and carries both
execution state (`received`, `acknowledged`, `running`, `completed`, etc.)
and scheduler state (`dispatch_ready`, `waiting_on_resources`,
`executing`, `delivery_pending`, `delivery_replaying`, `completed`,
`delivery_expired`, `abandoned`).

### Recovery rules

- The runtime writes `payload_json` and `envelope_json` before any ack/defer
  side effect. Duplicate deliveries and restart recovery decide from disk, not
  from process-local memory.
- On startup, the runtime scans non-terminal ledger rows. The scan reconstructs
  the runtime envelope directly from the stored lease row.
- If the row has a durable ack marker but no business execution, startup
  resubmits execution in replay mode and restores the session state without a
  second Discord ack.
- If the row has a pending delivery cursor, startup replays only that Discord
  callback and does not re-enter handler business logic.
- Duplicate `INTERACTION_CREATE` deliveries for an already completed row are
  rejected. Duplicates for a recoverable row are allowed back into the runtime
  so the durable lease can resume work after a post-ack crash.
- If a row never reached a durable ack state, recovery transitions it to
  `delivery_expired` because Discord no longer guarantees the initial callback
  window.
- If required recovery material is missing or malformed, recovery transitions
  the row to `abandoned` and emits an operator-visible log event.

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
| `interaction_dispatch.py` | post-ack runtime executor only; legacy normalized-dispatch path removed |
| `car_command_dispatch.py` | pure business router for `/car` commands; no ack or response-state decisions |
| `response_helpers.py` | replaced or narrowed into runtime responder/state machine owner |
| `service.py` | composition root only; wires gateway, runtime, outbox, and store, but does not call raw interaction callback primitives |

## Migration Map From Current Modules

This is the required migration map for the named seams in the ticket.

| Current module | Current seam | v2 destination |
| --- | --- | --- |
| `ingress.py` | `InteractionIngress.process_raw_payload()` now performs normalization and authz only | Return the normalized envelope and timing inputs. Ack side effects stay in the runtime responder/admission path. |
| `command_runner.py` | `submit()`, per-conversation/resource scheduling, replay modes | Accept the admitted runtime envelope, apply resource keys, persist scheduler state, and resume execution or delivery from ledger-backed recovery. |
| `interaction_dispatch.py` | `execute_ingressed_interaction()` | Keep only the post-ack executor shape. Route admission, authz, and ack are removed from this module. |
| `car_command_dispatch.py` | Service-driven subcommand fanout | Keep as business dispatch only. It may depend on normalized runtime context, never on raw Discord callback state. |
| `response_helpers.py` | Runtime responder plus delivery cursor replay | Own durable response-state restoration and the raw transport calls behind the session/effect path. |
| `service.py` | `_on_dispatch()` admits runtime envelopes and wires scheduler/responder/store together | `service.py` is a wiring layer. Queue policy lives in the scheduler and response delivery lives behind the responder/effect path. |

## Post-Cutover Priorities

The remaining priorities are follow-up hardening, not runtime coexistence:

1. Keep the runtime contract docs, module docstrings, and boundary tests aligned
   with the code as the v2 branch settles.
2. Continue narrowing handler-facing code onto typed runtime helpers and effect
   proxies so private service hooks do not creep back into new work.
3. Split registry construction by route family before adding another large
   interaction surface.
4. Add true owner/expiry execution leases only if the Discord runtime ever
   needs active-active execution rather than single-process restart recovery.

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
