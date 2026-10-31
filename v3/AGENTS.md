# CAR v3: instructions for agents changing this code

Scope: this directory. The repository-root instructions also describe the v2 ticket
runner. **Do not import its filesystem-authoritative lifecycle into v3.** V3 uses
one canonical SQLite database and one daemon owner per workspace.

## Product boundary

Agents bring problems; humans receive grounded decisions. CAR owns the handoff,
not task planning, agent execution, host management or a new general-purpose agent.
A complete source packet needs no model. One optional bounded preparation reviewer
may advise; it cannot answer, grant authority, change a published packet, close an
obligation or hide a blocker. Native integration providers retain their existing
separate typed-effect/safety boundary.

This PR has no deployed v3 users. Bootstrap a clean database from
`src/store/schema.ts`. Do not add upgrade/cutover/import machinery without an actual
supported deployed version and an accepted decision. Reject unknown/nonempty state
without deleting or guessing its contents. Do not change v2 as incidental cleanup.

## Non-negotiable contracts

- Source identity comes from credentials; packet project names, claimed hosts,
  memory, citations and model output cannot create scope or permission.
- Core state is deterministic. Model/client transport failure cannot remove an
  obligation. Silence, timeout and an empty inbox are not approval or global health.
- Publish incomplete context when its bounded preparation window ends. Urgent
  attention must not queue behind optional model work. Do not force agents to
  invent evidence just to satisfy required fields; accept an explicit limitation.
- Freeze published context. A new question, urgency or deadline needs a new request.
  Preserve exact replay identities and use revisions, not timestamps, for CAS.
- Human answer + human fact + durable reply intent commit atomically. External
  transmission happens separately. Never send and *then* pretend to persist it.
- Local spooling, server acceptance, notification, recorded answer, source receipt
  and confirmed unblocking are different facts. GET never acknowledges receipt.
  Standard CLI/MCP `receive` persists locally first. `resolved` requires receipt
  and source confirmation that the blocker is actually gone.
- Expiry is checked when answering/receiving, not only on a timer. Missed guided
  decisions stay in Needs you until reviewed; review preserves the expired outcome.
  Withdrawal is not rollback of already executed work.
- An uncertain native send cannot be replayed blindly. Recovery needs source
  evidence and a current reply revision. A retry must not close a sibling request.
- Agent credentials cannot sign in as humans. Never put the human credential in
  an MCP tool, connection file, URL, log, test fixture or agent environment.
- One API semantics, thin CLI/MCP adapters. Validate before accepting offline work.
  Do not add a second request store or invisible client-side permission cache.

## Human UI contract

Primary navigation is Needs you / Watching / Handled / Settings. Inspectors stay
secondary. A choice shows its exact answer and consequences before commitment.
Show missing evidence and uncertainty near the decision, not behind the approval.
Identify source recommendations and advisory reviewer suggestions. Escape source
text; do not interpret it as raw HTML or executable links.

UI is a projection, not a second state machine. Compute counts with the same
predicates as rows. No hidden obligations beyond page limits. Do not equate
last API contact with liveness. Preserve drafts across blur and live refresh.
Failed POSTs say what is unknown and preserve bounded submitted text, never tokens.
HTML works without JavaScript; JS only enhances refresh and timestamp display.

## Before handing back a change

From `v3/` run:

```sh
bun install --frozen-lockfile
bun run check:foundation
bun run check
bun test
bun run test:portable
```

For UI changes, generate and inspect narrow/mobile and desktop fixtures in light
and dark mode; see `docs/foundation-contract.md`. Also run the real Hono tests.
For lifecycle changes, add a failure/replay/race test, not only a happy-path test.
Add the regression to the contract matrix. For schema changes before first release,
edit the bootstrap and clean-install tests rather than manufacturing migrations.

`check:foundation` fences runtime dependency directions. It is not a correctness
proof. Portable tests are supplemental Node/SQLite/JSX seams, **not** a Bun typecheck
or a full Hono/browser deployment test. Say exactly which checks ran.

Do not relax an invariant or delete a failing guard just to pass CI. Update the
accepted ADR, invariant matrix and negative tests together for an intentional
boundary change. Record unresolved limits; do not describe an untested capability
as production-ready. Read `docs/architecture/0003-pre-release-foundation.md` and
`docs/foundation-contract.md` before changing the decision lifecycle.
