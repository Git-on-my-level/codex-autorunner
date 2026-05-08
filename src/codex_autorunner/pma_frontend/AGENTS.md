# PMA Hub Frontend - Agent Guide

This SvelteKit app is the default CAR hub UI. It builds into
`../pma_static/`, which is what `make serve`, release smoke tests, and deployed
hubs serve by default.

## Keep Straight

- App source: `src/codex_autorunner/pma_frontend/src/`
- Built output: `src/codex_autorunner/pma_static/`
- **Hub deep links**: SvelteKit client routing does not run until `index.html`
  loads. If you add routes under `src/routes/` that users can refresh or open in
  a new tab, the FastAPI hub must serve that same HTML for those paths (see
  `surfaces/web/AGENTS.md` section “PMA Hub SPA shell” and the `PMA Hub SPA shell`
  comment block in `../surfaces/web/app.py`). Prefer documenting a new deep path in
  `tests/surfaces/web/test_pma_static_routes.py`.
- Design system: `DESIGN.md`
- Legacy/reference UI: `../static_src/` and `../static/`; run it with
  `make serve-legacy-ui` or `CAR_ENABLE_LEGACY_UI=1`.
- PMA chat renders the backend canonical timeline (`/hub/pma/threads/{id}/timeline`). Frontend helpers may map canonical items to cards and reconcile temporary optimistic items by stable backend IDs, but must not compose `/turns` into a parallel transcript or own final-delivery state.

## Validation

- Run `pnpm pma:lint` after Svelte or TypeScript changes.
- Run `pnpm pma:test` for PMA frontend unit tests.
- Run `pnpm run build` or `make build` to regenerate committed `pma_static/`.
