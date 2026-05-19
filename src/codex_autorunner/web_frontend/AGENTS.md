# Web Hub Frontend - Agent Guide

This SvelteKit app is the default CAR hub UI. It builds into the ignored
`../web_static/` package-artifact directory. Normal PRs should commit source,
tests, and docs only; release/package builds produce and verify the static bundle.

## Keep Straight

- App source: `src/codex_autorunner/web_frontend/src/`
- Built output: `src/codex_autorunner/web_static/` (ignored package artifact)
- **Hub deep links**: SvelteKit client routing does not run until `index.html`
  loads. If you add routes under `src/routes/` that users can refresh or open in
  a new tab, the FastAPI hub must serve that same HTML for those paths (see
  `surfaces/web/AGENTS.md` section “Web Hub SPA shell” and the `Web Hub SPA shell`
  comment block in `../surfaces/web/app.py`). Prefer documenting a new deep path in
  `tests/surfaces/web/test_web_static_routes.py`.
- Design system: `DESIGN.md`
- PMA chat renders the backend-owned transcript projection (`/hub/pma/threads/{id}/transcript` and `/transcript/events`). Frontend helpers may map transcript rows to cards and keep temporary optimistic user rows until the backend confirms them, but must not compose `/turns`, `/tail`, or `/timeline` into a parallel transcript or own final-delivery state. `/timeline` and `/tail` are diagnostics/projection inputs, not the primary chat rendering contract.
- Screen data should come from Web Hub read models, not broad page-local
  choreography. For chats, repo/worktree, ticket, run, and artifact surfaces,
  prefer `src/lib/data/readModelClients.ts`, `readModelStream.ts`,
  `readModelStore.ts`, and selectors in `readModelViewModels.ts`.
- Route loaders: keep testable helpers in `src/lib/routes/` (or another
  non-`+page.ts` module). SvelteKit `+page.ts` / `+page.server.ts` may export
  only `load`, `prerender`, `csr`, `ssr`, `trailingSlash`, `config`, `entries`,
  or `_`-prefixed symbols. Type-only exports are fine. See
  `src/lib/routes/pageModuleExports.test.ts`.
- Route load tests should import helpers through
  `src/lib/test/importRouteLoader.ts` instead of re-implementing the browser
  environment mock in every file.
- Normal updates should arrive through cursor streams plus repair snapshots.
  Do not add recurring `setInterval`/quiet-refresh loops for migrated screens.
- High-cardinality UI must stay windowed and virtualized. Do not render
  unbounded chat, timeline, repo/worktree, ticket, artifact, or dispatch lists
  with raw `{#each}` loops.
- Legacy broad client methods are diagnostics/tests-only unless a durable doc
  explicitly says otherwise. A new screen shape needs a backend projection,
  typed event/contract, selector, and scale test.

## Validation

- Run `pnpm web:lint` after Svelte or TypeScript changes.
- Run `pnpm web:test` for Web frontend unit tests.
- With `make serve` running, run `pnpm web:smoke:dev` to catch Vite dev-server
  client routing regressions on repo deep links.
- Run `pnpm run build` or `make build` to validate and locally regenerate ignored
  `web_static/`.
