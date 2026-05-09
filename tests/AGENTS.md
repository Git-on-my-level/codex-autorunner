# Tests - Agent Guide

Use this directory as the router for frontend and web-ui validation.

## Frontend Test Map

- `src/codex_autorunner/pma_frontend/src/**/*.test.ts`: default Svelte PMA Hub tests
- `tests/js/`: browserless legacy UI tests (removed — legacy source deleted)
- `tests/surfaces/web/`: FastAPI route and web-service tests for `src/codex_autorunner/surfaces/web/`
- `tests/surfaces/web/test_pma_static_routes.py`: PMA `index.html` shell for hub UI paths (manual deep-link examples)
- `tests/surfaces/web/test_pma_spa_shell_contract.py`: shell coverage for every `pma_frontend/src/routes/**/+page.svelte` probe (see `scripts/check_pma_hub_spa_shell.py`)
- Root web-ui tests: `tests/test_static_asset_cache.py`, `tests/test_auth_middleware.py`, `tests/test_hub_ui_escape.py`, `tests/test_voice_ui.py`, plus the `tests/test_app_server*.py`, `tests/test_base_path*.py`, and `tests/test_static*.py` families

## Quick Selection

- PMA UI behavior: start with `pnpm pma:test`
- Legacy UI behavior: removed (legacy source deleted)
- HTML or DOM contract changes: run `make frontend-check`
- Web route/service changes: run `python -m pytest -q tests/surfaces/web ...`
- Static asset loading, caching, or auth/base-path changes: include the matching root web-ui tests

## Frontend-Backend Contract Tests

- `tests/contracts/`: Python contract tests for scope, memory, surface, and ticket stores
- `tests/contracts/surface/test_fake_surface_e2e.py`: fake-surface E2E journey (create chat, send message, open memory, link ticket)
- `src/codex_autorunner/pma_frontend/src/lib/viewModels/frontendContracts.test.ts`: TypeScript contract tests verifying frontend scope URN, labels, query formatting, and memory rendering match backend domain contracts
- Run contracts: `python -m pytest -q tests/contracts/`
- Run frontend contracts: `pnpm pma:test`
- The E2E tests are fast (<10s) and run in every local check; no slow-test split is needed.

## Validation Lane

- These paths all map to the `web-ui` validation lane in `src/codex_autorunner/core/validation_lanes.py`.
