# Tests - Agent Guide

Use this directory as the router for frontend and web-ui validation.

## Frontend Test Map

- `tests/js/`: browserless JS tests for `src/codex_autorunner/static_src/*.ts`
- `tests/surfaces/web/`: FastAPI route and web-service tests for `src/codex_autorunner/surfaces/web/`
- Root web-ui tests: `tests/test_static_asset_cache.py`, `tests/test_auth_middleware.py`, `tests/test_hub_ui_escape.py`, `tests/test_voice_ui.py`, plus the `tests/test_app_server*.py`, `tests/test_base_path*.py`, and `tests/test_static*.py` families

## Quick Selection

- TS-only UI behavior: start with `pnpm test:markdown`
- HTML or DOM contract changes: run `make frontend-check`
- Web route/service changes: run `python -m pytest -q tests/surfaces/web ...`
- Static asset loading, caching, or auth/base-path changes: include the matching root web-ui tests

## Validation Lane

- These paths all map to the `web-ui` validation lane in `src/codex_autorunner/core/validation_lanes.py`.
