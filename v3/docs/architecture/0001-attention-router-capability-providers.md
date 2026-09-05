# ADR 0001: CAR v3 is an attention router with capability providers

- **Status:** Accepted
- **Date:** 2026-08-27
- **Decision owners:** David and the CAR maintainers
- **Applies to:** CAR v3

Historical evidence for the invariants adopted here is indexed in
[`HISTORICAL_SCARS.md`](./HISTORICAL_SCARS.md).

## Context

Vendor runtimes increasingly own agent execution, durable sessions, queueing, and
resume. CAR cannot create a durable advantage by rebuilding those features for every
vendor. The cross-vendor problem remains unsolved: attention arrives through unrelated
hooks, CLIs, chats, webhooks, and files; replies have different delivery semantics; and
no vendor can be the neutral authority for what needs the operator across all of them.

The first v3 implementation combined three different products inside one daemon:

1. an attention router that ingests, normalizes, groups, delivers, and audits;
2. an autonomous operator that investigates incidents and proposes or performs work;
3. a learning policy and memory engine that tries to model operator preferences.

That coupling made the product boundary unclear and made CAR's correctness depend on a
particular LLM loop and memory model. It also prevented Hermes or another external
agent from supplying the intelligence while CAR remained the durable neutral router.

## Decision

**CAR v3 is, at its core, a durable cross-vendor attention router.** Autonomous
operation, policy judgment, and learning are replaceable capabilities supplied by
providers. A provider bundle may implement any or all of those capabilities, but CAR
interacts with them through separate versioned contracts.

```text
agents / CI / cron
        |
        | car.event.v1
        v
+----------------------- CAR router core ------------------------+
| ingest -> normalize -> incidents -> route -> effects -> outbox |
| sessions, deadlines, grants, audit, digest, watchdog, safety   |
+-------------------------------+--------------------------------+
                                | incident context / proposals
                  +-------------+-------------+
                  v             v             v
               operator       policy        memory
               provider       provider      provider
                  +-------------+-------------+
                         one bundle or several
```

### Router-core ownership

CAR core owns the behavior that must remain correct when every provider is absent,
unhealthy, slow, or wrong:

| Area | Core responsibility |
| --- | --- |
| Ingest | Authenticate, validate, normalize, persist, and deduplicate events. |
| Identity | Own CAR session, incident, escalation, and effect identity and lifecycle. |
| Routing | Apply deterministic urgent, expiry, response-required, and fallback routes. |
| Human handoff | Persist questions, replies, outcomes, snoozes, and explicit grants. |
| Delivery | Deliver replies and approvals through adapters and distinguish delivered, uncertain, failed, expired, suppressed, and other closed receipt outcomes. |
| Effects | Validate, authorize, execute, and audit typed effect proposals. Providers never execute directly. |
| Safety | Enforce non-bypassable rails, grants, dedupe, rate limits, budgets, circuit breakers, and panic mode. |
| Operations | Own leases, recovery, outbox retries, retention, digest, watchdog, health, and audit. |

The router remains useful without intelligence: informational events remain available
for status and digest, urgent and response-required events escalate, human replies are
delivered, and failures stay visible.

### Provider capabilities

Providers advertise capabilities independently:

- **Operator:** inspect an incident and propose a disposition and zero or more typed
  effects. It may request bounded context or probes through CAR, but it receives no raw
  executor.
- **Policy:** evaluate a proposed effect using preferences and contextual judgment. Its
  verdict is an input to the core safety kernel, never a way around it.
- **Memory:** retrieve scoped context and consume human instructions, decisions, and
  feedback. Provider memory is helpful context, not authoritative router state.

CAR selects one provider for each capability. One bundle may fill all three slots. v1
does not introduce provider voting, ensembles, or implicit failover between intelligent
providers. A missing, failed, incompatible, or timed-out provider falls back to the
deterministic router path, normally escalation.

### Typed effects, not provider execution

An operator can propose only closed, versioned effect types such as:

- `escalate`, `notify`, `defer`;
- `reply`, `approve`, `deny`;
- `probe`, `run_template`.

CAR resolves provider proposals into core decision and effect rows before doing
anything externally visible. The core safety kernel checks the effect, the selected
policy provider contributes a verdict when configured, and the core executor performs
the effect. Every attempt and result is auditable.

This separation is mandatory even for an in-process provider. A provider cannot retain
an executor reference, write core decision state directly, or convert a suggestion into
an action by mutating its own memory.

### Full grant-based autonomy

CAR v3 supports the full grant model from its first dogfood release: any supported
effect class is eligible for autonomy when an explicit human grant covers it. This does
not make any effect argument executable: every effect retains a distinct immutable
request lineage, and non-bypassable core rails still evaluate the canonical effect
type, normalized arguments, and current scope immediately before execution. One-shot
grants bind the exact lineage. A reusable grant may explicitly carry no grant-level
lineage, which means reviewed cross-request authority within its otherwise exact scope;
missing lineage is never silently inferred into broader authority.

The authority boundary remains fixed:

