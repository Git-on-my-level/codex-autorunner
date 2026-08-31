# Historical scars adopted by the CAR v3 rewrite

This is not a general v2 retrospective. It records failures, fixes, and hard-earned
contracts that materially constrain the v3 attention router and capability-provider
rewrite. The binding architecture remains [ADR 0001](./0001-attention-router-capability-providers.md)
and [`v3/DESIGN.md`](../../DESIGN.md); this document preserves why their less-obvious
invariants exist.

Evidence paths and line numbers refer to the repository state when ADR 0001 was accepted
on 2026-08-27. Commit ids identify representative fixes, not necessarily the first time
the underlying problem appeared.

## 1. Durable work and recovery

### Canonical rows must replay after restart

- **Scar:** In-memory or compatibility-file queues left work stuck after a hub restart.
  Commit `47e43c29` restored PMA wake-up lanes after restart.
- **Evidence:** `docs/ops/pma-queue.md:5-17,37-55` and
  `tests/core/test_pma_queue_cross_process.py:123-147`.
- **Adopted invariant:** every router, provider, effect, observation, and outbox worker
  discovers and replays eligible work from canonical SQLite rows. Mirrors, directories,
  process handles, and surface caches never gate draining.

### Idempotency must survive concurrent races and disagreeing replays

- **Scar:** A read-then-insert check is insufficient when multiple producers race, and
  blindly accepting a reused key can hide different work.
- **Evidence:** `tests/core/test_pma_queue_cross_process.py:150-210`. The v3 Store now
  reserves source-scoped identity transactionally and reports payload conflicts.
- **Adopted invariant:** unique constraints plus atomic insert-or-return-existing for
  events, human taps, provider requests, effects, grants, escalation intents, and
  deliveries. Same key/same payload is a duplicate; same key/different payload is an
  observable conflict.

### Leases need fencing tokens

- **Scar:** Expiry-only claims let a stale worker finish after a replacement reclaims
  its work.
- **Evidence:** the original scaffold path at `v3/src/store/db.ts:169-198`; current
  event/provider/effect/outbox workers use owner plus opaque claim-token CAS writes.
- **Adopted invariant:** claim atomically writes owner, opaque token, and expiry. Renew,
  complete, and terminal writes compare the token. Expired reclaim emits durable
  recovery evidence.

### Terminal truth comes before optional side effects

- **Scar:** Delivery or finalization failures kept completed turns `running` and blocked
  the next turn. Commit `66677a19` introduced the lifecycle/side-effect split.
- **Evidence:** `docs/architecture/managed-turn-lifecycle-contract.md:3-7,24-49,51-84`.
- **Adopted invariant:** provider invocations and effects durably record exactly one
  terminal outcome before delivery, provider-memory observation, timeline rendering,
  or cleanup. Same-outcome replay is idempotent; a conflicting outcome preserves the
  first fact and emits conflict evidence.

### Lifecycle owners must be explicit and disjoint

- **Scar:** Shared tables and convenient lane or surface conventions allowed unrelated
  owners to collide. v2 ultimately required authoritative `source_kind` partitioning.
- **Evidence:** `docs/ops/pma-queue.md:19-35` and
  `docs/architecture/unified-chat-ownership.md:45-74`.
- **Adopted invariant:** core alone owns event, incident, grant, decision, effect,
  receipt, and audit lifecycle. Providers own only namespaced provider state; adapters
  and surfaces submit inputs and consume projections.

### Retention and backpressure fail closed around unresolved work

- **Scar:** Cleanup based on mirrors, weak activity evidence, or broad file classes can
  remove active truth or disagree with its dry run.
- **Evidence:** `docs/ops/state-cleanup.md:8-31,98-177` and
  `docs/ops/pma-queue.md:65-76`.
- **Adopted invariant:** explicit byte/age/count budgets; never prune active claims,
  unresolved incidents, active grants, nonterminal effects, or required decision
  evidence. Uncertain cleanup/admission becomes visible blocked state, not silent drop.

## 2. Attention, delivery, and human handoff

### Persist an attention intent before sending it

- **Scar:** Transient delivery failure could erase a terminal wake-up. Commit `4b6dc4cb`
  repaired missing terminal wakeups.
- **Evidence:** `src/codex_autorunner/core/orchestration/execution_result_coordinator.py:378-426`
  and `tests/core/orchestration/test_execution_result_coordinator.py:89-125`.
