# PMA Hub Frontend - Agent Guide

This SvelteKit app is the default CAR hub UI. It builds into
`../pma_static/`, which is what `make serve`, release smoke tests, and deployed
hubs serve by default.

## Keep Straight

- App source: `src/codex_autorunner/pma_frontend/src/`
- Built output: `src/codex_autorunner/pma_static/`
- Design system: `DESIGN.md`
- Legacy/reference UI: `../static_src/` and `../static/`; run it with
  `make serve-legacy-ui` or `CAR_ENABLE_LEGACY_UI=1`.

## Validation

- Run `pnpm pma:lint` after Svelte or TypeScript changes.
- Run `pnpm pma:test` for PMA frontend unit tests.
- Run `pnpm run build` or `make build` to regenerate committed `pma_static/`.