- providers may learn preferences and propose grants;
- only an explicit human action creates or broadens a grant;
- CAR core stores and enforces the grant;
- provider confidence never becomes authorization;
- core safety rails may still block an effect covered by a grant;
- overrides and revocations are durable human outcomes delivered back to providers.

Whether one override automatically demotes a learned rule is provider behavior.
Whether a grant remains valid is core behavior.

### Provider state and topology

Core stores events, sessions, incidents, decisions, outcomes, grants, effects, outbox
delivery, and audit. Providers own implementation-specific state beneath:

```text
~/.car/providers/<provider-id>/instances/<instance-id>/<continuity-key>/state/
```

Core records the provider id, provider version, capability, request id, and response
reference on every provider-influenced decision. Provider state must not add tables to
the core schema or become necessary to reconstruct what CAR did.

Provider continuity is user-configurable. CAR does not impose one Hermes memory or
session topology. A user may choose, for example:

- one global Hermes instance or profile;
- different Hermes profiles by repo, host, source, or policy domain;
- incident-isolated Hermes turns;
- a different provider arrangement in a future protocol release.

Selection is explicit, ordered configuration. First match wins; ambiguous duplicate
selectors are invalid. CAR records the selector, resolved provider instance,
continuity key, state root, and configuration fingerprint for each invocation. Two
instances or continuity keys never share provider state unless the user explicitly
configures the same identity and root; providers must not silently merge scopes.

In the initial release, this freedom means assigning native and Hermes instances to the
three capability slots in any explicit topology. It does not mean installing or
activating a third provider implementation; that ecosystem boundary remains deferred.

### Shipped providers

v3 initially supports exactly two provider paths:

1. **Native provider:** ships with CAR, needs no external agent runtime, service, or API
   key, and supplies conservative deterministic operation, explicit policy rules, and a
   small inspectable memory system. It is the no-external-provider-dependency baseline
   and reference contract implementation; it still uses CAR's normal Bun package.
2. **Hermes provider:** the first non-native provider. It may supply operator, policy,
   and memory capabilities and is reached through the supported agentctl/ACP lifecycle,
   not unmanaged background processes or reads of Hermes internal state.

The provider protocol is public and versioned, but v1 does not promise a third-party
marketplace or arbitrary provider installer. We will stabilize the contract through the
native and Hermes implementations first.

### Provider protocol principles

The provider contract is semantic and transport-neutral. Every provider must support:

- exact API-version negotiation;
- capability discovery and health/preflight diagnostics;
- stable provider and instance identity;
- bounded request deadlines and cancellation;
- idempotent request identity;
- structured responses and typed failures;
- provenance sufficient to reproduce the selected provider configuration;
- no import-time or discovery-time side effects.

Capability names are canonical and closed for each protocol version. Unknown
capabilities reject registration; unsupported calls fail explicitly. One schema source
drives configuration parsing, validation, diagnostics, and surface metadata. Provider
registration is not provider readiness: bounded preflight and semantic health must
pass before routing.

For executable providers, provenance includes the resolved binary identity/version,
argv, provider instance/profile, state root, trust source, and configuration fingerprint.
CAR verifies process ownership and command identity before attach or termination and
does not infer completion from provider-private files or transcript snapshots. Only a
terminal result from the supported public protocol establishes provider completion.

The native implementation may run in-process. Non-native agent providers use agentctl
and their supported public protocol. Future command or HTTP transports may implement
the same contract without changing router semantics.

### v2 lifecycle (superseded by ADR 0003)

This PR has no deployed v3 users and requires no migration. V3 has its own clean
bootstrap and keeps `card` distinct from the untouched v2/Python runtime. Do not
build a cutover/import bridge or mutate v2 as a prerequisite for this PR. The former
migration assumptions in this section are superseded by ADR 0003. Native integration
support is not an excuse to maintain two writers for the same lifecycle.

## Consequences

- The current built-in LLM triage loop moves behind the operator-provider contract.
- The current memory implementation becomes provider-owned state for the native
  provider rather than part of the router's universal schema.
- The current policy module splits into provider judgment and a smaller non-pluggable
  safety kernel.
- Telegram and web surfaces record generic human feedback, instructions, grants, and
  revocations. They do not call a particular memory implementation.
- Reply-back adapters, effect execution, incident persistence, outbox, digest,
  watchdog, and audit remain core.
- Provider failure increases human attention but cannot make CAR lose an event, invent
  authorization, or silently claim delivery.

## Rejected alternatives

- **One built-in CAR brain:** couples router correctness and product identity to one
  operator, policy, and memory implementation.
- **Providers execute their own actions:** creates inconsistent safety and audit paths
  and lets provider memory become authority.
- **Policy fully pluggable:** allows a provider defect to bypass the very boundary the
  policy system is meant to defend.
- **Permanent v2/v3 sibling products:** preserves two identities and splits investment
  across a commoditized runner and the new attention-router direction.
- **Marketplace in v1:** freezes installation and compatibility contracts before the
  native and Hermes providers have dogfooded them.
