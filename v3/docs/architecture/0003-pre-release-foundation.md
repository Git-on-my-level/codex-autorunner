# ADR 0003: Clean pre-release foundation and truthful decision outcomes

Status: accepted. Supersedes v3 migration/cutover requirements and the old
power-user-first UI in DESIGN and ADR 0001. Extends ADR 0002; does not replace the
native integration provider/effect/safety architecture.

## Context

There is no deployed v3 database or v3 user to migrate. Building a transition system
for imaginary deployments increases maintenance and obscures the core model. The
second review also found inconsistent handling of missed decisions, optimistic
client response language, timestamp-based recovery checks, and human draft loss.

## Decisions

**One bootstrap, no fictional migration history.** `schema.ts` describes the whole
current schema. A new empty database receives CAR3 application identity and schema
version 1 transactionally. A recognized workspace reopens; any other nonempty or
versioned database fails without conversion or data deletion. v2 remains untouched
in its own tree. Initial release is the point to establish upgrade compatibility.

**Request lifetime and successful completion are different.** Preparing, needs_you,
answered and received are active states. Resolved requires exact receipt followed
by source confirmation. Cancelled is withdrawn, not successful. Expired is missed,
not approved. The human may review an expired outcome without rewriting it as
resolved. Every guided request belongs to exactly one primary board, including a
missed request awaiting review.

**Expiry is enforced on use.** The background sweeper makes states timely but is not
a correctness dependency. Answering or receiving after the deadline fails. Once the
source has durably received an answer before its deadline, it may finish work later.
The receipt deadline is not an execution time limit. Withdrawal cannot undo work
already performed; a source must check the current request before acting.

**Retry semantics are explicit.** Exact enrichment replay returns the existing
revision rather than editing twice. Delivery reconciliation compares monotonically
increasing revisions, not wall-clock timestamps. Agent pagination is chronological
keyset pagination, not lexical random-id pagination. Known rejected submissions are
preserved privately outside the automatic retry queue. Network uncertainty preserves
the original request key; auth failure pauses a batch rather than dropping work.

**Agent tools carry executable guidance.** `car.guidance.v1` includes a next-action
code, tool, arguments, required input and whether receipt has completed. The guide,
CLI and MCP are adapters over the same request API. `eligible_for_receipt` means
only that an answer can enter the receipt protocol. `guidance.can_apply_answer`
becomes true after receipt and is scoped to this exact request; neither overrides
a harness's own constraints nor creates standing authority.

**One optional, bounded reviewer.** It receives no general toolbelt and no mutation
of the authoritative packet. Deadline/stop signals propagate to the model runner;
late results are rejected even when publication has not yet been swept. Remote
model billing/processing may already have happened; cancellation is not a refund or
a reversal. MCP likewise suppresses late cancelled responses without pretending an
already committed remote request was undone.

**Decision-first interface.** Recommendations are attributed; uncertainty is visible.
Buttons cannot conceal a larger action behind a short label: show exact answer and
consequences. Needs you includes missed decisions requiring acknowledgment. Watching
reports receipt/delivery uncertainty honestly. Handled is labelled as a record of
outcomes, including cancellations and reviewed misses, not a success scoreboard.
Counters use shared queries. Forms bind revisions. Auto refresh never discards a
draft after blur. Error pages preserve bounded decision text and say “not confirmed.”

**Enforce architecture, not just prose.** An AST import-graph guard prevents the
core from depending on models/providers, pure decision views from owning mutations,
and agent client/MCP from importing server/human authority. Tests cover lifecycle,
transport recovery, scope, UI projections and the guard's negative cases.

## Non-goals and limits

Single host, tailnet and a dedicated hosted instance use one logical server/API.
Shared-database tenancy, multiple human roles, SSO/billing, federation, HA failover,
unlimited unattended retention, guaranteed delivery through offline hosts, semantic
cross-client correlation and a general-purpose autonomous CAR manager are not
implemented. Future work must not claim one of these merely because a workspace ID
or an HTTP endpoint exists.

The core can enforce provenance structure and transition validity. It cannot prove
that a source told the truth, really resumed work, or did not execute outside CAR.
Source-confirmed means reported by that authenticated source, not independently
verified. Grouping related human decisions in the future must preserve separate
native response handles, deadlines and permission scopes.

## Acceptance

See the executable mapping and release checks in `../foundation-contract.md`.
No architecture guard replaces full runtime tests. `VALIDATION.md` records what was
actually executed for this revision versus what still needs Bun/deployment testing.
