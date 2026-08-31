# CAR v3 — Target Design

**CAR v3 is a cross-vendor attention router.** It is the durable layer between you and
all of your agents (Claude Code, Codex, Hermes, OMP, agentctl-launched work, Multica
autopilots, CI, cron). It ingests lifecycle and attention events from everything,
normalizes them into one incident model, preserves human handoffs, routes replies back
to their source, and keeps one trustworthy record of "what needs me."

Autonomous operation, policy judgment, and learning are **capability providers**, not
the identity of CAR core. Providers propose typed effects; CAR's non-pluggable safety
kernel authorizes and executes them. The implementation ships a native provider with no
external agent runtime, service, or API key, plus a Hermes provider. The native provider
is also the deterministic fallback when no external intelligence is available.
The accepted boundary and product decisions are recorded in
[`docs/architecture/0001-attention-router-capability-providers.md`](./docs/architecture/0001-attention-router-capability-providers.md).

v3 replaces v2's runner/tickets/chat-surfaces product, now deprecated because its core
features are increasingly vendor-native. The one v2 primitive that survives,
generalized, is **Dispatch**: an agent's
`notify` vs `pause` (yield-for-human with a durable reply inbox) becomes the
`requires_response` field of the event contract, and the file reply inbox becomes the
universal reply-back fallback.

Design lineage: this doc merges two independent Fable design proposals (systems-lead and
brain/UX-lead) plus prior analysis of v2, agentctl, fleetctl, and hostctl. Patterns
deliberately stolen: agentctl (journal + `subscribe` callbacks, capability probing,
typed JSON-first CLI), fleetctl (unconditional daily digest, `/brief.md` endpoint for
agents to curl, stateless localhost UI), hostctl (behavior/config split, closed
vocabulary of typed action gates), v2 PMA safety (dedupe, rate limits, circuit breaker).
The concrete v2 failures and fixes carried forward are recorded in
[`docs/architecture/HISTORICAL_SCARS.md`](./docs/architecture/HISTORICAL_SCARS.md).

## Non-negotiables

1. **Durable SoT**: SQLite, crash-only daemon. Kill -9 at any point; restart resumes
   from table state. No authoritative in-memory queues.