- **Adopted invariant:** escalation, notification, and delivery are row-first. Sending
  is a separately claimable and recoverable effect.

### One intent has one delivery owner

- **Scar:** Durable and legacy completion paths could both send. Commit `93a83842`
  stopped duplicate Telegram finalization.
- **Evidence:** `src/codex_autorunner/adapters/telegram/handlers/commands_runtime.py:409-445`
  and `tests/test_telegram_turn_queue.py:707-740`.
- **Adopted invariant:** stable intent id, exclusive claim, and per-channel receipt
  ledger. No fallback path may send after another owner has claimed or completed it.

### Remote delivery uncertainty is not ordinary failure

- **Scar:** A timeout after remote acceptance can produce a duplicate when retried. The
  original v3 outbox also sent first and marked sent second without a durable claim.
- **Evidence:** `src/codex_autorunner/adapters/chat/outbox_kernel.py:214-245` and the
  pre-rewrite `v3/src/surfaces/telegram/outbox.ts` path.
- **Adopted invariant:** receipts distinguish `sent`, `uncertain`, and `failed`.
  Reconcile uncertain delivery using remote idempotency/status where possible; otherwise
  surface "possibly delivered" instead of blindly retrying. Digest archive rows and
  held alerts are folded only after a canonical `delivered` receipt; `sent_at` and
  dead-man digest evidence are never enqueue-time claims.

### Delivered receipts must be able to rebuild interaction projections

- **Scar:** A Telegram send can commit its canonical receipt and crash before writing
  the escalation message id and callback binding. The visible card then exists but its
  legitimate button taps look stale.
- **Evidence:** the receipt/projection fault-injection coverage in
  `v3/test/telegram/outbox.test.ts`.
- **Adopted invariant:** the delivered row retains all remote message/thread identity.
  Callback maps, escalation timestamps, thread ids, anchors, and ticker ids are atomic,
  idempotent projections replayed from that row without another transport send.

### Route by provenance and binding, not guessed fan-out

- **Scar:** Web-origin PMA notifications leaked into external surfaces until commit
  `e26cd1d7` made origin and fallback policy explicit.
- **Evidence:** `src/codex_autorunner/core/pma_dispatch_decision.py:192-301` and
  `tests/core/test_pma_dispatch_decision.py:82-127,229-287`.
- **Adopted invariant:** every attention intent records origin, explicit target,
  binding id/revision, allowed fallback, chosen primary recipient, and routing reason.
  Fallback is deterministic and never implicit fan-out.

### Human handoff identity must survive delivery and continuation

- **Scar:** Replyability, duplicate cross-surface pause notices, and notification
  navigation repeatedly broke (`dfd21b96`, `95036cd2`, `0d0afa07`).
- **Evidence:** `src/codex_autorunner/core/pma_notification_store.py:106-241,249-384`
  and `tests/core/test_chat_delivery.py:200-284`.
- **Adopted invariant:** correlation id, chosen surface/message, reply target,
  continuation identity, and deep link are durable core facts. One primary recipient
  is selected before delivery.

### Intentional quiet and routing failure are different outcomes

- **Scar:** "No message" hid suppression, missing targets, stale notices, and failed
  routing behind the same absence.
- **Evidence:** `docs/AGENT_SETUP_TELEGRAM_GUIDE.md:58-80,157-168` and
  `tests/chat_surface_integration/test_hermes_pma_ux_regressions.py:544-569`.
- **Adopted invariant:** typed outcomes include `suppressed_by_policy`, `no_target`,
  `delivery_failed`, `abandoned`, `superseded`, and `expired`; none may masquerade as
  delivered or silently vanish.

### Interaction acknowledgements and approvals are replay-safe core facts

- **Scar:** Stale Discord controls and non-durable acknowledgement state could rerun
  business logic; late approval taps depended on volatile pending objects.
- **Evidence:** `docs/AGENT_SETUP_DISCORD_GUIDE.md:7-39,53-80` and
  `src/codex_autorunner/adapters/chat/handlers/approvals.py:17-47,97-160`.
- **Adopted invariant:** interaction id, acknowledgement state, replay disposition,
  target turn/effect, issuer, scope, expiry, and consumed/rejected outcome belong to the
  core ledger. UI prompt delivery is separate from authorization truth.

### Approval scope needs verified repo identity and per-request lineage

