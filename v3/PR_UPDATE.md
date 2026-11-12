# Updating the existing CAR v3 pull request

> Integrated on 2026-09-05 using the cumulative patch against `82d34c98`.
> Do not reapply either patch to this tree. The instructions below describe the
> original bundle delivery. See `VALIDATION.md` for the subsequent Bun and live
> browser review, integration fixes, and remaining deployment qualification.

This is the **second implementation/review pass**, incorporating that v3 is an
unreleased PR with no users or deployment to migrate. Changes are confined to `v3/`
and `.github/workflows/v3.yml`; the v2/Python tree is unchanged.

## Choose one patch baseline

The accompanying patch bundle has two alternative patches. **Apply only one.**

- `incremental-from-previous-update.patch`: your branch already includes the earlier
  `CAR-v3-updated-repository.zip` / `CAR-v3-update.patch` delivery.
- `cumulative-from-original-upload.patch`: your branch still matches the originally
  supplied `codex-autorunner-car-v3.zip` snapshot.

The original upload SHA-256 is
`b71ca6fe3c7bf4feb4d25c4f9ec7a2c4f1b057b94423acdef8db978aae0fd545`.
The bundle records both baseline archive hashes, affected paths and verification.
Use the complete reviewed repository ZIP as an alternative source tree; honor
removals rather than overlaying files and retaining the deleted migration modules.

From the repository root, after committing or stashing unrelated work:

```sh
git apply --check /path/to/CHOSEN.patch
git apply /path/to/CHOSEN.patch
cd v3
bun install --frozen-lockfile
bun run check:foundation
bun run check
bun test
bun run test:portable
bun run test:smoke
```

If your PR has diverged, reconcile the differences rather than forcing the patch.
Never commit credentials, databases, connection files or generated dependencies.

## What this pass improves

- Single clean schema with database identity, constraints, reopen tests and safe
  refusal of unknown state. Removed unused migration/cutover modules and advice.
- Missed guided decisions stay in Needs you until explicit review. Receipt and
  expiry are checked at point of use; stale pages cannot approve elapsed work.
- Exact context retries, monotonic reply revisions, source-private pagination and
  machine-readable next actions across the shared HTTP/CLI/MCP contract.
- Offline acceptance distinguishes durable local spooling from server acceptance;
  rejected requests are quarantined. Saved answer receipts are verified before ACK.
- MCP validates before accepting work, bounds frames/concurrency, negotiates supported
  versions, rejects duplicate active IDs and suppresses cancelled late responses.
- Optional preparation is actually cancellable and cannot publish late advice.
- Human cards show exact answer/consequences, attributed recommendations, uncertainty
  and evidence. Recorded/received/unblocked states stay distinct. Drafts survive blur
  and live refresh; failed submissions preserve bounded text without false success.
- Pure view projections, shared count predicates, mobile/dark-mode fixture checks,
  revision-bound native recovery, closed effect outcomes and no false fallback success.
- `AGENTS.md`, accepted ADR 0003, deployment guide, invariant-to-test matrix and
  executable dependency guards in CI for future agents.

## Where to begin reading

`README.md` is the human/agent entry point. `AGENTS.md` and
`docs/foundation-contract.md` are the change contract. ADR 0003 explains the
pre-release simplification and superseded decisions. `docs/attention-protocol.md`
contains machine-facing semantics and `docs/deployment.md` the local/remote setup.

Read `VALIDATION.md` before merging: 102 portable tests and Chromium fixture checks
were executed; the full Bun/typecheck and live deployment checks were not available.
