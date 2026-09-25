# Security & Privacy Posture

## CAR v3: active posture

CAR v3 accepts untrusted event content and may ask capability providers to propose
autonomous effects. Full grant-based autonomy is supported, but authority is explicit:
a provider proposal is not executable until the core safety kernel validates it and an
active human grant covers it.

### Threat model

- Primary risk is accidental or manipulated blast radius: wrong session, repo, host,
  provider profile, grant scope, or effect arguments.
- Event bodies, transcripts, provider memory, and vendor payloads may contain prompt
  injection or stale claims. They are context, never authorization.
- Provider continuity may leak sensitive context across repos or policy domains if the
  configured topology silently merges scopes.
- Credentials or private source content may leak through provider requests, audit,
  logs, digests, or reply fallbacks.
- Localhost is a network boundary, not an identity boundary. Local callers must not be
  assumed trustworthy merely because they can reach `127.0.0.1`.

### Structural mitigations

- Providers propose typed effects and never receive a raw executor.
- Core owns grants, irreversible-action rails, expiry rechecks, dedupe, budgets,
  breaker/panic, execution, and audit.
- Every effect has immutable request lineage. Grant and rail matching uses the
  canonical effect type, normalized validated arguments, and verified scope
  immediately before execution. One-shot grants also match exact lineage; a reusable
  grant can omit grant-level lineage only as explicit reviewed cross-request authority.
  Provider/event prose and cwd basenames are never authority.
- Provider selection and continuity topology are explicit, inspectable, and recorded on
  every invocation; CAR does not silently merge scopes or swap intelligent providers.
- Credentials are capability-scoped and loaded centrally. Provider requests receive
  only the bounded material required for the selected capability.
- Redaction occurs before durable audit, provider dispatch, digest rendering, or file
  fallback. Raw secret-bearing payloads are not copied merely for debugging convenience.
- Local ingest and mutation endpoints require an explicit authentication and
  authorization posture before CAR claims multi-user or hostile-local-process safety.

### Privacy rules

- Core stores compact facts and references rather than unbounded transcripts.
- Provider-owned memory must be independently inspectable, exportable, and removable
  without corrupting core incident history.
- Human instructions and grants record their scope and provider recipients.
- Provider observation failure is visible and retryable; it does not broaden what is
  sent on retry or roll back the human action.

## Deprecated CAR v2 posture

The remainder of this document describes the deprecated runner/ticket product. Its
permissive default does not apply to v3 autonomous effects.

CAR v2 defaults to **YOLO** (full permissions) under an assumed isolated-workspace
model. This section encodes safe structural practices without adding runtime friction.

## Threat model (pragmatic)
- Primary risk is accidental blast radius (wrong repo, wrong workspace, wrong secrets).
- Secondary risk is credential leakage via logs/artifacts.
- Runtime permissions are permissive by default; mitigation is environment scoping + auditability.

## Structural mitigations
- Prefer isolated workspaces (worktrees/containers) and cheap reset/recreate.
- Scope credentials to the minimum needed for the task.
- Keep secrets out of logs and durable artifacts by default.
- Centralize where secrets are loaded so audit is possible.

## Optional safety postures (opt-in)
Examples of knobs a deployment may enable:
- no-network mode
- dry-run mode
- command allow/deny lists
- “no git push” guardrails
- write restrictions outside workspace root

## Privacy rules
- Treat run artifacts as potentially shareable; redact secrets at source.
- Avoid copying full sensitive documents into logs; store references and hashes where possible.
