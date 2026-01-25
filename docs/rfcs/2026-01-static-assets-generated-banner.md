# RFC: Mark compiled static assets as generated (issue #410)

## Summary
Compiled JS in `src/codex_autorunner/static/` lacks a generated header, inviting accidental manual edits and drift from `static_src/`. This RFC proposes adding a build-time banner and CI guard to enforce reproducible output.

## Proposed approach
1) Add a banner via the TS build (e.g., esbuild/tsc emit header) in `static_src` build pipeline: `// GENERATED FILE - do not edit directly. Source: static_src/...`.
2) Add CI check to run `pnpm build` and assert `git diff --exit-code src/codex_autorunner/static/`.
3) Document in contributor guide that `static_src/` is the source of truth; `static/` is generated.
4) Optionally exempt `static/` from lint autofix or add pre-commit rule preventing manual edits.

## Acceptance criteria
- Every file under `src/codex_autorunner/static/` carries a clear generated banner.
- CI fails if compiled output differs from committed output.
- Docs note `static_src/` as canonical source.

## Tracking
Fixes #410.
