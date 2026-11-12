# CAR v3 — grounded decisions between people and agents

**Agents bring problems. Humans receive decisions.** CAR does not run your projects
or replace their harnesses. It makes the questions that need human judgment easier
to answer, records the answer durably, and tracks whether it reached the source and
actually unblocked the work.

V3 is an **unreleased PR**, not an upgrade of a deployed service. It has a clean
SQLite bootstrap, no v3 migration chain and no v2 cutover command. The v2/Python tree
is unchanged. See [deployment](docs/deployment.md) and [actual validation](VALIDATION.md).

## Start here

Requires Bun. From this directory:

```sh
bun install --frozen-lockfile
bun run src/cli.ts init
bun run src/cli.ts serve
```

Open `http://127.0.0.1:7171/ui`. Local setup opens the UI without a human token.
To require a login, set `http.web_auth = "required"` and configure a web token.
Give an agent only its `agent.json`, never the server credential file. No model or
Telegram credential is needed for the default guided-decision path.

The ordinary interface has four places:

| View | Meaning |
|---|---|
| **Needs you** | Grounded questions plus missed deadlines still needing review |
| **Watching** | Context gathering, recorded answers and work awaiting receipt or unblocking |
| **Handled** | Explicit outcomes: source-confirmed resolution, withdrawal or reviewed misses |
| **Settings** | Connections, optional reviewer status, standing permissions and secondary inspectors |

An empty queue is not a health check for unobserved agents. A recorded answer is
not delivered work. Source-confirmed resolution is the authenticated source's
report, not a claim of independent verification.

## Let an agent ask well

```sh
bun run src/cli.ts raise --key api-migration-compatibility-1 \
  --file examples/attention/migration-decision.json
bun run src/cli.ts request get req_RETURNED_ID
bun run src/cli.ts wait req_RETURNED_ID --timeout 60
bun run src/cli.ts request receive req_RETURNED_ID
# After applying this decision and actually clearing the blocker:
bun run src/cli.ts request ack req_RETURNED_ID --answer reply_RETURNED_ID \
  --outcome resolved --note 'Migration resumed with compatibility retained'
```

Use the returned IDs. The example's evidence is illustrative, not evidence about
this repository. Set `CAR_CONNECTION_FILE` for a nondefault agent profile.

The tool asks for missing context rather than relying on a skill being read. A
minimum packet has a goal, blocker and question. CAR returns machine-readable next
actions for gathering evidence, describing attempts, giving a recommendation and
explaining why human judgment is needed. A source may explicitly explain why it
cannot investigate. It should never invent facts just to get through a form.

Preparation is bounded; urgent requests bypass it. Published decisions are frozen.
The human sees an attributed recommendation, uncertainties, evidence and the exact
answer/consequences of each option before choosing. An optional single preparation
reviewer can improve incomplete packets; it cannot answer, grant authority, execute
work or hide the obligation. Complete callers bypass model work entirely.

Read the [HTTP / CLI / MCP protocol](docs/attention-protocol.md). `card schema`
exposes the current schemas; `car_guide` is the MCP workflow guide. Standard `receive`
writes the answer locally before receipt acknowledgment. GET, timeout and silence
never confer approval or imply receipt.

## One server, local or remote clients

One logical server owns a workspace/database. Outbound-only clients use the same
HTTP protocol on localhost, across a tailnet, or with a dedicated hosted instance.
Each host gets a separate authenticated identity. The durable local spool handles
connectivity failure without inventing server acceptance. A running relay or MCP
process retries; a powered-off host cannot.

This is not an active-active control plane or a shared-database multi-tenant SaaS.
See [first installation and remote deployment](docs/deployment.md) for credentials,
TLS/proxy origin, tailnet opt-in and the isolated-hosted boundary.

## Existing native integrations

The guided request lane and native event adapters share incidents, escalation,
audit and durable reply plumbing. Native inputs continue through the router and
typed provider/effect safety boundary; they do not need to be relaunched by CAR.
The event stream and diagnostic views are secondary, not the human home page.

Integration references: [generic events](docs/generic-events.md),
[reply-file contract](docs/replies-file-contract.md), and the source-specific guides
in [docs](docs/). The configuration schema is in `src/config/config.ts`.

Native delivery can remain staged or uncertain when a harness cannot provide a
receipt. Watching exposes evidence-based reconciliation rather than blind resend.
Existing provider judgment remains advisory; grants, deterministic safety, budgets,
leases and audit remain core-owned. A one-off human answer creates no standing grant.

## Maintain the foundation

```sh
bun run check:foundation
bun run check
bun test
bun run test:portable
bun run test:smoke
```

Read [AGENTS.md](AGENTS.md), the [invariant-to-test map](docs/foundation-contract.md),
and [ADR 0003](docs/architecture/0003-pre-release-foundation.md) before changing the
lifecycle. Architecture/import checks run in CI beside the Bun suite and supplemental
portable tests. Runtime correctness, negative/replay cases and UI evidence matter
more than a larger checklist of product features.

Architecture history: [ADR 0001](docs/architecture/0001-attention-router-capability-providers.md),
[ADR 0002](docs/architecture/0002-grounded-decisions.md),
[historical scars](docs/architecture/HISTORICAL_SCARS.md), and the detailed native
router [design](DESIGN.md). ADR 0003 supersedes old migration and power-user UI plans.

For an isolated seeded interactive preview: `bun run scripts/ui-preview.ts`. It
uses in-memory state and no live delivery channels. Its human sign-in credential is
`preview-token`, useful only for that fixture. Browser fixture checks and their
limitations are documented in [foundation-contract.md](docs/foundation-contract.md).
