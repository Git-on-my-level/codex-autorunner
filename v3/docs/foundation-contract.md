# Foundation contract and regression map

These are product/engineering contracts, not optional prompting tips. The closest
agent instructions are `../AGENTS.md`; ADR 0003 records the choices. Add an entry and
a negative test when changing an invariant. Never optimize only for fewer cards or
more “resolved” rows: missed blockers and false success make those metrics misleading.

## Executable checks

| Contract | Executable regression coverage |
|---|---|
| Empty CAR3 bootstrap, reopen, one answer/request and positive revisions; reject unknown state unchanged | `test/store.test.ts`; `test/portable/core.case.mjs`: fresh workspace, reopen, constraints, unknown state |
| No model dependency in deterministic core, no human authority in agent clients, pure decision views | `scripts/check-foundation.cjs`; `test/portable/architecture.case.mjs` tests transitive imports, `require`, dynamic import, type-only imports |
| Missing context never hides an urgent/overdue blocker | portable core: incomplete/complete/urgent/preparation budgets; `test/attention/integration.test.ts`: stopped/late reviewer |
| Frozen surfaced packets and exact replay | portable core: context replay, identity changes, frozen packet, stable publication |
| Human input and delivery intent atomic; no standing grant | portable core: transaction rollback, answer replay, revision/options; integration: HTTP/human boundary |
| Point-of-use expiry, receipt before resolution, no premature success | portable core: answer expiry without timer, receipt_required, duplicate receipt, resolution after receipt deadline |
| Missed decisions remain visible; review preserves expiry | portable core: board counts/review/rollback; integration: agent cannot review; portable UI: expired cards |
| Effect outcomes stay in a closed vocabulary; fallback is not completion | portable core: invalid post-execution outcome and no replay; `test/effects/adapters.test.ts`: queued/degraded remains staged |
| Uncertain transmission needs explicit evidence and current revision | portable core: crash-during-send, native recovery, same-timestamp stale CAS, sibling obligation |
| Server/client/host scope cannot be selected by packet fields | integration: credentials/HTTP isolation; portable core: owner isolation; client: audience-bound spool |
| Before network send, persist locally; known reject is not “accepted locally” | portable client: on-disk-before-send, 400 HTML quarantine, poison request, auth batch pause |
| Local answer persistence precedes exact receipt, not execution | portable client: receipt-before-ACK, mismatched answer identity/receipt, no GET/WAIT acknowledgement |
| CLI/MCP validate before offline acceptance | required Zod parser in CLI/MCP; integration schema tests; portable MCP validates before client call |
| Reply payload is exactly one text answer or boolean approval, before persistence or receipt | `test/attention/payload.test.ts`: core/session rejection and malformed server response |
| Browser sign-in preserves same-origin forms without accepting opaque/cross-origin writes | `test/attention/integration.test.ts`: referrer policy and cookie-origin checks; live browser round trip |
| Native expiry is durable after routing, preserves in-flight uncertainty and sibling obligations, and cannot be rewritten by reconciliation | `test/attention/native-expiry.test.ts`; native card outcome regression |
| Multica closure names one exact authenticated source request, never a card-wide or expired outcome | `test/ingest/normalizers.test.ts`; `test/router/router.test.ts` |
| MCP bounds work and does not falsely undo remote commits | portable MCP: handshake, duplicate in-flight ID, cancelled late response; transport limits in `mcp.ts` |
| List pagination and board counts cannot silently omit work | portable core: 220-request multi-page test and 76-card native+guided count |
| Human sees exact option answer/consequences and uncertainty | portable UI and real Hono integration rendering tests |
| Source text is not raw HTML; failed POST keeps draft without claiming success | portable UI and real Hono stale form/source escaping tests |
| Read-only views have no action forms; mobile controls accessible | portable UI; development browser checks at 320/390/1440, light/dark |
| Mailbox selection preserves canonical tabs, pagination, exact native replies, and recovery for stale links | `test/web/mailbox.test.ts`; `test/web/native-message.test.ts` |
| Browsers use standards mode; drafts survive blur, scroll, and protected in-app navigation | `test/web/document.test.ts`; `test/web/live-refresh.test.ts` |
| Blur and automatic refresh preserve typed drafts | `scripts/check-ui-browser.py`: close all disclosures, run refresh callback, cancel manual refresh |

## Required commands

```sh
bun install --frozen-lockfile
bun run check:foundation
bun run check
bun test
bun run test:portable
bun run test:smoke
```

Portable tests compile actual TypeScript source and run deterministic components on
Node 22's SQLite through a small compatibility seam. UI fixtures use a **test-only**
synchronous JSX renderer. This tests the view's content/projection, not Hono's
implementation. The real Bun/Zod/Hono tests must also pass before merge.

For browser inspection, with Python Playwright and Chromium available:

```sh
CAR_UI_ARTIFACT_DIR=/tmp/car-ui bun run test:portable
python scripts/check-ui-browser.py /tmp/car-ui --chromium /path/to/chromium
```

The script loads generated view fixtures without a network dependency, inlines the
identical refresh enhancement at body end, and writes screenshots/results to that
folder. It does not test login, actual HTTP middleware, live Telegram or a deployed
reverse proxy. Look at the screenshots as well as the assertion count.

## Behavior and UX rules for future changes

A decision must explain the goal, blocker, human judgment needed, attempted
investigation, evidence, recommendation/alternatives, uncertainty and effect of
waiting where known. Missing fields lead to source-facing requests; honest inability
is accepted. Urgency and bounded preparation take precedence over presentation
completeness. A schema cannot certify that an agent investigated honestly.

Do not introduce success-like labels for recorded, staged or delivered answers.
“Handled” includes explicit unsuccessful outcomes; keep outcome labels visible.
Never expose a source-provided URL as an executable/action endpoint or let it select
client identity. Source facts and reviewer advice remain distinguishable.

The standard user flow is read a decision, inspect evidence as needed, record an
answer, then let CAR track receipt and unblocking. Advanced providers/grants/delivery
inspection must not be prerequisites for ordinary decisions. An empty queue says
nothing about hosts CAR does not observe. Last contact is telemetry, not liveness.

## Release evidence still needed

Run the actual Bun suite/typecheck and a daemon smoke test in the supported runtime.
Dogfood the human round trip on one local and one tailnet client. Test process death
at receipt/send boundaries, disconnect/reconnect, native-side answer before a CAR
tap, daemon lease competition, and reverse-proxy origin/HTTPS configuration. Verify
Telegram allowlists and an independent deadman endpoint with real credentials.
Avoid claiming hosted multi-tenancy, universal exactly-once transmission, or a
security audit on the strength of unit tests alone.
