# Validation and handoff — pre-release foundation review

## Local integration review — 2026-09-05

The reviewed cumulative patch was applied to PR #2090's `82d34c98` baseline and
tested with Bun 1.4.0 and the installed dependency versions. Frozen installation,
foundation boundaries, dependency-aware TypeScript, **767 Bun tests**, **102 portable
tests**, and **17 daemon smoke assertions** passed. The seeded UI preview also starts.
These results supersede the original bundle's unavailable-Bun limitation below.

Final review also added native deadline sweeping after routing, terminal-outcome
preservation during delivery reconciliation, authenticated exact-request Multica
closure, and canonical schema validation beyond version headers. Regressions cover
live siblings, foreign sources, late closure, in-flight delivery uncertainty, and
partial databases without changing their contents.

Integration fixed Hono error-page serialization, typed test responses, authenticated
private-page fixtures, provider fallback deadlines under replay clocks, strict
one-of answer payloads, and the preview's missing receipt step. The smoke check now
requires authenticated Markdown, not a successful redirect to the login page.

Real browser testing found that `Referrer-Policy: no-referrer` produced an opaque
Origin on login POSTs. `same-origin` preserves local form origins without exposing
cross-origin referrers; opaque and cross-origin writes remain rejected. Sign-in,
recording an answer, CLI durable receipt, and explicit source resolution were tested
against a fresh loopback daemon. The previous development state was preserved.
Screenshots covered desktop and mobile dark UI and narrow/desktop light source
fixtures; checked widths did not overflow. Source fixtures remain distinct from
live HTTP evidence. A second sample decision is left pending for local testing.

No live Telegram, Hermes/model, tailnet, reverse proxy, or production deployment
qualification is claimed. GitHub's Linux/macOS jobs provide the supported-runtime
checks; see the PR for their current status.

## Executed in this environment

| Check | Result and scope |
|---|---|
| Portable regression suite | **102 passed, 0 failed**, Node 22.16.0. Actual core/store/client/effect code with a narrow Node SQLite seam; UI uses a test-only JSX renderer. |
| TypeScript syntax | **154 source/test files**, zero syntax/transpilation errors. Separate TypeScript scripts also syntax checked. This is not dependency-aware typechecking. |
| Runtime dependency boundaries | Passed for the actual source tree. Negative tests exercise transitive imports, `require`, dynamic imports, extensionless imports and type-only imports. |
| Chromium fixture checks | **7 scenario groups passed**: 320, 390 and 1440px in both light/dark modes, plus an expired-decision scenario. Exact options, uncertainty, labels, mobile navigation, no horizontal overflow, draft retention after blur/refresh and stale-answer removal checked. |
| Manual visual inspection | Mobile light and desktop dark decision screenshots inspected. These are actual view-source fixtures, not a live daemon. |
| Patch verification | Incremental and cumulative patches are independently applied to their corresponding supplied archives during packaging, then compared file-by-file with the final source tree. See the patch bundle's `PATCH_VERIFICATION.json`. |

The portable run and browser assertions are recorded in `docs/validation/`.
The verifier compiles actual source, extracts exact dependency-free contract helpers,
and substitutes only the documented runtime/JSX seams. Fake transports exercise
success/failure/unknown remote outcomes. It does not replace business logic with a
second implementation. A passing fixture test is not a successful Hono integration.

Coverage added in this pass includes clean bootstrap/reopen, refusal of unknown
state, database uniqueness/revision constraints, point-of-use expiry, visible missed
decisions, human review rollback, receipt-before-resolution, exact replay, safe
recovery CAS, owner isolation, keyset pagination, offline rejection quarantine,
receipt integrity, bounded MCP work/cancellation, closed effect outcome vocabulary,
escaped UI content and retained failed-submission drafts.

## Not executed here — required before merge

**Bun and the external dependencies were unavailable.** A direct TypeScript check
stopped because `bun-types` could not be found. No successful frozen installation,
full dependency-aware typecheck, full `bun test`, or daemon smoke-test pass is claimed.
A supplemental compiler inspection helped catch an invalid effect outcome; its
missing-dependency diagnostics are not a substitute for the required check.

The Bun-native integration tests are included, including actual Hono routes, strict
Zod/MCP validation, human/agent authentication, review/withdrawal, stale-form draft
retention, reviewer cancellation and native fallback effects. They **have not been
executed here**. The existing Linux/macOS CI runs installation, boundary checks,
typechecking, Bun tests and portable tests. Run the smoke test as well before release:

```sh
bun install --frozen-lockfile
bun run check:foundation
bun run check
bun test
bun run test:portable
bun run test:smoke
```

There was no live Telegram delivery, external model call, tailnet deployment,
reverse-proxy test, load test or security audit. Browser checks load generated view
fixtures without HTTP and inline the identical refresh script at body end. They do
not exercise login, CSP enforcement or Hono middleware. Those acceptance checks
remain necessary; see `docs/foundation-contract.md`.

## Scope

This is an unreleased PR with a **single clean bootstrap**, not a migration of users
or v2 state. Unknown databases are refused without conversion/deletion. The v2 tree
is unchanged. There is no artificial migration/cutover support promise.

One server owns one isolated workspace. Local/tailnet/outbound hosted clients share
one protocol; this is not a finished shared multi-tenant SaaS, active-active service,
SSO/billing system or arbitrary remote-execution platform. The optional preparation
reviewer is advisory and off by default. Recommendations are not grants; received
answers are not independent proof of task success. Native delivery uncertainty
remains visible and cannot be retried on an assumption of non-delivery.
