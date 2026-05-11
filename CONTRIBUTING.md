# Contributing

Thanks for helping improve codex-autorunner.

## Ground rules
- Keep changes small and focused.
- Keep docs in sync with behavior changes.
- Avoid unnecessary dependencies.

## Proposing changes
- Open an issue for bugs or larger changes so we can align first.
- For small fixes, a focused PR is fine without prior discussion.

## Development
- Bootstrap dev env (venv, dev deps, npm deps, hooks): `make setup`
- Install dev deps: `pip install -e .[dev]`
- Run tests: `.venv/bin/python -m pytest` after `make setup` (or `make test`)
- Lane-aware validation: `./scripts/check.sh` (auto-detects scope from staged files)
- Force full validation: `./scripts/check.sh --full` (or `make check-full`)
- JS lint (UI): `pnpm run lint:js`
- Format: `python -m black src tests`
- Build Web Hub frontend: `pnpm run build` (source is `src/codex_autorunner/web_frontend/`, output is `src/codex_autorunner/web_static/`)

## Validation lanes
The pre-commit hook and CI use lane-based validation to avoid running unnecessary checks:
- `core`: Backend/logic changes only (no frontend build or JS tests)
- `web-ui`: Frontend/UI changes (includes core + frontend build + JS lint + JS tests)
- `web-core-contract`: Scoped core + Web surface contract changes (same checks as `web-ui`, without chat-app validation)
- `chat-apps`: Chat integration changes (Discord/Telegram adapters and tests)
- `aggregate`: Full validation (all of the above; used for broad multi-lane or shared-risk diffs)

Lane is auto-detected from changed files. Force a specific lane with `./scripts/check.sh --lane <lane>`.

## Test hermeticity
All tests must use isolated temp directories via fixtures (`tmp_path`) rather than writing to shared `/tmp` paths. A guard (`scripts/check_test_tmp_usage.py`) enforces this automatically during `check.sh` and CI.

## Pull requests
- Explain the user-facing impact.
- Include tests when behavior changes.
- Update relevant docs if you touch config or UX.