2. **Unconditional daily digest**: CAR attempts a digest every day, even when there is
   nothing to report. A separately supervised dead-man check makes a missing digest
   visible; the daemon cannot prove its own liveness merely by intending to send one.
   (v2's founding failure: repos sat "degraded" in Telegram for two months, unnoticed.)
3. **Silence watchdog**: absence of expected source or provider progress is itself a
   typed event, while absence of all CAR output is detected outside the CAR daemon.
4. **Autonomy is granted, never inferred**: providers may learn and propose, but only an
   explicit human action creates or broadens a core-enforced grant. Full grant-based
   autonomy is supported for every effect class from the first dogfood release, always
   subject to non-bypassable core rails applied to canonical effect arguments.
5. **Providers propose; CAR executes**: operator and policy providers never receive a
   raw executor. Every effect crosses the core safety kernel, grant ledger, and audit.
6. **Failed delivery re-escalates**: a reply that can't reach its agent is loudly
   reported, never dropped.
7. **Side-by-side migration, not two products**: v3 uses its own state dir (`~/.car/`),
   port (7171), bot token, and temporary `card` CLI during alpha. v2 is deprecated now;
   it remains runnable only for migration, security, and critical correctness support.

## 1. Layout

The implementation is a single Bun package at `v3/` (no workspace — one process,
layered modules, with protocol/core/provider/surface boundaries). Legacy Python/Svelte
remains only as the deprecated v2 migration bridge.

```
v3/
  package.json  bunfig.toml  tsconfig.json  DESIGN.md (this file)
  schemas/                  # JSON Schema emitted from contract (generated, committed)
  src/
    contract/     # wire types + zod schemas + ids. FROZEN after scaffold. No internal imports.
    store/        # bun:sqlite, migrations, typed repositories. FROZEN interfaces after scaffold.
    config/       # TOML config + policy.toml load/validate/hot-reload
    ingest/       # Hono routes + per-source normalizers (agentctl, claude, multica, generic, telegram-note)
    router/       # claim, coalesce, incident lifecycle, deterministic routing
    providers/    # capability contracts, registry, invocation, native + Hermes adapters
    safety/       # non-pluggable grants, gates, budgets, breaker, irreversible-action rails
    effects/      # reply-back + typed effect execution; providers never execute directly
    surfaces/telegram/   # grammY bot: escalations, buttons, replies, commands
    surfaces/web/        # Hono server-rendered JSX + htmx; /brief.md
    digest/       # digest builder + silence watchdog
    daemon.ts     # composition root (router loops, below)
    cli.ts        # `card` CLI: serve | emit | status | doctor | migration-audit
  test/           # mirrors src/; test/fixtures/ = golden wire events per source
  ops/            # launchd plist + systemd unit templates + install notes
```

**Router stack** (Bun-native or pure TS; no ORM and no SPA framework): `bun:sqlite`
(WAL), `zod` (contracts), `hono` (HTTP + server-rendered JSX), `grammy` (Telegram,
long-polling — no inbound port), and `smol-toml` (config). Intelligence-specific
dependencies belong to provider implementations. The native provider requires no
external agent runtime, service, or API key. The first non-native provider is Hermes,
reached through Hermes's supported public ACP lifecycle and public profile selector.

## 2. Event wire contract (`car.event.v1`)

`POST /v1/events` (single JSON object or NDJSON batch). Published as zod source in
`src/contract/` and emitted JSON Schema in `schemas/`; served at `GET /v1/schema`.
Additive-only within v1.

```jsonc
{
  "contract": "car.event.v1",
  "idempotency_key": "claude-code:sess-abc:PermissionRequest:toolu_01X", // REQUIRED, source-scoped
  "ts": "2026-08-26T18:04:11Z",
  "source": { "vendor": "claude-code", "host": "davids-mbp", "adapter": "hook-http" },
  "session": {                       // nullable for sessionless sources (cron, CI)
    "vendor": "claude-code",         // claude-code|codex|cursor|hermes|omp|agentctl|multica|ci|cron|other
    "native_id": "sess-abc123",
    "host": "davids-mbp",
    "cwd": "/Users/dazheng/omi",
    "repo": "github.com/x/y",        // optional
    "title": "fix BLE reconnect"
  },
  "type": "attention.permission",    // closed vocab, below
  "severity": "attention",           // info | notice | attention | urgent
  "requires_response": true,
  "response_channel": {              // how a reply can get back; null = file fallback only
    "kind": "claude-hook-http",      // | codex-exec-resume | claude-resume | agentctl-run | multica-api | file
    "hint": { "tool_use_id": "toolu_01X", "deadline_ms": 110000 }
  },
  "title": "Permission: gh pr merge 42",
  "body": "…free text ≤16KB…",
  "payload": { },                    // vendor blob ≤32KB, stored verbatim, truncated with marker
  "expires_at": "2026-08-26T18:06:00Z"  // optional; resolving after this is moot
}
```

**Types (closed enum v1):** `session.started`, `session.ended` (payload: outcome, cost
if known), `attention.permission` (approve/deny), `attention.question` (free-text
answer), `attention.idle` (waiting on input), `attention.error` (failed/crashed),
`attention.cleared`, `progress`, `artifact`, `heartbeat`, `cost.report`, `note`
(FYI / Telegram-forwarded / freeform).

**Identity & dedupe.** Server assigns ULID `event_id`. The uniqueness domain is
`(authenticated_source_id, idempotency_key)` (at-least-once sources like agentctl
subscribe are safe; a duplicate POST returns the original id). Sources without native ids get server-computed
`sha256(source,type,payload)[..16]` bucketed to the minute. Ingest ACKs 200 only after
the insert commits: exactly-once processing on top of at-least-once delivery. An
idempotency-key replay with the same canonical payload fingerprint returns the original
result; the same scoped key with a different fingerprint is a visible conflict, never
silently deduped. A transaction reserves the key and inserts its object atomically. The
same registry and insert-or-return-existing rule applies to human inputs, provider
requests, effects, grants, escalation intents, and outbox delivery.

Permission and approval lineage is immutable and per native request, not merely per
session or event type. It includes the authenticated source, native request/tool id,
canonical repo identity, target session, and payload fingerprint. Repo identity comes
from a verified VCS root/remote or an explicit source field; a cwd basename is never
sufficient to scope a grant.

**Session identity.** Canonical key `(vendor, host, native_id)` → CAR mints
`car_session_id`. One CAR session may hold **multiple refs** (`session_refs` table): an
agentctl exec wrapping a codex process contributes both ids; adapters link refs when a
nested native id is learned. Incidents, delivery threads, grants, and provider-context
references hang off `car_session_id`, surviving vendor id churn. `response_channel`
tells the effects layer
which adapter can reach the session; adapters **probe vendor CLI capabilities at
runtime** (cached), never trust docs — e.g. installed codex-cli 0.145.0 has no
`codex queue`; the working path is `codex exec resume <uuid> "<text>"`.

**Routing provenance.** Ingest preserves source origin separately from reply and
notification targets. Any external delivery decision records the origin, explicit
target or chosen primary target, binding id/revision, fallback policy, and routing
rationale. Web-origin work does not fan out to external chat merely because a repo has
a binding. Rebinding never retargets an already-created intent implicitly.

**Ingest adapters (thin normalizers; anything that can `curl -d` is a source):**
- **agentctl**: authenticated callbacks for explicitly subscribed executions;
  plus an opt-in, label-scoped, read-only `recent` observer that compacts local
  execution metadata into the Runs projection. The observer never calls
  `result`/`await` and does not use `recent --unreconciled`, which tracks result
  collection rather than missed delivery.
- **Claude Code**: settings-level `type:"http"` hooks for `Notification`, `Stop`,
  `SessionStart/End`, `PermissionRequest` → `/v1/ingest/claude`. No wrapper scripts.
- **Multica**: webhook → `/v1/ingest/multica`.
- **Generic**: `POST /v1/events` raw envelope; `card emit --type attention.error
  --title "…" ` wraps it for CI/cron.
- **Telegram**: forwarded/plain messages not matching a reply flow become `note` events.

Auth: every ingest, mutation, or provider-control caller has an explicit source
identity and credential, including localhost callers. Only explicitly classified safe
health/read endpoints may be anonymous. Non-local binds additionally require transport
and Host/Origin protections appropriate to the deployment. Credentials never appear
in URLs or routine logs. Process locality alone is not authentication.

## 3. Data model (SQLite, WAL, single file `~/.car/car.db`)

```sql
idempotency(scope, key, payload_sha256, object_type, object_id, created_at,
            PRIMARY KEY(scope,key));
events(id PK, source_id, idempotency_key, payload_sha256, car_session_id NULL,
       type, severity, ts, received_at,
       requires_response, response_channel_json, title, body, payload_json, expires_at,
       actor,             -- 'external' | 'car'  (loop guard: CAR tags events its own actions caused)
       route_state,       -- pending|coalescing|router_resolved|provider_resolved|escalated|expired|skipped
       route_claim_owner NULL, route_claim_token NULL, route_lease_until NULL,
       incident_id NULL, UNIQUE(source_id,idempotency_key));
sessions(car_session_id PK, vendor, host, title, cwd, repo, state, first_seen,
         last_event_at, last_heartbeat_at, expected_heartbeat_s NULL, telegram_thread_id NULL);
session_refs(vendor, host, native_id, car_session_id, PRIMARY KEY(vendor,host,native_id));
incidents(id PK, car_session_id, opened_by_event, state,  -- open|resolved|escalated|snoozed|expired
          snooze_until NULL, summary, telegram_message_id NULL, opened_at, closed_at NULL);
decisions(id PK, incident_id, decided_by,   -- router|provider|human
          disposition,                      -- auto_resolve|keep_informed|escalate|defer
          action_class NULL, action_args_json NULL, rationale, model NULL,
          provider_id NULL, provider_version NULL, provider_request_id NULL,
          tokens_in, tokens_out, cost_usd, created_at);
escalations(id PK, intent_id UNIQUE, incident_id, severity, question, suggested_action_json NULL,
            state,                          -- pending|answered|snoozed|expired|superseded
            telegram_message_id NULL, sent_at, answered_by NULL, answer_json NULL, answered_at NULL);
effects(id PK, intent_id UNIQUE, decision_id, type, args_json, args_sha256,
        lineage_id, provider_policy_verdict NULL,
        safety_verdict, grant_id NULL, dedupe_hash,
        state,                              -- proposed|blocked|pending|running|terminal_recorded
        terminal_outcome NULL,              -- ok|failed|cancelled|expired|uncertain
        claim_owner NULL, claim_token NULL, lease_until NULL,
        started_at, finished_at, result_json NULL);
outcomes(id PK, decision_id, escalation_id NULL, verdict,  -- confirmed|overridden|corrected|flagged
         david_action_json NULL, note NULL, created_at);
human_facts(id PK, interaction_id UNIQUE, kind, actor_id, target_type, target_id,
            body_json, lineage_id NULL, created_at);
            -- feedback|instruction|reply|grant_created|grant_revoked
interactions(id PK, source_id, idempotency_key, payload_sha256, kind,
             target_type, target_id, actor_id, state, -- received|acknowledged|consumed|rejected|expired
             expires_at NULL, terminal_outcome NULL, created_at, updated_at,
             UNIQUE(source_id,idempotency_key));
grants(id PK, intent_id UNIQUE, lineage_id NULL, scope_json, effect_type,
       constraint_json, status, -- active|consumed|rejected|revoked|expired
       uses_remaining NULL, created_by, created_at, expires_at NULL,
       consumed_at NULL, revoked_at NULL, provenance_json);
provider_invocations(id PK, incident_id, provider_id, provider_instance,
                     provider_version, capability, request_id, request_sha256,
                     state,                       -- pending|running|terminal_recorded
                     terminal_outcome NULL,       -- succeeded|failed|cancelled|timed_out|unknown
                     recovery_state NULL,         -- lease_expired|reclaimed|resume_requested|abandoned
                     claim_owner NULL, claim_token NULL, lease_until NULL,
                     started_at, finished_at NULL, response_ref NULL,
                     error_json NULL, UNIQUE(provider_instance,request_id));
outbox(id PK, intent_id, channel, target_json, body_json, state,
       route_json,                       -- origin, binding id/revision, fallback, rationale
       claim_owner NULL, claim_token NULL, lease_until NULL,
       attempts, next_attempt_at, remote_idempotency_key NULL,
       sent_message_id NULL, UNIQUE(intent_id,channel));
digests(day PK, rendered_md, sent_at NULL, outbox_id NULL,
        held_outbox_ids_json NOT NULL DEFAULT '[]');
audit(id PK, ts, actor, verb, object_type, object_id, detail_json);  -- append-only; EVERYTHING writes here
spend(day, provider, model, calls, tokens_in, tokens_out, cost_usd, PRIMARY KEY(day,provider,model));
```

Provider-specific memory and policy state does not add tables to the core schema. It
lives beneath a resolved instance and continuity key:
`~/.car/providers/<provider-id>/instances/<instance-id>/<continuity-key>/state/`.
Core stores the human facts needed for authority and reconstruction: outcomes, grants,
provider invocation provenance, effect proposals, safety verdicts, and results.

`human_facts.kind` and `interactions.state` are closed vocabularies. Surfaces write
human instructions, feedback, replies, and grant facts through these core contracts;
they are not external `car.event.v1` lifecycle events. Interaction replay may repeat an
acknowledgement but cannot create a second fact, consume a grant twice, or rerun an
effect. `/remember`, feedback, replies, and grant/revoke actions atomically reserve the
central idempotency key, insert-or-return the interaction and its unique human fact,
and apply any grant mutation in one transaction. Provider observation uses the stable
human-fact id as its own idempotency input.

Outbox state is closed: `pending`, `sending`, `delivered`, `uncertain`, `failed`,
`deferred`, `expired`, `superseded`, `abandoned`, `suppressed`, or `no_target`.

Claims atomically write an owner, opaque claim token, and expiry. Renew, complete, and
terminal recording compare the token; a stale worker cannot finish work after a
replacement has reclaimed it. Reclaim is itself durable recovery evidence. This rule
applies to router events, provider invocations, effects, and outbox delivery.

Effect claim is also the final authorization boundary. In the same SQLite transaction
that moves `pending -> running` and consumes bounded grant authority, core revalidates
panic, grant status and exact constraints, expiry/deadline, dangerous arguments,
dedupe, circuit breaker, rolling rate, and rolling spend. A rail failure blocks the
effect without consuming the grant. Operator-reported usage cost is conservatively
partitioned once across all effects from that decision, so multiple proposals neither
erase nor multiply one reported charge.

## 4. Router daemon architecture

One Bun process (`card serve`) runs durable loops over the shared core store and is
supervised by launchd/systemd KeepAlive:

1. **HTTP** (Hono, `127.0.0.1:7171`): ingest routes, web UI, `/brief.md`, `/healthz`,
   `/v1/schema`. A Claude `PermissionRequest` may park the HTTP response until its
   deadline while a core grant, provider proposal, or human tap races to answer it. On
   deadline CAR returns no decision so Claude falls back to its local prompt.
2. **Channels**: Telegram initially; Discord later. Buttons and replies write durable
   human input rows. Surfaces do not own incident, provider, grant, or delivery state.
3. **Router worker**: claims pending events, applies deterministic routes, coalesces
   related attention into incidents, invokes selected providers when needed, and turns
   proposals into core decisions and effects.
4. **Scheduler**: daily digest, snooze and grant expiry, retention, provider health,
   and the silence watchdog. A provider may run its own consolidation, but provider
   maintenance cannot block router deadlines.
5. **Outbox/effect workers**: deliver messages and execute authorized effects with
   leases, bounded retries, idempotency, and explicit terminal states.

Authoritative work lives in SQLite. Parked HTTP response handles and provider process
handles are necessarily in memory, but their loss has a defined degraded path: the
source falls back locally, the incident remains durable, and CAR records that in-band
delivery was lost.

Every worker replays eligible rows from the canonical store at startup. Compatibility
files, cached projections, process handles, and surface state never gate whether core
work drains. Long-lived provider runtimes have one scope-configured lifecycle owner;
Telegram chats, web tabs, sessions, incidents, and other surface routing keys cannot
own or key a provider process.

Provider and daemon health is semantic, not just PID liveness. CAR distinguishes
starting, ready, making progress, stale-alive, disconnected, timed out, intentionally
stopped, reaped, and crashed. Termination origin and recovery decisions are typed,
durable evidence.

Production qualification uses an operator-configured dead-man webhook outside the CAR
host and CAR delivery credentials. Once per minute, CAR sends its daemon instance id,
monotonic heartbeat sequence, last durable progress timestamp, and last digest receipt.
The observer alerts through an independently configured destination after three missed
heartbeats. launchd/systemd remains the local restart owner; it is not the external
observer. Deployments without this observer may run, but cannot claim that silence is
unambiguous.

## 5. Routing and providers

**Deterministic routing comes first; provider judgment is optional.** The pipeline for
an event or coalesced incident is:

1. Authenticate, normalize, validate, and persist.
2. Apply routes that providers cannot delay or override:
   - expired events become expired and perform no external effect;
   - `severity=urgent` escalates immediately;
   - self-caused events attach to their originating incident and cannot create loops;
   - routine lifecycle/progress stays available for status and digest;
   - a matching active human grant may authorize a typed effect after safety checks.
3. Coalesce related events into one incident using explicit identity and lineage rules.
4. Resolve the configured provider instance for each required capability.
5. Ask the memory provider for bounded context, if configured.
6. Ask the operator provider for a disposition and typed effect proposals, if needed.
7. Ask the policy provider to evaluate proposed effects, if configured.
8. Pass every effect through the core safety kernel and grant ledger.
9. Persist the decision and effect before execution. Execute only through core effect
   adapters. Provider failure, timeout, invalid output, or missing capability escalates.

For provider invocations and effects, durable terminal recording precedes optional
side effects such as delivery, provider-memory observation, timeline rendering, or
cleanup. Replaying the same terminal outcome is idempotent; a conflicting terminal
outcome keeps the first fact and emits conflict evidence. Optional side effects retry
from their own durable intents and cannot keep completed work in `running`.

Provider invocation lifecycle is closed: `pending -> running -> terminal_recorded`.
Only a supported public-protocol terminal result may record `succeeded`, `failed`, or
`cancelled`. A reached deadline records `timed_out`; loss of transport after the
provider may have completed records `unknown`, never inferred success. Lease expiry and
reclaim are separate recovery evidence. Reusing the same request and terminal outcome
is idempotent; a different terminal outcome preserves the first fact and emits a
conflict. Recovery reuses the same request id when the provider supports idempotent
resume; otherwise it records `unknown` and escalates rather than starting duplicate
work.

Escalations, notifications, effects, and channel deliveries use stable intent ids
derived from immutable domain identity, not provider output text, retry counters,
restart attempts, or mutable projections. Each channel keeps its own delivery ledger so
a retry cannot spam and a distinct intent cannot be accidentally suppressed.

### Capability-provider contract

The contracts are separate even when one bundle implements all of them:

```ts
interface OperatorProvider {
  decide(input: IncidentPacket): Promise<ProviderDecision>;
}

interface PolicyProvider {
  evaluate(input: EffectProposalPacket): Promise<PolicyAdvice>;
}

interface MemoryProvider {
  context(input: ContextQuery): Promise<ContextBundle>;
  observe(input: HumanOrDecisionOutcome): Promise<void>;
}
```

The exact wire types live in a versioned provider schema. Requests carry an idempotent
request id, deadline, provider instance id, capability, incident snapshot reference,
and bounded inline context. Responses are closed typed objects, not prose that core
must reinterpret. Provider discovery and health checks have no side effects.

Version compatibility is exact-match and fail-closed. `operator`, `policy`, and
`memory` are independent canonical capabilities; unknown names reject registration and
unsupported calls return typed errors. One schema definition drives config parsing,
validation, doctor output, and UI metadata so those views cannot drift. Registration is
not readiness: preflight and semantic health must pass before routing.

Provider event semantics are closed and typed: `started`, `heartbeat`,
`progress_delta`, `progress_snapshot`, `artifact`, `final_answer`,
`terminal_result`, and `recovery_state`. Providers may omit unsupported optional event
kinds but cannot relabel snapshots as deltas, transcript content as terminal evidence,
or silence as success. Core performs semantic dedupe and owns the canonical timeline.

Interactive provider requests and channel buttons use a durable interaction ledger:
interaction id, acknowledgement state, replay disposition, target incident/effect,
issuer, expiry, and terminal outcome. A transport retry may replay an acknowledgement;
it may not rerun provider judgment or effect business logic.

An operator may request bounded reads or probes through CAR, but those requests are
themselves typed effects. It may never spawn a command, call a vendor, deliver a reply,
or edit core state directly. A policy provider supplies advice; it cannot waive a core
rail or mint a human grant. A memory provider supplies context and accepts observations;
its state is never the source of truth for grants, incident resolution, or delivery.

### Provider selection and continuity

Provider topology is explicit user configuration rather than a CAR opinion. Selection
may resolve by capability and scope, allowing one global Hermes, different Hermes
profiles per repo/host/policy domain, incident-isolated Hermes turns, the native
provider, or another future arrangement after the provider ecosystem expands. The
initial release may only recombine native and Hermes instances across the three
capability slots; it does not install or activate a third provider implementation.

Conceptually:

```toml
[providers.defaults]
operator = "native"
policy = "native"
memory = "native"

[[providers.routes]]
match = { repo = "github.com/acme/*" }
operator = "hermes:work"
policy = "hermes:work"
memory = "hermes:work"

[providers.instances."hermes:work"]
adapter = "hermes"
profile = "work"
continuity = "global" # provider-specific: global, scoped, or incident
```

CAR validates the resolved instance before routing, records it on every invocation, and
never silently falls back to a different intelligent provider. An unavailable provider
falls back to deterministic human escalation. Selection rules must be ordered,
inspectable, and testable with `card doctor`.

Routes use explicit first-match order; ambiguous duplicate selectors are invalid rather
than resolved by hidden specificity. After selection, CAR derives a continuity key from
the configured mode (`global`, explicit scope tuple, or incident id) and records the
selector, resolved instance, continuity key, state root, and config fingerprint. Two
instances or continuity keys never share provider state unless the user explicitly
configures the same identity and root. The filesystem component is a stable encoded
hash; the readable scope tuple remains in core provenance rather than leaking repo or
host names into an unsafe path.

### Shipped providers

**Native** is the dependency-free reference provider. It runs without an external
agent runtime, service, or API key. It supplies conservative deterministic operation,
explicit rules, and a small inspectable scoped memory. It proves the provider contract
without making CAR's router depend on optional intelligence.

**Hermes** is the first non-native provider. The adapter uses Hermes only through its
supported agentctl/ACP lifecycle. Hermes owns its native session history; CAR owns the
incident packet it sent, the provider identity/topology it selected, the structured
response it accepted, and every resulting effect. CAR never reads Hermes internals or
assumes one required continuity topology. Only a terminal event from the public
protocol establishes completion; transcript snapshots or provider-private files cannot
turn a timeout or unknown outcome into success.

Provider descriptors and resolved executables carry immutable provenance: provider and
protocol versions, configured binary identity, argv, profile/instance, state root, and
configuration fingerprint. Process records also include owner PID/PGID and command
identity. CAR verifies identity before attach or termination; stale or reused PID
metadata is discarded without signaling an unrelated process.

The provider API is public and versioned. The initial target supports these two paths
only; marketplace,
third-party installation UX, voting, and ensembles wait until both have been dogfooded.

## 6. Safety, grants, and provider memory

### Core safety kernel

The kernel is deliberately smaller than a policy engine and cannot be replaced by a
provider. It enforces:

- effect-schema and argument validation;
- expiry and stale-context checks immediately before execution;
- action dedupe and per-class/session limits;
- circuit breaker, panic, and budget stops;
- self-event suppression and incident-lineage limits;
- active human grants and their exact scope;
- built-in irreversible-action rails plus operator-configured stricter rails;
- effect idempotency, leases, timeout, and terminal result recording.

The effect lifecycle is closed and monotonic:

```text
proposed -> blocked
proposed -> pending -> running -> terminal_recorded(ok|failed|cancelled|expired|uncertain)
```

Only core performs these transitions. An uncertain terminal outcome, including a crash
after a remote send but before its acknowledgement is recorded, is not automatically
retried as though nothing happened. CAR first uses remote idempotency or reconciliation
when available; otherwise it exposes "possibly delivered" and escalates rather than
risk an invisible duplicate effect.

Rails match the canonical effect type and normalized, schema-validated arguments at
execution time, never provider prose, event wording, or a cached earlier verdict. The
initial irreversible rail covers force push, `reset --hard`, recursive forced
deletion, sudo, pipe-to-shell, PR merge, package publish, Terraform apply/destroy,
`kubectl delete`, destructive database operations, production mutations, and credential
paths. The rail escalates rather than silently denies. Providers may make this list
stricter; they cannot weaken it.

### Full grant-based autonomy

Every supported effect class is eligible for a reusable or one-shot grant, but no grant
can bypass the core rails above. Execution always has a distinct immutable request
lineage and requires a grant covering the canonical effect type, normalized argument
fingerprint, and current resolved scope. A one-shot grant must also cover that exact
lineage. A reusable grant may explicitly omit grant-level lineage to authorize future
requests inside its otherwise exact scope; absence is stored and reviewed as
cross-request authority, never inferred as a wildcard. Grants are typed core records
with scope, constraints, provenance, status, and expiry. Scope may include provider
instance, vendor, host, verified repo, session, event type, effect type, template, and
argument constraints.

One-shot approvals are grants with a target effect/request, exact lineage, expiry, and
one remaining use. They become `consumed`, `rejected`, or `expired` independently of
whether a chat prompt was successfully rendered. Reusable autonomy grants use the same
authority ledger with explicit cross-request scope and exact effect/argument matching;
UI prompt state is never authorization truth.

Providers may infer preferences, raise suggestion confidence, and propose a grant.
Only a human action creates, broadens, or renews one. A provider override or negative
feedback is recorded and delivered to the selected memory/policy providers, but each
provider decides how to update its learned model. Core alone decides whether a grant is
still active. Revocation is immediate and does not depend on provider availability.

First autonomous use, unusual use near a scope boundary, and every blocked granted use
remain visible in the digest and audit. `/panic` bypasses providers and disables all
autonomous effects until explicitly cleared.

### Provider memory

Human instructions, decisions, feedback, and grants are durable core facts. A memory
provider receives those facts through an observation contract and may build notes,
rules, episodes, embeddings, profiles, or durable agent sessions in its own state.

The native provider uses a small inspectable scoped store and export. Hermes may use
one global memory, multiple profiles, scoped sessions, or incident-isolated turns as
the user configures. No provider may silently merge scopes, and CAR's incident detail
must show which provider instance and context references influenced a decision.

Telegram and web surfaces write generic `human.feedback`, `human.instruction`,
`grant.created`, and `grant.revoked` facts. They do not call a particular provider's
memory API directly. Provider observation failure is retryable and visible but does
not roll back the human action.

## 7. Telegram UX

grammY. Forum-supergroup mode: one topic per session; flat-chat fallback: one anchor
message per session, all traffic replies to it.

Telegram is an authenticated operator surface, not an anonymous chat. The configured
`telegram.chat_id` selects the conversation and non-empty `telegram.allowed_user_ids`
select the human actors; a chat id never substitutes for a user identity. The bot
boundary and row-only handlers both reject missing or unauthorized actors, including
read-only commands. Escalation-card callbacks that can answer, snooze, edit, or create
authority additionally require a pending escalation and an exact durable binding for
the Telegram message (the escalation's message id plus its persisted message map).
Stale cards are rejected and audited rather than reapplying lifecycle state.
The transport receipt is canonical and includes the remote message/thread identifiers;
callback, thread, anchor, ticker, escalation, and incident fields are deterministic
projections replayed from that receipt. A crash after receipt commit therefore repairs
the controls without sending the Telegram message again.

**Escalate:**
```
🔴 needs you · claude-code · omi-desktop @ mac-studio
Agent asks: force-push to fix/telemetry-cliff? Remote diverged.
CAR probed: `git status` — remote has 2 CI-authored commits.
Memory: you've denied force-push twice on this repo.
Suggests: DENY — tell agent to rebase instead.
[✅ Approve] [❌ Deny] [💬 Reply] [😴 ▾] [🧠 Always…]
```
✅/❌ execute via reply-back, edit the message in place to the resolution, and record
the core outcome (the tap is also delivered to configured providers as feedback). 💬 or any plain reply to the
anchor/topic routes verbatim to the agent — the v2 reply inbox, generalized. 😴 snooze
(1h / tonight / next digest), resurfaces bold in digest. Quiet hours: only `urgent`
breaks through.

**Keep-informed:** no push. One pinned, edited-in-place status line per active session.
Optional `/ticker on`.

**Auto-resolved:** normally digest-only, except first-use-after-grant and safety-boundary
notifications. The incident detail always names the provider instance, grant, safety
verdict, effect, and delivery result.

**Daily digest (unconditional):**

The archive row is written before enqueue, but `sent_at` is delivery truth: it is
populated only after the canonical digest outbox row records a `delivered` receipt.
Held alert rows remain `deferred` until that receipt; `uncertain` and `failed`
outcomes remain visible for explicit reconciliation and never silently fold alerts.

```
☀️ CAR digest — Tue Aug 26
🤖 Handled (3): approved dep bump ×2 [👍/👎] · restarted forgejo [👍/👎]
🙋 You resolved (2): denied force-push · answered hermes planning q
⚠️ Stuck/silent: multica autopilot #12 — no heartbeat 26h [🔍 probe] [escalate]
💸 Spend: providers $0.41 (34 calls) · agents ~$12.30 (reported)
🧠 Providers: native healthy · Hermes/work healthy · 1 grant proposal [review]
```

Commands: `/status`, `/digest`, `/remember <text>`, `/mute <session> <dur>`,
`/providers`, `/grants`, `/panic` (escalate-only mode + cancel in-flight effects),
`/ticker`. `/remember` records a human instruction in core and delivers it to the
selected memory provider; it does not write provider-specific state from the surface.

## 8. Reply-back adapters

One interface: `deliver(session, intent_id, payload: {text}|{approval:boolean}) →`
one of the closed outbox outcomes above, dispatched on `response_channel.kind`,
capability-probed at runtime, every attempt and receipt audited. A crash or timeout
after the remote side may have accepted a send becomes `uncertain`; it is reconciled
rather than blindly retried. Definitive failure re-escalates, never drops. `expired`
and `superseded` are visible terminal outcomes, not successful delivery.

- **claude-hook-http**: answer the parked `PermissionRequest` response in-band —
  race-free, verified.
- **claude-resume**: `claude -p --resume <native_id> "<text>"` in the session's cwd
  (headless/ended sessions). Live interactive terminal session: no safe injection —
  stage the file reply AND say so honestly in-thread ("reply staged; paste or it's
  picked up next hook fire"); a shipped `UserPromptSubmit` hook snippet slurps staged
  replies into context.
- **codex-exec-resume**: `codex exec resume <uuid> "<text>"` (verified against
  codex-cli 0.145.0; there is no `codex queue` in the installed CLI).
- **agentctl-run**: a reply to agentctl-launched work is a new bounded execution
  (`agentctl run --label car-continuation --label car-observe -- <vendor resume argv>` from
  `session_refs`); CAR immediately `subscribe create`s on it.
- **multica-api**: comment/approve on the card via the self-hosted REST API.
- **file (universal fallback)**: `~/.car/replies/<car_session_id>/reply-<seq>.md` —
  v2's `tickets/replies.py` inbox generalized and documented in the public contract;
  consumed replies archived to `reply_history/`.

## 9. Web UI

Hono server-rendered JSX + htmx. No build step, no SPA. Localhost bind (tailnet
exposure via tailscale serve is deliberately out of scope). Stateless views over
SQLite. It is a power-user viewport onto the same tables the bot uses — no chat.

v1 pages: **Inbox** (event stream; filters vendor/repo/severity/state; FTS),
**Incident detail** (full context → decision → outcome → audit rows; the "why did CAR
do that" page, including provider/grant/safety/effect provenance), **Providers**
(resolved capability routes, health, versions, instance topology, and provider-owned
memory links or exports), **Grants** (core-owned create, constrain, revoke, and audit),
**Safety** (core rails and current breaker/budget state; provider policy shown
separately),
**Digest archive**, **`GET /brief.md`** (open escalations + stuck sessions +
yesterday's digest as markdown — for other agents to curl).

## 10. Implemented rewrite boundary

The production composition in this PR implements the accepted boundary:

1. `car.event.v1`, provider, effect, grant, interaction, outbox, and terminal lifecycle
   contracts are typed and persisted in the core database.
2. The core safety kernel owns grants, panic, dangerous-content rails, numeric budgets,
   idempotency, and fenced effect execution. Provider policy advice has no authority.
3. The provider host resolves explicit global/scoped/incident topology, owns one runtime
   identity per resolved scope, records readiness/health, and persists invocation and
   ACP-session continuity evidence.
4. Native is the no-dependency default operator/policy/memory provider. Hermes is the
   first non-native provider through its public ACP server and public `-p <profile>`
   selection; CAR does not read Hermes-private state.
5. The daemon runs the attention router, not the legacy single-brain triage loop.
   Telegram writes core grants and human facts; learned memory remains non-authoritative.
6. Canonical outbox claims distinguish known transport rejection (safe retry) from the
   remote-send crash window (`uncertain`, never blind retry). One daemon lease owns each
   state store, and optional dead-man evidence is sent to an independent observer.

The remaining legacy policy/memory code used by current web/digest views is a
compatibility projection only. It is not consulted for routing, provider selection,
grant creation, safety authorization, or effect execution and can be removed as those
views move to provider/core projections.

Migration is one-way and auditable: import/replay into the one v3 database, verify
readiness, then archive v2 state. There is no steady-state dual writer, bidirectional
sync, or legacy fallback that can create two lifecycle authorities.

`card migration-audit --v2-root <quiesced-root>` produces the immutable
`car.v2-cutover-report.v1` artifact used at this gate. It hashes the source inventory,
inspects SQLite lifecycle columns, fails closed on active rows or live journal evidence,
and preserves unclassified nonempty tables for human review. It deliberately does not
synthesize v3 facts from ambiguous v2 rows; an explicit import must cite the report,
and the audit must be rerun against the final drained/imported source tree before
read-only archival.

The cutover gate is criterion-based, not time-based:

- all active source adapters write v3 only, and every active v2 item is drained or
  represented in an import report with unknown/contradictory facts preserved;
- native no-dependency and Hermes modes pass shared contract, restart, timeout, and
  scope-isolation tests;
- grant/safety, stale-claim, idempotency-race, interaction-replay, and
  uncertain-delivery fault tests pass;
- all localhost write/provider-control paths reject unauthenticated callers; verified
  VCS identity cannot be substituted by cwd basename; distinct native permission
  requests retain distinct immutable lineages; and canonical dangerous-content rails
  block protected arguments even under a matching grant;
- the external dead-man observer and independent alert destination are proven;
- rollback is an immutable v2 state archive/export, never a second live writer.

When the gate passes, v3 takes the `car` name and v2 state becomes read-only. The next
removal release deletes the v2 runtime while preserving the documented archive/export.

**Tests:** golden wire fixtures per source; `:memory:` core SQLite; scripted provider
fakes that assert typed proposals rather than prose; contract tests shared by native
and Hermes; provider timeout/crash/version/scope-mismatch tests; over-tested safety and
grant boundaries; e2e smoke that proves native no-dependency routing and Hermes
adapter mode, reply delivery truth, audit completeness, and rendered digest.

## 11. Cut order (if scope must shrink) & risks

Cut first → last: Discord; rich provider-memory editing; marketplace/third-party
installation; mutating provider-requested templates; Multica-specific behavior beyond
the generic webhook; sophisticated native-provider consolidation. **Never cut:**
ingest durability, native no-dependency fallback routing, escalation with reply-back, core
grants and safety, unconditional digest, watchdog, delivery truth, and audit.

Risks: (1) reply-back into live interactive sessions is structurally weak across all
vendors — mitigate with first-class file fallback and honest staged/delivered states;
(2) A3 full autonomy increases the cost of a bad scope match — mitigate with core-owned
typed grants, exact argument constraints, pre-execution rechecks, irreversible rails,
and immediate panic/revoke; (3) provider continuity can leak context across scopes —
make topology explicit, show the resolved instance, and forbid silent scope merging;
(4) provider and vendor drift — use exact protocol versions, runtime capability probes,
and fail-loud health; (5) the digest cannot prove the daemon itself is alive — add an
independently observed dead-man signal before claiming silence is unambiguous; (6)
surface complexity — buttons write core facts only, while web remains a stateless
overflow and explanation surface.

Retention and backpressure are explicit operational policy. Core assigns byte, age,
and count budgets to event payloads, provider context, outbox, and audit. Cleanup never
removes unresolved incidents, active grants, nonterminal claims/effects, or evidence
needed to explain provider decisions. When safety of cleanup or admission cannot be
proven, CAR fails closed and surfaces the blocked reason instead of dropping work.
