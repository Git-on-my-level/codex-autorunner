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

## Validation

- Run `pnpm web:lint` after Svelte or TypeScript changes.
- Run `pnpm web:test` for Web frontend unit tests.
- Run `pnpm run build` or `make build` to regenerate committed `web_static/`.