- **Scar:** Commit `7f2f22e2` fixed three v3 dogfood failures together: permission
  prompts lacked reliable repo scope, unrelated permission requests collapsed into one
  lineage, and approval content was not checked by a non-bypassable rail. The current
  Claude normalizer also derives a repo label from the final cwd segment, which is not
  a safe grant identity.
- **Evidence:** `v3/src/policy/index.ts:41-90`, `v3/src/ports.ts:35-45`, and
  `v3/src/ingest/claude.ts:129-143`.
- **Adopted invariant:** one-shot approvals bind to a verified VCS repo identity,
  native request/tool id, target session, canonical effect type, normalized argument
  fingerprint, and immutable lineage. Reusable grants may omit grant-level lineage only
  as an explicit cross-request choice while retaining exact verified scope, effect, and
  argument constraints; every resulting effect still has distinct immutable lineage.
  A cwd basename, provider prose, or accidental session bucket cannot authorize an
  effect.

### Authorization and claim are one safety boundary

- **Scar:** A valid authorization can wait long enough for a grant, budget, rate limit,
  breaker, panic state, deadline, or dangerous argument evidence to change before the
  worker claims it.
- **Evidence:** delayed/restart claim tests in `v3/test/safety/safety.test.ts` and the
  transactional claim path in `v3/src/store/db.ts`.
- **Adopted invariant:** the `pending -> running` claim transaction revalidates every
  non-bypassable rail and consumes bounded grant authority atomically. Failed rails
  block without spending the grant; effect costs derived from one provider decision
  sum to one conservatively rounded charge rather than one charge per effect.

### Stream semantics must be typed

- **Scar:** Snapshot/delta confusion created duplicate assistant bubbles and an observed
  861 KB repeated reasoning block (`d23a0dad`, `39aa46a6`, `21568c2b`).
- **Evidence:** `src/codex_autorunner/core/orchestration/progress_projection.py:167-222`.
- **Adopted invariant:** provider events distinguish started, heartbeat, progress delta,
  cumulative snapshot, artifact, final answer, terminal result, and recovery state.
  Core performs semantic dedupe and owns the canonical timeline.

## 3. Provider host and Hermes

### Protocol and capabilities are exact and fail closed

- **Scar:** Early plugin loading tolerated ambiguous versions, broad exceptions, and
  legacy capability names; commit `b07d4f8c` tightened the contract.
- **Evidence:** `docs/plugin-api.md:28-42,99-122` and
  `tests/agents/test_registry_capabilities.py:61-79`.
- **Adopted invariant:** exact API version, canonical closed capability vocabulary,
  bounded loader failures, duplicate-id rejection, typed unsupported errors, and
  side-effect-free discovery.

### Registration is not readiness

- **Scar:** A statically registered agent could still be missing, incompatible, or
  unable to start.
- **Evidence:** `docs/plugin-api.md:106-115` and
  `tests/test_doctor_checks.py:1461-1510`.
- **Adopted invariant:** every provider has typed preflight and semantic health. Routing
  requires readiness; failure produces diagnostics and deterministic fallback.

### One schema drives parse, validation, doctor, and surfaces

- **Scar:** Config parser and validator semantics drifted until explicit schema-drift
  tests and commit `eb5338b9` expanded validation coverage.
- **Evidence:** `src/codex_autorunner/core/config_field_schema.py:1-7,94-213` and
  `tests/core/test_config_schema_drift.py:18-64`.
- **Adopted invariant:** provider configuration and topology have one versioned schema.
  Invalid or missing config blocks activation; it does not silently become a current
  default or another provider.

### Hermes identity includes profile and explicit continuity scope

- **Scar:** Alias/profile resolution launched the wrong Hermes configuration until
  fixes `b210fea2` and `a4fc6c46`. Shared `HERMES_HOME` also means sessions are not
  implicitly isolated.
- **Evidence:** `docs/ops/hermes-acp.md:55-85,110-123` and
  `tests/agents/hermes/test_hermes_supervisor.py:455-608`.
- **Adopted invariant:** provider id + instance/profile + continuity key + state root +
  config fingerprint are recorded on every invocation. State is isolated beneath the
  resolved instance and continuity key. Users may choose global, scoped,
  profile-specific, incident-isolated, or other explicit topology; ordered first-match
  selection is inspectable and CAR never silently merges scopes.

