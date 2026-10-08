# ADR 0002: Grounded decisions with a deterministic core

Status: implemented; extends ADR 0001. ADR 0003 adds clean-install, receipt, expiry-review and UI contracts.

## Decision

CAR owns the human–agent handoff, not the task planner or execution harness. Calling agents report the goal, blocker and specific decision. The deterministic core requests missing context, preserves outstanding obligations, captures a human answer, and records delivery and source clearance separately. A bounded optional preparation reviewer supplies advisory questions/recommendations. It cannot answer, authorize, close, defer indefinitely, spawn arbitrary work, or change a surfaced packet.

This is not a replacement for the existing operator/policy/memory provider architecture. Native events continue through that pipeline. The new guided request lane can reach a human without any model. The first-party reviewer uses the existing LLM seam and is off by default.

## Invariants

1. A provider disposition is an assessment, never proof that an answer-required request was satisfied.
2. Human answer + durable reply intent + authoritative human fact commit together, before external I/O.
3. Recorded, staged, delivered, received, and resolved have distinct meanings. Silence is not approval. GET is not acknowledgement. A source receipt does not prove resumed work.
4. Unknown remote send outcomes are not retried automatically. Retry requires source verification; guided polling avoids server-initiated callbacks entirely.
5. Preparation has a time budget and a round budget. Urgent requests bypass it. Incomplete evidence surfaces explicitly rather than hiding the blocker.
6. Published packets are immutable. Context changes require a revision while preparing; a changed question requires a replacement request. Human forms bind the revision reviewed.
7. Client/workspace/host identity comes from server-side credentials, never packet fields. Project strings and citations are source claims, not authorization scopes.
8. Human and agent credentials are distinct. Legacy shared/wildcard ingest credentials cannot also authorize human decisions. One-off answers create no standing grant.
9. One authoritative process owns one workspace/database. Localhost, tailnet and dedicated hosted instances use the same HTTP protocol. Multiple clients are not a replicated control plane.
10. Human complexity stays low: Needs you, Watching, Handled and Settings. Advanced event/provider/legacy views remain inspection tools.

## Why not caller-only triage?

The caller has the cheapest access to task context, so it does initial investigation. But caller quality must not determine whether a request can disappear. Schema-driven guidance and bounded publication are core behavior. A reviewer can synthesize or ask better follow-up questions without becoming the reliability boundary.

## Why not an autonomous CAR manager?

Queue state, expiry, delivery recovery and authorization do not need an LLM. Model execution introduces latency and cost; it is only useful when it improves the decision. The reviewer has one advisory tool and no execution toolbelt. Stronger future callers can bypass it by submitting a complete packet.

## Deliberate boundaries

The protocol enforces structure and lifecycle, not the truth of source claims. It does not certify that the agent genuinely performed an investigation. A source can explicitly report that it cannot investigate; CAR shows that limitation. Model advice and previous decisions are not grants.

Historical review context is scoped to the same authenticated client and host. A shared project label must not let a caller retrieve another client's private decisions. Cross-client semantic correlation remains a future human-facing feature; it must not merge response handles or authority.

The workspace field is not shared-database multi-tenant isolation. Hosted deployment in this implementation means an isolated instance and state directory per workspace. Billing, SSO, multi-human organizations, automatic onboarding, active-active availability and federated server synchronization are not implemented.

## Consequences

The default path needs no model key. Preparation failures cannot suppress a decision past its deadline. Agents can operate over outbound HTTP without exposing their own listeners. The durable client spool requires a live relay/MCP process for automatic retry; it cannot wake a powered-off host. Native transports without receipts can remain uncertain, which is an honest operational state rather than a dashboard success.
