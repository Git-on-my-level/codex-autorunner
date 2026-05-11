# Web Hub Frontend - Agent Guide

This SvelteKit app is the default CAR hub UI. It builds into
`../web_static/`, which is what `make serve-hub`, release smoke tests, and deployed
hubs serve by default.

## Keep Straight

- App source: `src/codex_autorunner/web_frontend/src/`
- Built output: `src/codex_autorunner/web_static/`
- **Hub deep links**: SvelteKit client routing does not run until `index.html`
  loads. If you add routes under `src/routes/` that users can refresh or open in
  a new tab, the FastAPI hub must serve that same HTML for those paths (see
  `surfaces/web/AGENTS.md` section “Web Hub SPA shell” and the `Web Hub SPA shell`
  comment block in `../surfaces/web/app.py`). Prefer documenting a new deep path in
  `tests/surfaces/web/test_web_static_routes.py`.
- Design system: `DESIGN.md`
- PMA chat renders the backend canonical timeline (`/hub/pma/threads/{id}/timeline`). Frontend helpers may map canonical items to cards and reconcile temporary optimistic items by stable backend IDs, but must not compose `/turns` into a parallel transcript or own final-delivery state.
- Screen data should come from Web Hub read models, not broad page-local
  choreography. For chats, repo/worktree, ticket, run, and artifact surfaces,
  prefer `src/lib/data/readModelClients.ts`, `readModelStream.ts`,
  `readModelStore.ts`, and selectors in `readModelViewModels.ts`.
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
- Run `pnpm run build` or `make build` to regenerate committed `web_static/`.