### Provider-private state cannot establish terminal truth

- **Scar:** Persisted identical Hermes output falsely completed a hung second turn;
  commit `b0740730` removed session-store recovery.
- **Evidence:** `tests/agents/hermes/test_hermes_supervisor_official_prompt_hang.py:94-140`
  and `docs/ops/hermes-acp.md:110-123`.
- **Adopted invariant:** only supported public protocol terminal events establish
  provider completion. Timeout and unknown remain timeout and unknown; CAR never reads
  provider files or transcript snapshots to invent an outcome.

### Executable providers require provenance and process identity

- **Scar:** Executable apps needed hash locks, trust, argv-only execution, path
  validation, and tamper refusal; PID reuse could signal unrelated processes until
  `81d61b76` hardened the reaper.
- **Evidence:** `docs/apps.md:163-176,235-252` and
  `docs/process-registry.md:17-47`.
- **Adopted invariant:** provider version, trust source, binary identity, argv,
  profile, state root, owner PID/PGID, and config fingerprint are durable. Verify owner
  and command identity before attach or kill. No marketplace or arbitrary installer in
  the initial native-plus-Hermes release.

### Runtime ownership is centralized and scope-configured

- **Scar:** Surface-created supervisors leaked and duplicated long-lived processes;
  fixes `1cad7b71`, `bd79ea97`, and `cb7578f7` added budgets, locks, startup reaping,
  and centralized ownership.
- **Evidence:** `docs/runtime-services-ownership.md:3-25` and
  `tests/test_opencode_supervisor_process_management.py:138-318`.
- **Adopted invariant:** one lifecycle owner per configured provider scope, cross-process
  single-flight attach/start, process and handle budgets, active-turn protection,
  startup reaping, and idempotent shutdown. Surface routing keys never own processes.

### Local is not automatically trusted

- **Scar:** Remote cookie/bootstrap and URL-token behavior required hardening in
  `83f90e1c` and `69de09e4`; localhost still needs Host/Origin protection.
- **Evidence:** `docs/web/security.md:7-32,63-84,99-119`.
- **Adopted invariant:** provider control and core mutation endpoints authenticate
  independently of grants. Nonlocal access adds HTTPS and explicit Host/Origin policy;
  credentials never appear in URLs or routine logs; being a local process is not proof
  of identity.

## 4. Migration and operations

### Do not invent historical facts during migration

- **Scar:** Current defaults, UI selections, and provider state were tempting but false
  sources for runtime backfill.
- **Evidence:** `docs/ops/runtime-identity-backfill.md:3-7,31-46,68-73`.
- **Adopted invariant:** v2-to-v3 import fills only facts reconstructable from durable
  evidence. Unknown and contradictory fields remain explicitly unknown/contradictory.
  Migration is one-way, auditable, and never a steady-state dual writer.

### PID liveness is not semantic health

- **Scar:** Dead, stale-alive, disconnected, intentionally stopped, and reaped workers
  collapsed into misleading statuses (`7ccf7716`, `471ac7f5`).
- **Evidence:** `tests/core/flows/test_flow_supervisor.py:92-172` and
  `docs/car_constitution/50_OBSERVABILITY_OPERATIONS.md`.
- **Adopted invariant:** daemon/provider state distinguishes startup, ready, progress,
  stale-alive, disconnect, timeout, user stop, watchdog, reaper, and crash with durable
  origin and recovery evidence.

### The daemon cannot certify its own liveness

- **Scar:** An in-process digest or watchdog says nothing while the daemon, host,
  credentials, or delivery path is dead.
- **Evidence:** operational consequence of the v2 two-month degraded/silent failure and
  the external-supervision contracts in `docs/ops/hub-single-owner-verification.md:38-119`.
- **Adopted invariant:** qualification includes one canonical daemon owner, explicit
  starting/ready/deferred/failed states, and a once-per-minute heartbeat to an
  operator-configured dead-man webhook outside CAR's host and delivery credentials.
  The heartbeat carries daemon identity, monotonic sequence, last durable progress,
  and last digest receipt; three misses alert through an independent destination.

## Rewrite acceptance use

Implementation tickets should cite the relevant scar and prove its adopted invariant
with contract, recovery, or end-to-end tests. A green happy-path test is insufficient
when the scar is specifically about duplicate delivery, restart, stale ownership,
ambiguous completion, scope leakage, or silent fallback.
